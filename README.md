# teammate-comms

A Claude Code **plugin** bundling an **MCP server** that gives independent full
Claude Code instances agent-to-agent messaging plus **channel-based idle wake**.

Two full instances (each its own `claude` process a human started in a terminal)
cannot wake each other — the harness `SendMessage` nudge only works from a parent
agent to a subagent it spawned. teammate-comms closes that gap with a Claude Code
**channel**: an MCP server that watches its own inbox and pushes an event into a
*running, idle* session whenever a peer sends a message. A peer's `teammate_send`
*is* the nudge — no ports, no cross-instance addressing.

The server is **pure stdlib** (zero third-party runtime dependencies). It speaks
MCP over newline-delimited JSON-RPC on stdio directly: it serves the `teammate_*`
tools and pushes `notifications/claude/channel` events.

## Tools

| Tool | Args | Behavior |
|------|------|----------|
| `teammate_send` | `to`, `message`, `priority?` | Append a message to `to`'s inbox; report whether `to`'s channel is live or queued. Self-send is rejected. |
| `teammate_inbox` | `count_only?` | Read your unread messages (or count). |
| `teammate_ack` | `id` (or `"all"`) | Move messages unread → read. |
| `teammate_list` | — | List registered teammates with type + liveness. |
| `teammate_whoami` | — | Resolved identity, team, comms dir (diagnostics). |

## Two wake regimes

- **Full instance** → woken by the **channel** here.
- **Spawned subagent** → woken by its lead's `SendMessage` (no independent session
  for a channel to inject into); it then calls `teammate_inbox`.

## Install & launch

Prerequisites: Claude Code **v2.1.80+**, [`uv`](https://docs.astral.sh/uv/), and
channels enabled (individual Pro/Max: on by default). On Windows the SessionStart
hook runs under `bash`, so **git-bash must be on PATH** (it is if you already run
other bash-hooked plugins).

### Local development (recommended)

`--plugin-dir` loads the plugin straight from this directory and bypasses
marketplace registration entirely:

```powershell
$env:TEAMMATE_AGENT = 'Grant'        # per-instance identity, set BEFORE launching
claude --plugin-dir C:\Users\colto\Documents\Projects\teammate-comms --dangerously-load-development-channels plugin:teammate-comms@coltondyck
```

Set `$env:TEAMMATE_TEAM` too if you want team-namespaced inboxes. Custom channels
require `--dangerously-load-development-channels` — the flag only bypasses the
plugin allowlist; your org's `channelsEnabled` policy still applies.

**First install:** a SessionStart hook builds the (zero-dep) venv so the server
launches with `uv run --no-sync`. If the server isn't connected on the very first
session, **restart Claude Code once** — every session after is instant.

## Identity & storage

- **Identity** (`TEAMMATE_AGENT`) is per-instance and read from the shell
  environment. If it is unset, the MCP channel will not connect — `/mcp` shows the
  server not-connected, and the resolved identity is logged to
  `~/.claude/debug/<session-id>.txt`.
- **Storage root** is resolved as `$TEAMMATE_COMMS_DIR` (explicit override, enables
  cross-project/global comms) → else `$CLAUDE_PROJECT_DIR` (the project root Claude
  Code provides to the spawned server). Messages live at
  `<root>/TeammateComms/[<team>/]inboxes/`. `teammate_whoami` reports which won.

## ⚠️ Marketplace note (read before publishing)

The `coltondyck` marketplace on this machine is already registered to the
**vibe-cognition** repo, and marketplaces are keyed by name. **Do not run
`/plugin marketplace add` against this repository** — it would re-point `coltondyck`
away from vibe-cognition and break that working install.

This repo ships a `coltondyck` `marketplace.json` for brand/intent only; it is not
the served marketplace. The durable fix for a working *published* install is to add
a `teammate-comms` plugin entry to **vibe-cognition's** `marketplace.json` (one
marketplace repo hosting both plugins). Until then, use `--plugin-dir` for local use.

## Development

```bash
uv run --no-dev python tests/test_handshake.py    # drives the server end-to-end
```
