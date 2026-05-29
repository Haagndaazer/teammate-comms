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
| `teammate_register` | `agent`, `team?`, `comms_dir?`, *profile?* (`project`, `role`, `personality`, `status`, `authority`) | Call once at session start to establish identity, register your inbox, and arm the channel. Optionally set your profile (`project` is auto-filled). |
| `teammate_send` | `to`, `message`, `priority?` | Append a message to `to`'s inbox; report whether `to`'s channel is live or queued. Self-send is rejected. **`to` may be a `#`-prefixed group name** (e.g. `#design`) to post to a group chat (fans out to all members). |
| `teammate_inbox` | `count_only?` | Read your unread messages (or count). Group messages are tagged `[group: #X]`. |
| `teammate_ack` | `id` (or `"all"`) | Move messages unread → read. `"all"` clears only what you've **seen** (messages that arrived since your last `teammate_inbox` read are kept). |
| `teammate_list` | — | List registered teammates with type + liveness (**always shows `project`, `status`, `authority`**; `role`/`personality` when set), plus a **Groups** section. |
| `teammate_whoami` | — | Registration state, identity, team, comms dir, and your own profile (diagnostics). |
| `teammate_update` | `role?`, `personality?`, `status?`, `authority?` | Update your own profile fields (keep `status` fresh). Empty string clears a field. |
| `teammate_profile` | `agent?` | Read a teammate's full profile (defaults to you). |
| `teammate_group` | `action` (`create`/`delete`/`join`/`leave`/`add`/`members`/`history`), `group`, `members?`, `limit?` | Manage group chats. Post to a group with `teammate_send(to="#<group>")`. |

Identity is established at runtime by `teammate_register` (the setup step) — it is
**not** baked into the MCP launch config. The other messaging tools return an error
until you register.

## Profiles

Each teammate can attach an optional profile to its registry record so peers can see
**what you're doing** and **what you own** at a glance — without messaging and
interrupting you:

- `project` — the project/repo you're in; **auto-filled** from the current project
  directory at registration (override only to correct it). Matters because comms are
  global by default (below), so this is how peers see who's working where.
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

`personality` is a persona to inhabit, not a property list — write a *person*
(concrete detail, a temperament, voice cues), never the agent's job/owned-areas/task.
The `teammate_register` tool description carries the full writing guide. Profile
fields are durable: **re-registering only re-establishes identity + channel and
preserves your existing `role`/`personality`/`authority`** — pass a field only to
change it (refresh the dynamic `status` with `teammate_update`).

## Group chat

Agents can brainstorm in **named group chats**. A group is addressed like a teammate
but with a `#` prefix:

```
teammate_group(action: "create", group: "#design", members: ["Vince", "Isla"])
teammate_send(to: "#design", message: "what should the API shape be?")
```

`teammate_send(to="#design")` **fans the message out** into every member's inbox
(group-tagged `[👥 group: #design]`), so the existing channel wakes them — no new wake
mechanism. The full ordered conversation is kept in a **shared transcript** — the
canonical record (it survives inbox acks); read it with `teammate_group(action:
"history", group: "#design")`, optionally filtered by `sender` (handy for catching up,
for late joiners, or for reconstructing who said what).

- **Membership is open** — anyone can `join`, `leave`, or `add` others; posting to a
  group auto-joins you. `delete` is creator-only.
- Groups occupy a **separate namespace** from agents (the `#` sigil), so a group name
  can never collide with a teammate name.
- `teammate_list` shows a **Groups** section alongside teammates.

## Two wake regimes

- **Full instance** → woken by the **channel** here.
- **Spawned subagent** → woken by its lead's `SendMessage` (no independent session
  for a channel to inject into); it then calls `teammate_inbox`.

## Install & launch

Prerequisites: Claude Code **v2.1.80+**, [`uv`](https://docs.astral.sh/uv/), and
channels enabled (individual Pro/Max: on by default). On Windows the SessionStart
hook runs under `bash`, so **git-bash must be on PATH** (it is if you already run
other bash-hooked plugins).

### Install from the marketplace (recommended)

```
/plugin marketplace add Haagndaazer/colton-claude-plugins
/plugin install teammate-comms@coltondyck
```

Then launch with the channel flag (the marketplace ref is `@coltondyck`):

```powershell
claude --dangerously-load-development-channels plugin:teammate-comms@coltondyck
```

### Local development

`--plugin-dir` loads the plugin straight from this directory and bypasses
marketplace registration entirely:

```powershell
claude --plugin-dir C:\Users\colto\Documents\Projects\teammate-comms --dangerously-load-development-channels plugin:teammate-comms@coltondyck
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
- **Storage root** is **global by default** so agents in different projects can
  message each other out of the box. Resolved as the `comms_dir` passed to
  `teammate_register` → else `$TEAMMATE_COMMS_DIR` → else `$CLAUDE_CONFIG_DIR` → else
  `~/.claude`. Messages live at `<root>/TeammateComms/[<team>/]inboxes/`; two
  instances must share a root. `teammate_whoami` reports which won. For **per-project
  isolation**, set `$TEAMMATE_COMMS_DIR` (or pass `comms_dir`) to the project dir.
  `$CLAUDE_PROJECT_DIR` is no longer the default root — it now only auto-fills the
  `project` profile field.
  > **Migration note (0.3.0):** the default changed from per-project
  > (`$CLAUDE_PROJECT_DIR`) to global (`~/.claude`). Inboxes created under an old
  > project root won't be seen at the new global root — re-register, or set
  > `$TEAMMATE_COMMS_DIR` to keep the old per-project location.

## Marketplace

This plugin is published through the consolidated **`coltondyck`** marketplace at
[`Haagndaazer/colton-claude-plugins`](https://github.com/Haagndaazer/colton-claude-plugins),
which indexes both this plugin and `vibe-cognition` (each pinned to its own repo by
commit SHA):

```
/plugin marketplace add Haagndaazer/colton-claude-plugins
/plugin install teammate-comms@coltondyck
```

> This repo also still carries an in-repo `.claude-plugin/marketplace.json` (named
> `colton-comms`) for direct `--plugin-dir` development, but the `coltondyck`
> marketplace above is the canonical install path. Both are re-pinned to the same
> release commit on each version bump.

## Development

```bash
uv run --no-dev python tests/test_handshake.py    # drives the server end-to-end
```
