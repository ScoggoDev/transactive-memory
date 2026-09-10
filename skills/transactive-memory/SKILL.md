---
name: transactive-memory
description: "Use to give a Hermes agent transactive memory across sessions."
version: 1.0.0
author: Lucas Scognamiglio
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [context, continuity, multi-channel, sessions, whatsapp, profiles, sync, hooks, transactive-memory]
    homepage: https://github.com/ScoggoDev/transactive-memory
---

# Transactive Memory Protocol

_Idea and framing by Lucas Scognamiglio. Implementation: a Hermes `pre_llm_call` hook._

## The problem

You talk to your Hermes agent from multiple channels (TUI, WhatsApp,
Telegram...). Each channel/session is an **isolated process** with its own
history in `state.db`. Ask your WhatsApp session about something you told
the TUI session five minutes ago, and it has no idea — even though, in
every meaningful sense to the user, it's "the same agent."

A skill with instructions like "check other sessions before replying" does
**not** fix this reliably — it depends on the model deciding, per turn,
that a message might need cross-channel context. Tested in production and
it failed: a continuity test (a code word agreed on one channel) went
unrecognized on another, despite the agent having that exact instruction
loaded.

## The concept: transactive memory

**Transactive memory** (Daniel Wegner, 1985) is the psychology term for how
groups coordinate without every member holding the same information
internally. No single member has the full picture — instead, each member
knows *where to look*, and the group draws on a shared external store to
act, functionally, as one coordinated mind.

That's the actual situation an AI agent identity is in across channels.
"The agent" answering on WhatsApp and "the agent" answering on the CLI are
two separate inference processes — there is no continuous thread of
execution linking them, and there never will be with today's LLM serving
architecture. What *can* exist is a shared external store (the session
databases) that each process consults before acting. This project applies
transactive memory as an engineering pattern, not a metaphor: the hook
below **is** the shared store lookup, running automatically before every
turn.

**Important framing, stated explicitly so it isn't oversold:** this gives
an agent identity functional/informational continuity across its own
sessions — not a claim about consciousness, subjective experience, or a
single persistent "self." It's a measurable, falsifiable property (does
instance B correctly use information generated in instance A, unprompted?)
implemented with plain infrastructure (SQLite checkpoints), nothing more.

## What's included

- `scripts/transactive_memory.py` — the hook itself. Diffs each subscribed
  source's `state.db` against a lightweight on-disk checkpoint
  (`~/.hermes/.transactive_memory_checkpoints.json`) using SQLite's
  monotonic `rowid` (not wall-clock time — no clock-skew ambiguity between
  processes), and returns Hermes' `{"context": "..."}` payload only when
  there's something new. Zero cost when nothing changed.

## Setup

1. Copy `scripts/transactive_memory.py` to `~/.hermes/agent-hooks/` (or any
   stable path — shell hooks run as subprocesses).
2. Edit `SOURCES`: map each profile name to its `state.db` path. Your
   default/root profile's db lives at `~/.hermes/state.db`; named profiles
   at `~/.hermes/profiles/<name>/state.db`.
3. Edit `SUBSCRIPTIONS`: by default this is scoped to **one identity's own
   sessions** (e.g. your `default` profile's TUI session hearing from its
   own WhatsApp session) — not a broadcast between different agent
   identities/profiles. See "Why not broadcast everything" below before
   widening this.
4. Register in **each profile's** `config.yaml` you want it active on:
   ```yaml
   hooks:
     pre_llm_call:
       - command: "python3 /full/path/to/transactive_memory.py <profile_name>"
         timeout: 10
   hooks_auto_accept: true
   ```
5. Restart/reload the gateway (and any live CLI/TUI/Desktop session) for
   each touched profile.
6. Run `hermes hooks doctor` per profile — confirm `✓ allowlisted`.

## Verifying it works

Plant a distinctive marker on one channel/session, then — in a *different*,
fresh turn on another channel/session of the same profile — ask about it
without mentioning it directly. If the answer is correct, the hook is live
end-to-end. Test the reverse direction too (it's bidirectional by design:
each session excludes only its own `session_id` when reading the shared
store, so any two sessions of the same profile can inform each other,
first-to-speak or not).

## Why not broadcast everything to every agent identity

It's tempting to widen `SUBSCRIPTIONS` so every profile hears every other
profile's activity in real time. Don't, by default. Recent research on
real-time multi-agent synchronization (arXiv:2606.21666, "Hallucination as
Context Drift") found that naive full-broadcast sync between *distinct*
agents **raised hallucination rate 34%** over no sync at all — because a
mistaken belief formed by one agent gets propagated and adopted by the
others before anyone corrects it. A selective, divergence-triggered sync
protocol performed better with far fewer sync events.

This project's default scope (one identity's own parallel sessions, not
different agent identities) sidesteps that failure mode structurally:
there's only one identity's own beliefs being reconciled with itself, not
several distinct agents' competing beliefs being merged. If you do want a
coordinator profile to see other profiles' activity, that's a deliberate,
narrow subscription (see the commented-out examples in `SOURCES`) — not a
default broadcast.

## Gotchas (read before you debug this for an hour)

**1. Silent hook invalidation on edit.** Hermes revokes a shell hook's
approval the moment the script's mtime changes after approval — with no
runtime error, it just stops firing. After every edit, run
`hermes hooks doctor`; if it says `script modified since approval`,
re-approve or sync `script_mtime_at_approval` in
`~/.hermes/shell-hooks-allowlist.json`.

**2. Keep injected context short and clearly labeled.** A verbose,
unlabeled injection can cause the model to echo/quote it back to the user
instead of treating it as silent background — verified in production
(raising the per-message char limit caused the model to literally repeat
old turns to the user on WhatsApp). Keep blocks short, explicitly say
"don't quote this."

**3. This hook does NOT wake a fully idle session.** It only fires when a
turn is already running (someone/something is talking to that session) —
there's no execution to "wake up" otherwise. It guarantees *"whenever you
talk to any instance of this identity, it's caught up with its other
sessions"* — not *"an idle instance proactively interrupts you."* For
genuinely proactive wakes, Hermes has a separate primitive,
`gateway/wake.py` (used today for Kanban task-completion notifications),
that resumes a real existing session from a background event — in
principle reusable for transactive-memory-triggered wakes, still scoped to
one identity's own sessions, but that's a separate, heavier build (a
background watcher process, plus a decision policy for *when* a wake is
warranted — see the hallucination-propagation risk above for why "wake on
every message" is a bad default even within one identity).

## Cost

Each hook invocation is one read-only SQLite query per subscribed source
(no LLM call) — sub-100ms, and prints nothing (zero token cost) when
there's no new activity. Tune `MAX_CONTEXT_CHARS` / `MAX_MSGS_PER_SOURCE` /
`MAX_MSG_CHARS` for tighter cost control.

## References

- Wegner, D. M. (1985). *Transactive Memory: A Contemporary Analysis of the
  Group Mind.*
- "Hallucination as Context Drift: Synchronization Protocols for
  Multi-Agent LLM Systems" (arXiv:2606.21666) — evidence for scoping sync
  narrowly rather than broadcasting.
