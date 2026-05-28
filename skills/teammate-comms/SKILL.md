---
description: Agent-to-agent messaging for Claude Code. Use the teammate_* MCP tools to send/read messages between teammates. Full instances are woken by the teammate-comms channel; spawned subagents are woken by their lead's SendMessage.
---

# Teammate Comms

File-backed messaging between Claude Code agents, plus a **channel** that wakes an
idle *full instance* the moment a teammate sends it a message. Provided as MCP
tools by the bundled `teammate-comms` server — call the tools; do not shell out.

## Tools

| Tool | Args | Behavior |
|------|------|----------|
| `teammate_register` | `agent`, `team?`, `comms_dir?`, *profile?* (`project`, `role`, `personality`, `status`, `authority`) | **Call once at session start.** Establishes your identity, registers your inbox, and arms the channel that wakes you. Optionally set your profile (`project` is auto-filled). The other messaging tools error until you do this. |
| `teammate_send` | `to`, `message`, `priority?` (`normal`\|`urgent`) | Append a message to `to`'s inbox. Reports whether `to`'s channel is live (auto-nudge) or the message is queued. `from` is your registered identity; sending to yourself is rejected. |
| `teammate_inbox` | `count_only?` | Read *your* unread messages (or just the count). |
| `teammate_ack` | `id` (a message id, or `"all"`) | Move message(s) from unread → read. |
| `teammate_list` | — | List registered teammates with type + liveness; **always shows each teammate's `project`, `status`, and `authority`** (plus `role`/`personality` when set). |
| `teammate_whoami` | — | Your registration state, identity, team, comms dir, and your own profile (diagnostics). |
| `teammate_update` | `role?`, `personality?`, `status?`, `authority?` | Update your own profile (keep `status` fresh as you switch tasks). Empty string clears a field. |
| `teammate_profile` | `agent?` | Read a teammate's full profile (defaults to you). |

**Startup protocol:** as soon as the session begins, call
`teammate_register(agent: "<your-name>")` (the human/lead tells you your name) —
optionally with a profile (`role`, `personality`, `status`, `authority`) — then
`teammate_inbox` to drain anything that queued while you were down. After that the
channel wakes you for new arrivals — no polling loop.

**Profiles — at-a-glance coordination.** Set a profile so peers can see what you're
doing and what you own without interrupting you. Keep `status` current with
`teammate_update` as you move between tasks, and set `authority` to the parts of the
project you own. Before modifying an area, check `teammate_list` (always shows
`project` + `status` + `authority`) or `teammate_profile(agent)` to see if a teammate
owns it or is mid-task there. Because comms are global across projects, `project`
(auto-filled from your project dir) tells peers which repo each teammate is in. Your own profile is echoed back in the `teammate_register` return,
and the channel wake event leads with `You are <name>: <personality>` so a woken idle
instance is reminded who it is.

`personality` is a *persona to inhabit* (write a person — concrete detail, a
temperament, voice cues — never the job/owned-areas/current-task; those are
role/authority/status); see the `teammate_register` tool description for the full
guide. Profile fields are **durable**: re-registering later only re-establishes your
identity and channel — your `role`/`personality`/`authority` persist, so you don't
re-supply them (refresh the dynamic `status` with `teammate_update`).

> Tool names may appear in the model surface with an MCP prefix
> (`mcp__plugin_teammate-comms_teammate-comms__teammate_inbox`). Refer to them by
> the short names above; they resolve to the same tools.

## Two wake regimes — pick by process topology

- **Full instance** (its own `claude` process a human started): woken by the
  **teammate-comms channel**. A peer's `teammate_send` writes your inbox and the
  channel pushes a `notifications/claude/channel` event into your live session —
  even while idle. No polling loop. On startup, call `teammate_inbox` once to drain
  anything that arrived while you were down.
- **Spawned subagent** (a lead invoked it via the Agent/Task tool): has no
  independent session for a channel to inject into. Its lead wakes it with
  `SendMessage` ("check your inbox"); the subagent then calls `teammate_inbox`.

## Reliability contract

- The inbox JSON is the **source of truth**. A dropped/missed channel push never
  loses a message — it is read on the next `teammate_inbox`.
- `teammate_send` **warns** when the recipient's channel is offline (message
  queued, seen on their next start) — never a silent no-op.
- Exactly **one live channel per agent name per machine**. Launching two instances
  with the same `TEAMMATE_AGENT` makes both bind the same inbox; the server logs a
  loud stderr collision warning.
- Diagnostics (resolved identity, comms root, warnings) go to stderr →
  `~/.claude/debug/<session-id>.txt`. Check `/mcp` for connection status.

## Launching a full instance (channel)

No identity env var is required — launch with the channel flag, then register from
inside the session:

```powershell
claude --plugin-dir C:\Users\colto\Documents\Projects\teammate-comms --dangerously-load-development-channels plugin:teammate-comms@colton-comms
```

Then, at session start, call `teammate_register(agent: "Grant")` (add `team:` if
using team-namespaced inboxes). The channel arms on registration.

Prerequisites: Claude Code **v2.1.80+**, `uv` installed, channels enabled
(individual Pro/Max: on by default). Custom channels require
`--dangerously-load-development-channels` (they are not on Anthropic's allowlist;
the flag only bypasses that allowlist — org `channelsEnabled` policy still applies).

Storage lives at `<comms-root>/TeammateComms/[<team>/]inboxes/`. The comms root is
**global by default** — `comms_dir` passed to `teammate_register`, else
`$TEAMMATE_COMMS_DIR`, else `$CLAUDE_CONFIG_DIR`, else `~/.claude` — so agents across
different projects share one space and can message each other. For per-project
isolation, set `$TEAMMATE_COMMS_DIR` (or pass `comms_dir`) to the project dir. Two
instances must share the same root to message each other; `teammate_whoami` reports
the resolved root.

> Power-user shortcut: if `$TEAMMATE_AGENT` (and optionally `$TEAMMATE_TEAM`) is set
> in the environment, the server auto-registers with it at startup — no explicit
> `teammate_register` call needed.
