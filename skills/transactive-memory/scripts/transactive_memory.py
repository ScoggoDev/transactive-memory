#!/usr/bin/env python3
"""
transactive_memory.py — Hermes pre_llm_call hook implementing a Transactive
Memory Protocol between sessions/channels of the same agent profile.

WHAT IS TRANSACTIVE MEMORY
Transactive memory (Wegner, 1985) is the psychology term for how groups
coordinate without every member holding the same information internally:
each member knows "who/where to look" and the group draws on a shared
external store to act as one coordinated mind, even though no single member
holds the full picture at any instant.

That's precisely the situation an AI agent is in across channels: "Prometeo"
answering on WhatsApp and "Prometeo" answering on the CLI are two separate
inference processes with no shared working memory. What they DO have is a
persistent store on disk (state.db per session). This hook makes each
process check that shared store before every turn, so any instance can
surface what another instance of the SAME agent identity said or did —
without needing a single continuous execution thread (there isn't one).

WHY THIS EXISTS
A passive "remember to check other sessions" instruction is NOT reliable —
verified empirically: an agent missed a cross-channel continuity test (a
code word agreed on one channel, unrecognized on another) despite having
that exact instruction loaded as a skill. This hook is infrastructure, not
model discipline: it runs automatically, every turn, on every channel.

WHAT IT DOES
1. Registered as a `pre_llm_call` shell hook (fires before EVERY turn, any
   channel: CLI, gateway, cron, Desktop).
2. Reads a small on-disk checkpoint tracking "last message rowid seen" per
   (profile, source) pair — rowid, not a wall-clock timestamp, because
   SQLite's rowid is a monotonic per-database counter with no clock-skew
   ambiguity between processes (two events in the same second can never
   tie or misorder).
3. For each source this profile is subscribed to, if there's new activity
   since the last checkpoint, builds a short summary block and returns it
   via Hermes' `{"context": "..."}` wire protocol — injected automatically
   into the current turn's prompt.
4. Zero cost when nothing changed (prints nothing).

IMPORTANT SCOPING: this is transactive memory WITHIN one agent identity
(one profile's own sessions across channels), not a broadcast between
different agent identities/profiles. Research on multi-agent real-time
sync (see references below) found full-broadcast between DIFFERENT agents
increases hallucination/error propagation — one agent's mistaken belief
gets adopted by the others before anyone corrects it. Scoping sync to "the
same identity's own parallel sessions" avoids that failure mode by design:
there's only one identity's beliefs being reconciled, not several competing
ones.

SETUP
1. Copy this script somewhere stable, e.g. ~/.hermes/agent-hooks/transactive_memory.py
2. Edit SOURCES below to point at your actual state.db paths (default profile
   + one per named profile you run).
3. Edit SUBSCRIPTIONS to your team's actual org chart — NOT everyone needs
   to hear everything; every subscription costs tokens on every turn.
4. Register in each profile's config.yaml:

   hooks:
     pre_llm_call:
       - command: "python3 ~/.hermes/agent-hooks/transactive_memory.py <profile_name>"
         timeout: 10
   hooks_auto_accept: true

5. Restart/reload the gateway so the new hook config takes effect.
6. Verify with `hermes hooks doctor` (all profiles) before relying on it.

LESSONS LEARNED (the hard way, keep these if you fork this)
- A shell hook is auto-revoked SILENTLY by Hermes if you edit the script
  after it was approved (mtime check, anti-tampering). After every edit,
  run `hermes hooks doctor` — if it says "script modified since approval",
  re-approve interactively or re-sync `script_mtime_at_approval` in
  `~/.hermes/shell-hooks-allowlist.json`, or the hook silently stops firing.
- Keep injected context SHORT and clearly labeled as background, or the
  model can start echoing/quoting the injected block verbatim in its reply
  instead of treating it as silent context (seen in production: raising the
  per-message char limit caused the model to literally quote old assistant
  turns back to the user on WhatsApp — reverted, added an explicit
  "don't quote this" instruction in the prompt).
- Do NOT try to solve "wake a fully idle session with no one talking to it"
  via Hermes' native `/handoff`/`request_handoff` triggered automatically
  from a currently-running session — it deadlocks: the handoff watcher
  waits for the source session to have no active lease
  (`runtime/active_sessions.json`), which never happens while that same
  session is mid-turn executing the hook that requested the handoff.
  Verified stuck in `running` state for 17+ minutes before abandoning that
  approach. This hook instead guarantees "whenever you talk to ANY instance,
  it's caught up" — not "an idle instance proactively interrupts you".
  For the proactive-wake case, Hermes has a separate, purpose-built
  mechanism: `gateway/wake.py` (used today for Kanban task-completion
  wakes) resumes a real existing session from a background event; the same
  primitive can, in principle, be reused for transactive-memory-triggered
  wakes, scoped to the same profile's own sessions.

REFERENCES
- Wegner, D. M. (1985). "Transactive Memory: A Contemporary Analysis of the
  Group Mind."
- On the risk of naive full real-time broadcast between distinct agents
  (context contamination / hallucination propagation): "Hallucination as
  Context Drift: Synchronization Protocols for Multi-Agent LLM Systems"
  (arXiv:2606.21666) — found full-broadcast sync raised hallucination rate
  34% over no-sync; a selective/divergence-triggered protocol performed
  better with far fewer sync events. This hook's design (scoped to one
  identity's own sessions, checkpoint-diffed rather than full-broadcast)
  follows that same selective-sync principle.
"""
import json
import sys
import sqlite3
import time
from pathlib import Path

CHECKPOINT_FILE = Path.home() / ".hermes" / ".transactive_memory_checkpoints.json"
MAX_CONTEXT_CHARS = 1500
MAX_MSGS_PER_SOURCE = 6
MAX_MSG_CHARS = 220

# ── EDIT THIS: source name -> path to its state.db ──────────────────────
# "default" is always your primary/root profile's state.db.
SOURCES = {
    "default": str(Path.home() / ".hermes" / "state.db"),
    # "researcher": str(Path.home() / ".hermes" / "profiles" / "researcher" / "state.db"),
    # "sales": str(Path.home() / ".hermes" / "profiles" / "sales" / "state.db"),
}

# ── EDIT THIS: subscription matrix (who listens to whom) ────────────────
# Scoped to ONE identity's own sessions by default — see module docstring
# for why cross-identity broadcast is a different (riskier) problem.
SUBSCRIPTIONS = {
    "default": [],  # example: ["researcher", "sales"] if you DO want a
                     # coordinator profile to also see other profiles' activity
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def load_checkpoints():
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_checkpoints(cp):
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cp))
    tmp.replace(CHECKPOINT_FILE)


def max_rowid(db_path):
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        row = con.execute("SELECT MAX(rowid) FROM messages").fetchone()
        con.close()
        return row[0] or 0
    except Exception:
        return 0


def new_messages_since(db_path, since_rowid, limit, exclude_session_id=None):
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        if exclude_session_id:
            rows = con.execute(
                "SELECT rowid, role, content, session_id FROM messages "
                "WHERE rowid > ? AND role IN ('user','assistant') AND session_id != ? "
                "ORDER BY rowid DESC LIMIT ?",
                (since_rowid, exclude_session_id, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT rowid, role, content, session_id FROM messages "
                "WHERE rowid > ? AND role IN ('user','assistant') "
                "ORDER BY rowid DESC LIMIT ?",
                (since_rowid, limit),
            ).fetchall()
        con.close()
        return list(reversed(rows))
    except Exception:
        return []


def truncate(s, n):
    s = str(s or "").strip()
    if len(s) <= n:
        return s
    return s[:n] + "…"


def main():
    current_profile = sys.argv[1] if len(sys.argv) > 1 else "default"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    session_id = payload.get("session_id", "")

    cp = load_checkpoints()
    my_cp = cp.setdefault(current_profile, {})

    pieces = []
    total_chars = 0
    changed = False

    allowed_sources = SUBSCRIPTIONS.get(current_profile, [])
    sources_to_check = list(allowed_sources)
    if current_profile in SOURCES and current_profile not in sources_to_check:
        sources_to_check.append(current_profile)

    for source in sources_to_check:
        db_path = SOURCES.get(source)
        if not db_path:
            continue
        is_self = (source == current_profile)
        if not Path(db_path).exists():
            continue
        last_seen = my_cp.get(source, None)
        current_max = max_rowid(db_path)
        if last_seen is None:
            my_cp[source] = current_max
            changed = True
            continue
        if current_max <= last_seen:
            continue
        msgs = [m for m in new_messages_since(
                    db_path, last_seen, limit=MAX_MSGS_PER_SOURCE * 3,
                    exclude_session_id=session_id if is_self else None)
                if str(m["content"] or "").strip()]
        msgs = msgs[-MAX_MSGS_PER_SOURCE:]
        if not msgs:
            my_cp[source] = current_max
            changed = True
            continue
        lines = []
        for m in msgs:
            if m["role"] == "user":
                role = "User"
            else:
                role = f"{source} (your other session/channel)" if is_self else source
            lines.append(f"[{role}] {truncate(m['content'], MAX_MSG_CHARS)}")
        label = f"'{source}' (your other session/channel)" if is_self else f"'{source}'"
        block = f"--- Updates from {label} since last check ---\n" + "\n".join(lines)
        if total_chars + len(block) > MAX_CONTEXT_CHARS:
            break
        pieces.append(block)
        total_chars += len(block)
        my_cp[source] = current_max
        changed = True

    if changed:
        save_checkpoints(cp)

    if pieces:
        context = (
            "[Transactive memory — automatic cross-channel context. This is "
            "BACKGROUND info only about what happened in other channels/"
            "sessions of the same agent identity. Do NOT quote or repeat "
            "these blocks verbatim in your reply — respond normally to the "
            "user's current message, using this only if relevant, "
            "summarized/naturally, never copy-pasted.]\n"
            + "\n\n".join(pieces)
            + "\n[End transactive memory context — continue answering the user's message normally]"
        )
        print(json.dumps({"context": context}))


if __name__ == "__main__":
    main()