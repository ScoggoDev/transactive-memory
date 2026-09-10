# transactive-memory

A Hermes Agent skill implementing a **Transactive Memory Protocol**: real
functional/informational continuity for an agent identity across its own
sessions and channels (TUI, WhatsApp, Telegram, multi-profile setups).

Idea and framing by **Lucas Scognamiglio**.

## Install

```bash
hermes skills tap add ScoggoDev/transactive-memory
hermes skills search transactive
hermes skills install ScoggoDev/transactive-memory/transactive-memory
```

Or install directly without adding the tap:

```bash
hermes skills install ScoggoDev/transactive-memory/skills/transactive-memory
```

## What it solves

Each channel/session of a Hermes agent is an isolated process — ask your
WhatsApp session about something you told the TUI session five minutes ago
and it has no idea, even though it's "the same agent" to the user. This
skill fixes that with infrastructure (a `pre_llm_call` hook diffing session
databases), not a passive "please remember to check" instruction — which
was tested and found unreliable on its own.

See [skills/transactive-memory/SKILL.md](skills/transactive-memory/) for
the full concept (grounded in Wegner's 1985 transactive memory theory),
setup, verification steps, and the research-backed reasoning for why this
is scoped to one agent identity's own sessions rather than a broadcast
between different agents.
