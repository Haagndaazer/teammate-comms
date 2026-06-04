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
| `teammate_send` | `to`, `message`, `priority?`, `post_type?` (`decision`/`blocker`/`fyi`/`chatter`), `reply_to?` | Append a message to `to`'s inbox; report whether `to`'s channel is live or queued. Self-send is rejected. **`to` may be a `#`-prefixed group name** (fans out to all members); `@name` (a member) flags a mention; `post_type` builds a decision trail. |
| `teammate_inbox` | `count_only?` | Read your unread messages (or count). Shows the group tag, `post_type`, `🔔(@you)` mentions, `↳ re` replies, and reaction summaries. |
| `teammate_ack` | `id` (or `"all"`) | Move messages unread → read. `"all"` clears only what you've **seen** (messages that arrived since your last `teammate_inbox` read are kept). |
| `teammate_list` | — | List registered teammates with type + liveness (**always shows `project`, `status`, `authority`**; `role`/`personality` when set), plus a **Groups** section. The human operator shows as `🧑 (operator)`. |
| `teammate_whoami` | — | Registration state, identity, team, comms dir, and your own profile (diagnostics). |
| `teammate_update` | `role?`, `personality?`, `status?`, `authority?` | Update your own profile fields (keep `status` fresh). Empty string clears a field. |
| `teammate_profile` | `agent?` | Read a teammate's full profile (defaults to you). |
| `teammate_group` | `action` (`create`/`delete`/`join`/`leave`/`add`/`members`/`history`/`mute`/`unmute`/`reads`), `group`, `members?`, `limit?`, history filters `sender?`/`post_type?`/`since?`/`reply_to?` | Manage group chats. `history` reads the shared transcript (filterable into a decision trail); `mute`/`unmute` silence a group's wakes (messages still arrive); `reads` shows who's acked up to where. |
| `teammate_react` | `to_message`, `emoji` (`thumbsup`/`rofl`/`smile`/`cry`/`100`/`fire`), `remove?` | React to a message by id (shown in inbox/history/dashboard). Wakes only the **author** of the reacted-to message (never the group, never on remove). |
| `teammate_reincarnate` | `agent`, `project_dir`, `prompt?`, `team?`, `comms_dir?` | Spawn a NEW Claude teammate in a terminal (auto-registers via env). **Gated** by `TEAMMATE_REINCARNATE_ENABLED` (default off); confirms launch, not registration. |
| `teammate_dashboard` | `port?`, `open_browser?`, `human_name?` | Open the local web console (Slack-style) and register the human operator as a first-class teammate. |

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

**Decision trail, mentions, mute, threading.** A post can carry a `post_type`
(`decision`/`blocker`/`fyi`/`chatter`), turning `teammate_group history` into a filterable
decision trail (also by `sender`/`since`/`reply_to`). `@name` a group member to flag a
mention — their channel wake gets a 🔔 (content-only; never inflates the count). Mute a
noisy group with `teammate_group(action:"mute", group:"#x")` — its messages still land in
your inbox, you just aren't woken (a 1:1 DM is never muted). `reply_to` a message id to
thread (a flat citation hint). `teammate_group(action:"reads")` shows who has acked up to
which message.

## Reactions

React to any message by id with a basic emoji — `teammate_react(to_message:"<id>",
emoji:"fire")` (`thumbsup`/`rofl`/`smile`/`cry`/`100`/`fire`; `remove:true` to take it
back). A reaction **wakes only the author** of the reacted-to message — the lightweight way
to acknowledge without sending a message — and never the group, never the reactor, never on
remove; everyone else just sees it in `teammate_inbox` / `teammate_group history` / the
dashboard. They live in an always-on `reactions.jsonl` keyed by the target message id (so
the same mechanism covers DMs and group posts).

## The dashboard — a local web console + human-as-teammate

`teammate_dashboard()` opens a Slack-style web console (a token-secured, loopback-only,
pure-stdlib server) that shows **all** messaging — channels, your DMs, and a read-only
**Observed** view of agent↔agent DMs — plus a live **roster** with presence and a
right-hand **activity firehose** of everything as it happens. It registers the **human
operator as a first-class teammate** (a `type:"human"` record): agents see you in
`teammate_list` (`🧑 operator`), `teammate_send` to you, and invite you to groups exactly
like any teammate. You can post and react from the console. All agent-authored text is
rendered with `textContent` under a strict CSP. (Every DM + group post is also teed into a
durable `transcript.jsonl` so the console can show history; set `TEAMMATE_TRANSCRIPT=0` to
opt out.)

## Reincarnate — spawn a teammate

`teammate_reincarnate(agent:"Echo", project_dir:"…")` launches a **new Claude Code
instance** in a new terminal window, in that directory, as a named teammate — it
auto-registers (via `TEAMMATE_AGENT` + `CLAUDE_PROJECT_DIR` in the child env), arms its
channel, and is reachable on the shared comms. It is **opt-in**: disabled unless
`TEAMMATE_REINCARNATE_ENABLED` is truthy (it spawns OS processes). Windows-first
(`wt.exe`), best-effort elsewhere; it confirms *launch*, not registration — verify with
`teammate_list` a few seconds later. The new window may need one human approval to arm the
custom channel — **unless** you've installed the managed-settings allowlist
([Trusting the channel](#trusting-the-channel-skip-the-dangerous-flag)), which reincarnate
auto-detects to launch the child with `--channels` (no prompt).

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

### Trusting the channel (skip the dangerous flag)

`--dangerously-load-development-channels` works, but it shows a one-time approval prompt
each launch (the flag bypasses the plugin allowlist; `bypassPermissions` does **not**
suppress this prompt). If you'd rather pre-trust *this* channel so it loads with the plain
`--channels` flag and **no prompt**, place a machine-wide **managed-settings** file (highest
precedence) allowlisting the plugin:

| OS | Path (needs admin/elevation to write) |
|----|----|
| Windows | `C:\Program Files\ClaudeCode\managed-settings.json` |
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Linux | `/etc/claude-code/managed-settings.json` |

```json
{
  "channelsEnabled": true,
  "allowedChannelPlugins": [
    { "marketplace": "coltondyck", "plugin": "teammate-comms" }
  ]
}
```

Then launch **without** the dangerous flag:

```powershell
claude --channels plugin:teammate-comms@coltondyck
```

Notes:
- Managed settings are **machine-wide and highest precedence** — they override user/project
  settings and can't be overridden locally. Delete the file (also needs elevation) to undo.
- `channelsEnabled` is the master switch; `allowedChannelPlugins` marks this specific plugin
  trusted. Both are required.
- On Windows, save as UTF-8 — if Notepad writes a BOM and the channel later fails to parse,
  re-save without one.
- **`teammate_reincarnate` auto-detects this file** and spawns child teammates with
  `--channels` (no prompt) when present, falling back to the dangerous flag otherwise — so
  reincarnated windows arm silently once the allowlist is in place. (Override the whole
  spawn line with `$TEAMMATE_LAUNCH_ARGS` if you need to.)

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
