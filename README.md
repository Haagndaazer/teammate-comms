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
| `teammate_register` | `agent`, `team?`, `comms_dir?`, *profile?* (`role`, `personality`, `status`, `authority`) | Call once at session start to establish identity, register your inbox, and arm the channel. Optionally set your profile. |
| `teammate_send` | `to`, `message`, `priority?` | Append a message to `to`'s inbox; report whether `to`'s channel is live or queued. Self-send is rejected. |
| `teammate_inbox` | `count_only?` | Read your unread messages (or count). |
| `teammate_ack` | `id` (or `"all"`) | Move messages unread → read. |
| `teammate_list` | — | List registered teammates with type + liveness; **always shows each teammate's `status` and `authority`** (plus `role`/`personality` when set). |
| `teammate_whoami` | — | Registration state, identity, team, comms dir, and your own profile (diagnostics). |
| `teammate_update` | `role?`, `personality?`, `status?`, `authority?` | Update your own profile fields (keep `status` fresh). Empty string clears a field. |
| `teammate_profile` | `agent?` | Read a teammate's full profile (defaults to you). |

Identity is established at runtime by `teammate_register` (the setup step) — it is
**not** baked into the MCP launch config. The other messaging tools return an error
until you register.

## Profiles

Each teammate can attach an optional profile to its registry record so peers can see
**what you're doing** and **what you own** at a glance — without messaging and
interrupting you:

- `role` — your job on the team (e.g. `backend / API`)
- `personality` — a short blurb (mostly for fun)
- `status` — what you're doing right now; **keep it fresh** with `teammate_update`
- `authority` — areas of the project you own (e.g. `src/auth/**, billing`), so
  teammates know before modifying them

Set any of these at `teammate_register` and update them anytime with
`teammate_update`. `teammate_list` always surfaces `status` + `authority` for every
teammate (and shows `role`/`personality` when set); `teammate_profile` returns the
full set. All fields are optional and single-line.

Your own profile is surfaced back to you so you stay in character: the
`teammate_register` return echoes it, and the channel wake event leads with
`You are <name>: <personality>` so a woken idle instance is reminded who it is. You
can always re-read it with `teammate_whoami` or `teammate_profile`.

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
claude --plugin-dir C:\Users\colto\Documents\Projects\teammate-comms --dangerously-load-development-channels plugin:teammate-comms@colton-comms
```

No env var is required. At session start, call `teammate_register(agent: "Grant")`
(add `team:` for namespaced inboxes) to establish identity and arm the channel.
Custom channels require `--dangerously-load-development-channels` — the flag only
bypasses the plugin allowlist; your org's `channelsEnabled` policy still applies.

**First install:** a SessionStart hook builds the (zero-dep) venv so the server
launches with `uv run --no-sync`. If the server isn't connected on the very first
session, **restart Claude Code once** — every session after is instant.

## Identity & storage

- **Identity** is set at runtime via `teammate_register(agent: …)` — once per
  session, like the old `setup.py` step. The channel arms on registration. As a
  shortcut, if `$TEAMMATE_AGENT` (and optionally `$TEAMMATE_TEAM`) is set in the
  environment, the server auto-registers with it at startup. Diagnostics (resolved
  identity, comms root, collisions) are logged to `~/.claude/debug/<session-id>.txt`.
- **Storage root** is resolved as the `comms_dir` passed to `teammate_register` →
  else `$TEAMMATE_COMMS_DIR` (cross-project/global) → else `$CLAUDE_PROJECT_DIR`
  (the project root Claude Code provides). Messages live at
  `<root>/TeammateComms/[<team>/]inboxes/`; two instances must share a root.
  `teammate_whoami` reports which won.

## Marketplace

This repo ships its own marketplace named **`colton-comms`** (distinct from the
`coltondyck` marketplace, which is registered to the **vibe-cognition** repo).
Because the names differ, there is no collision — you can register this repo as its
own marketplace without disturbing vibe-cognition:

```
/plugin marketplace add Haagndaazer/teammate-comms
/plugin install teammate-comms@colton-comms
```

Or, for local development, skip registration entirely and load straight from the
directory with `--plugin-dir` (see the launch command above).

> If you later prefer a single shared marketplace for both plugins, add a
> `teammate-comms` plugin entry to vibe-cognition's `coltondyck` `marketplace.json`
> (one marketplace can list many plugins, each pointing at its own repo) instead of
> shipping a second marketplace here.

## Development

```bash
uv run --no-dev python tests/test_handshake.py    # drives the server end-to-end
```
