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
| `teammate_send` | `to`, `message`, `priority?` (`normal`\|`urgent`) | Append a message to `to`'s inbox. Reports whether `to`'s channel is live (auto-nudge) or the message is queued. `from` is your own identity; sending to yourself is rejected. |
| `teammate_inbox` | `count_only?` | Read *your* unread messages (or just the count). |
| `teammate_ack` | `id` (a message id, or `"all"`) | Move message(s) from unread → read. |
| `teammate_list` | — | List registered teammates with type + liveness. |
| `teammate_whoami` | — | Your resolved identity, team, and comms dir (diagnostics). |

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

## Launching a full instance (identity + channel)

Identity is per-instance, set in the shell **before** launching `claude`:

```powershell
$env:TEAMMATE_AGENT = 'Grant'      # and $env:TEAMMATE_TEAM = '<team>' if using teams
claude --plugin-dir C:\Users\colto\Documents\Projects\teammate-comms --dangerously-load-development-channels plugin:teammate-comms@coltondyck
```

Prerequisites: Claude Code **v2.1.80+**, `uv` installed, channels enabled
(individual Pro/Max: on by default). Custom channels require
`--dangerously-load-development-channels` (they are not on Anthropic's allowlist;
the flag only bypasses that allowlist — org `channelsEnabled` policy still applies).

If `/mcp` shows the server not-connected, `TEAMMATE_AGENT` is almost certainly
unset/empty — set it in the shell and relaunch. Storage location: messages live in
`<project>/TeammateComms/[<team>/]inboxes/` (or under `$TEAMMATE_COMMS_DIR` if set).
