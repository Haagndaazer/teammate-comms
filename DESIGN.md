# teammate-comms — Design (as-built)

> A Claude Code **plugin** bundling an **MCP server** that gives independent full
> Claude Code instances agent-to-agent messaging plus **channel-based idle wake**.
> **Pure-stdlib** Python (zero runtime dependencies), shipped as a marketplace plugin.

This document began as a pre-build blueprint; it has been **reconciled to the
as-built implementation (v0.3.1)**. Where a design decision reversed during the
build, an *“Originally planned … shipped … because …”* note preserves the lineage —
the rationale is the valuable part, even when the choice flipped.

---

## 1. Purpose & lineage

Two **independent full Claude Code instances** (each started by a human in its own
terminal) cannot wake each other: the harness `SendMessage` nudge only works from a
parent agent to a subagent it spawned. teammate-comms closes that gap with a Claude
Code **channel** — an MCP server that pushes an event into a *running* session, even
while it sits idle waiting for its human.

This generalizes a prototype validated inside the `TestSVN` repo
(`.claude/skills/teammate-comms/scripts/channel_server.py` + `common.py`), which
proved the core mechanic end-to-end: a one-way channel server watches its own
agent's inbox file and emits `notifications/claude/channel` when new messages
arrive, so a peer's `send` *is* the nudge — no ports, no cross-instance addressing.

### Wake regimes (pick by process topology, not by "team")
- **Full instance** (its own `claude` process): woken by the **channel** here.
- **Spawned subagent** (a lead invoked it via the Agent/Task tool): woken by the
  parent's `SendMessage`. Channels do not apply — a spawned subagent has no
  independent session for a channel to inject into.

---

## 2. Repo layout (as-built)

```
teammate-comms/
├── .claude-plugin/
│   ├── plugin.json             # inline mcpServers + channels (§4)
│   └── marketplace.json        # in-repo manifest, name "colton-comms" (local dev; §4b)
├── pyproject.toml              # hatchling; dependencies = [] (pure stdlib)
├── uv.lock                     # COMMITTED — reproducible installs (§3)
├── src/teammate_comms/
│   ├── __init__.py             # __version__ (synced with plugin.json + pyproject)
│   ├── server.py               # stdlib JSON-RPC server: tools + channel (§6)
│   ├── comms.py                # storage / registry / liveness / transcript (§8)
│   ├── channel.py              # background inbox watcher + push (§7)
│   ├── tools.py                # MCP tool definitions + handlers (§9)
│   ├── spawn.py                # teammate_reincarnate launcher (argv/env builders + spawn)
│   ├── dashboard.py            # stdlib web console server (§9, teammate_dashboard)
│   └── static/index.html       # single-file Slack-style UI (inline CSS/JS, no CDN)
├── hooks/
│   ├── hooks.json
│   └── session-start.sh        # builds the venv before the server spawns
├── skills/teammate-comms/SKILL.md
├── tests/test_handshake.py     # end-to-end server test (handshake + tools + channel)
├── README.md
└── .gitignore
```

No `cli.py` — the optional CLI-parity scripts in the original blueprint were never
built. No `session-start.ps1` — the SessionStart hook is **bash-only**, so on Windows
git-bash must be on PATH (it is if you already run other bash-hooked plugins).

---

## 3. Dependency install & MCP wiring

The server is **pure stdlib** (`dependencies = []`), so there is no third-party tree
to resolve. The install pattern still mirrors vibe-cognition for consistency and a
fast, predictable spawn:

- **Ship a committed `uv.lock`**.
- A **SessionStart hook** (`hooks/session-start.sh`, registered in `hooks/hooks.json`
  via `${CLAUDE_PLUGIN_ROOT}`) runs `uv sync` against a stamp so the venv exists
  *before* the server is spawned. The hook runs under `bash` (git-bash on Windows).
- The MCP server is launched with **`uv run --no-sync`** (venv already present).
- **First-session UX:** on a fresh install the hook builds the venv and the server
  connects after a **restart** — identical to vibe-cognition. Documented in the README.

`mcpServers` is declared **inline in `plugin.json`** with `${CLAUDE_PLUGIN_ROOT}`, so
the plugin is self-contained and does not write into a project's `.mcp.json`.

> *Originally planned:* a `mcp>=1.27` SDK dependency (a non-trivial tree to sync).
> *Shipped:* zero deps (see §6) — the venv is near-empty, which keeps the spawn
> instant and removes the lockfile-resolve hazard that motivated this section.

---

## 4. `.claude-plugin/plugin.json` (as-built)

```json
{
  "name": "teammate-comms",
  "version": "0.3.1",
  "description": "Agent-to-agent messaging with channel-based idle wake for full Claude Code instances.",
  "author": { "name": "ColtonDyck" },
  "license": "MIT",
  "repository": "https://github.com/Haagndaazer/teammate-comms",
  "keywords": ["mcp", "channel", "agent", "messaging"],
  "mcpServers": {
    "teammate-comms": {
      "command": "uv",
      "args": ["run", "--no-sync", "--directory", "${CLAUDE_PLUGIN_ROOT}", "python", "-m", "teammate_comms.server"]
    }
  },
  "channels": [{ "server": "teammate-comms" }]
}
```

`version` MUST stay in sync with `pyproject.toml` **and** `src/teammate_comms/__init__.py`
— the handshake test asserts all three agree. The `channels` array marks the
`teammate-comms` MCP server as a channel so Claude Code registers the notification
listener.

> *Originally planned:* an `env` block expanding `${TEAMMATE_AGENT}` /
> `${TEAMMATE_TEAM}` / `${TEAMMATE_COMMS_DIR}`. *Shipped:* no `env` block — unset
> `${VAR}` refs made Claude Code reject the config ("Missing environment variables"),
> and identity moved to the `teammate_register` tool (§10). The server still *reads*
> those env vars when present (convenience auto-register), it just doesn't declare them.

---

## 4b. Marketplace — consolidated (resolved)

Both this plugin and `vibe-cognition` are published through **one** consolidated
marketplace named `coltondyck`, hosted in the index-only repo
**`Haagndaazer/colton-claude-plugins`**. It lists each plugin by `url`+pinned `sha`;
plugin code stays in each plugin's own repo. Install:

```
/plugin marketplace add Haagndaazer/colton-claude-plugins
/plugin install teammate-comms@coltondyck
```

This repo also carries an in-repo `.claude-plugin/marketplace.json` named
**`colton-comms`** (a *distinct* name, so it cannot collide with `coltondyck`) for
`--plugin-dir` local development. It is non-canonical but is re-pinned in parallel on
each release.

> *Originally a hazard:* a second marketplace **also** named `coltondyck` would
> collide — Claude Code keys marketplaces by name, so only one `coltondyck` can be
> registered at a time. *Resolved:* consolidate into `colton-claude-plugins` as the
> single source of truth; vibe-cognition's in-repo `coltondyck` manifest was deleted
> and this repo's in-repo manifest kept the distinct name `colton-comms`.

---

## 5. `pyproject.toml` (as-built)

- Build backend: `hatchling`.
- `name = "teammate-comms"`, `version` synced with `plugin.json` + `__init__.py`.
- `requires-python = ">=3.11"`.
- **`dependencies = []`** — pure stdlib, zero third-party runtime deps (deliberate:
  keeps the MCP spawn instant and avoids a dependency tree blocking the handshake).
- `[tool.hatch.build.targets.wheel] packages = ["src/teammate_comms"]`.
- `[project.scripts] teammate-comms = "teammate_comms.server:main"` (the server entry).
- `[dependency-groups] dev = ["pytest>=8.0.0"]`.
- Commit `uv.lock`.

> *Originally planned:* `dependencies = ["mcp>=1.27,<2"]` plus CLI-parity scripts
> (`teammate-send` / `-inbox` / `-ack` via a `cli.py`). *Shipped:* neither — no SDK,
> no `cli.py`; agents call the MCP tools directly.

---

## 6. `server.py` — stdlib JSON-RPC server (tools + channel)

One server is **both** a tool server and a channel.

**As-built:** a **pure-stdlib, newline-delimited JSON-RPC 2.0 server over stdio** — no
`mcp` SDK. The main thread reads stdin and dispatches `initialize`,
`notifications/initialized`, `ping`, `tools/list`, `tools/call` (an unknown method
with an id returns `-32601`; unknown notifications are ignored). A background
**daemon thread** (`channel.run_watcher`) heartbeats the registry and pushes
`notifications/claude/channel` once registered. Both the main thread and the watcher
write stdout under a **single lock**, so messages never interleave at the byte level.

- `initialize` advertises `capabilities.experimental['claude/channel'] = {}` plus
  `tools`, and echoes `protocolVersion` + `instructions`.
- The channel push is a **raw JSON-RPC notification** (`method:
  "notifications/claude/channel"`, `params: {content, meta}`) written directly to
  stdout under the lock.
- The server starts **identity-less**; `teammate_register` establishes identity and
  arms the watcher (§10).

**Getting a session for an unsolicited push** (the original "riskiest piece"): because
the stdlib server owns its own stdio loop, pushing while the agent is idle is trivial
— the watcher thread just writes to stdout under the lock. Pushes are **gated** on
`notifications/initialized` AND registration, and the unread **baseline is seeded** at
that moment so pre-existing messages don't trigger a spurious nudge.

> *Originally planned:* an `mcp`-SDK low-level `Server` owning a `ServerSession`, with
> the watcher as an **anyio task** in the SDK's event loop, and pure stdlib as a
> *"documented fallback."* *Shipped:* the stdlib path. It sidesteps the hard problem
> the blueprint flagged — the SDK's `request_context.session` is only populated
> *during* a request handler, exactly when an idle channel can't use it — and keeps
> the dependency tree empty.

**Windows stdio:** each message is one **BOM-free UTF-8 line + `\n`** written under the
stdout lock (no CRLF, no cp1252); stdin is decoded with `utf-8-sig` to tolerate a
leading BOM. No handler may `print` to stdout (that's the protocol stream);
diagnostics go to stderr → `~/.claude/debug/<session-id>.txt`.

---

## 7. `channel.py` — wake mechanics

A background **daemon thread** (started with the server) that stays dormant until two
gates open: `notifications/initialized` AND registration. Once armed it:
- Polls `<self>_unread.json` every ~0.5s via a **non-destructive read** (never
  rewrites the file on a partial/corrupt read — that would destroy a message
  mid-delivery).
- **Nudge gating (v0.4.2):** nudges only for messages the agent hasn't been shown —
  an unread id that is neither in `Identity.last_seen` (ids returned by the last full
  `teammate_inbox`) nor in a watcher-local `known_ids` set (seeded to the inbox
  contents at registration so pre-existing messages don't nudge, then accumulating
  what's already been nudged). The emitted `count` is the number of *unseen* unread
  messages, so a read-but-unacked message never pads it. Reading (not acking) silences
  a nudge. Missed-nudge-safe: a genuinely new message has a fresh id in neither set, so
  it always nudges. (Replaced the earlier integer `baseline` count, which re-nudged on
  every count rise and counted read-but-unacked messages — the v0.4.1-test noise.)
- Emits `notifications/claude/channel` with `meta = {count, agent}` and content that
  references the MCP tools. If the agent has a `personality` set, the content **leads
  with `You are <name>: <personality>`** so a woken idle instance is reminded who it
  is (personality is read from the registry only at nudge time, not every poll).
- **Group reply target (v0.4.3, broadened v0.4.4):** when there's any **unseen**
  (unread, not-yet-read) group message, the content **names the group reply target** —
  *"reply to the group with `teammate_send to:'#<group>'`"* (distinct unseen groups,
  `sorted`) — so a woken agent replies to the group, not 1:1 to the sender (which would
  silently fracture the thread). v0.4.4 computes this from `unread − last_seen` (not just
  the messages that triggered the wake), so a DM-triggered wake still surfaces a pending
  group thread (the mixed-batch case). 1:1-only wakes keep the generic text.
- Heartbeats the agent's registry record every ~5s.

Dropped pushes (session closed) never lose a message — the inbox JSON is the source of
truth and is drained on the next `teammate_inbox`.

---

## 8. `comms.py` — storage, registry, liveness

Stdlib helpers, parameterized by the resolved comms root: `get_inboxes_dir`,
`get_agents_dir`, `validate_agent_name` (+ `AGENT_NAME_PATTERN`),
`validate_profile_field` (+ `PROFILE_FIELDS`), `read_json_readonly` (non-destructive),
`write_json_atomic` (`os.replace`), `file_lock` / `file_lock_optional` (cross-platform
`mkdir`-based locks), `write_agent_record` / `read_agent_record` (field-level merge
under a non-fatal lock), `is_channel_alive` (same-host pid check, else heartbeat
freshness), and a single pinned timestamp format.

**Comms-root resolution.** A plugin-spawned server's **cwd is the plugin cache
directory** (itself a git repo), so cwd/git-based resolution would scatter inboxes
into the cache. Resolve in this order, first hit wins:
1. `comms_dir` arg passed to `teammate_register`.
2. `$TEAMMATE_COMMS_DIR` (explicit override — use for per-project isolation).
3. `$CLAUDE_CONFIG_DIR` (the user's Claude config dir, if relocated).
4. `~/.claude` (the default). Comms live at `<root>/TeammateComms/[<team>/]…`.

**Global by default (0.3.0).** The default root is the user config dir (`~/.claude`),
NOT the project dir — so every agent on the machine shares one comms space and agents
in different projects can message each other out of the box. `$CLAUDE_PROJECT_DIR` is
no longer the default root (that isolated agents per repo); it now only auto-fills the
`project` profile field at registration. Tradeoff: a flat global namespace means agent
inbox names must be unique across all projects (two repos each registering `lead`
collide on one inbox — the server's same-name collision warning still applies); team
namespacing carves out subsets.

---

## 9. MCP tools

Agents call tools instead of shelling out. `from` is implicit (the server's own
resolved identity). `to` is validated with `validate_agent_name`. The dispatcher
converts `CommsError` → an `isError` result so a single bad call never tears down the
long-lived server. **12 tools:**

| Tool | Args | Behavior |
|------|------|----------|
| `teammate_register` | `agent`, `team?`, `comms_dir?`, profile? (`project`/`role`/`personality`/`status`/`authority`) | Establish identity, register the inbox, arm the channel. Optionally set a profile (`project` is auto-filled). Re-registering only re-establishes identity + channel and **preserves** the existing profile. |
| `teammate_send` | `to`, `message`, `priority?` (`normal`\|`urgent`), `post_type?` (`decision`/`blocker`/`fyi`/`chatter`), `reply_to?` | Append a message to `to`'s inbox (atomic write). Report whether `to`'s channel is live (auto-nudge) or offline (queued). Self-send rejected. **A `#`-prefixed `to` posts to a group** (fan-out); `@name` tokens to group members become `mentions`. |
| `teammate_inbox` | `count_only?` | Read this agent's unread messages (or just the count). Shows group tag, `post_type`, `🔔(@you)`, `↳ re`, and reaction summaries. |
| `teammate_ack` | `id` (or `"all"`) | Move a message from unread → read. |
| `teammate_list` | — | List registered agents with type + liveness (**always shows `project`, `status`, `authority`**; `role`/`personality` when set), plus a Groups section. Humans show `🧑 (operator)` + `presence`. |
| `teammate_whoami` | — | Resolved identity, team, comms dir, and own profile (diagnostics). |
| `teammate_update` | `project?`/`role?`/`personality?`/`status?`/`authority?` | Update own profile fields (self-only field-merge; empty string clears a field). |
| `teammate_profile` | `agent?` | Read a teammate's full profile (defaults to self). |
| `teammate_group` | `action` (`create`/`delete`/`join`/`leave`/`add`/`members`/`history`/`mute`/`unmute`/`reads`), `group`, `members?`, `limit?`, `sender?`/`post_type?`/`since?`/`reply_to?` (history filters) | Manage group chats (see below) + mute/unmute a group's wakes + `reads` (read receipts). |
| `teammate_react` | `to_message`, `emoji` (`thumbsup`/`rofl`/`smile`/`cry`/`100`/`fire`), `remove?` | React to a message by id. Ambient — recorded in `reactions.jsonl`, shown in inbox/history/dashboard, **never wakes anyone**. |
| `teammate_reincarnate` | `agent`, `project_dir`, `prompt?`, `team?`, `comms_dir?` | Spawn a NEW Claude instance in a terminal as a named teammate (auto-registers via env). **Gated** by `TEAMMATE_REINCARNATE_ENABLED`; confirms launch, not registration. |
| `teammate_dashboard` | `port?`, `open_browser?`, `human_name?` | Launch the local web console + register the human as a teammate (see below). |

Every tool's error text wraps the underlying cause with a one-line action sentence.

**Group chat (added 0.4.0).** Named group chats addressed with a `#` sigil
(`teammate_send(to="#design")`) — a separate namespace from agents, so names can't
collide. A group is a per-group subdir under `groups/`: `meta.json` (`name`, `members`,
`creator`, `createdAt`) + an append-only `messages.json` transcript (the canonical
ordered history; read via `teammate_group action=history`). Posting **fans the message
out** into each member's `_unread.json` (group-tagged), so the **existing channel wake
delivers it with no `channel.py` change** — the transcript is written first
(authoritative), then fan-out is best-effort (a locked member inbox is non-fatal; they
catch up via `history`). Membership is **open** (`join`/`leave`/`add`; posting
auto-joins the sender); `delete` is creator-only. Fan-out liveness uses
`pid_check=False` (no per-member `tasklist`); all transcript/meta reads use
`read_json_readonly` (never `read_json_safe`, which would reset a mid-write file to []).

**Dashboard + human-as-teammate (added 0.5.0-dev).** `teammate_dashboard` opens a
local **Slack-style web console** (`dashboard.py` — a pure-stdlib
`http.server.ThreadingHTTPServer` in a daemon thread inside the calling instance's
process) that shows all messaging (groups + DMs) and a live roster, and registers the
**human operator as a first-class teammate** (a `type:"human"` registry record with an
inbox — but **no `pid`/`channel`**, so it's never a wakeable channel and never trips the
register-time collision guard). Agents see the human in `teammate_list`/`teammate_profile`
marked `🧑 (operator)` with a `presence` (online/away) instead of `channel`, and can
`teammate_send` to them / invite them to groups by flat name like any teammate. The
console posts **as the human** by calling the **sender-explicit cores** `send_dm` /
`send_group` (extracted from `_handle_send` / `_send_to_group`; the tool handlers are now
thin wrappers), so a human message wakes live agents through the existing file-driven
watcher with **no `channel.py` change**.
- **Security:** loopback-only bind (`127.0.0.1`), a per-launch `secrets.token_urlsafe(32)`
  (query token on `/`, `X-Dashboard-Token` header on `/api/*`, `compare_digest`), a
  Host-header allowlist (DNS-rebind defense, missing Host rejected), and an
  XSS-safe frontend (agent-authored text rendered via `textContent` only + a strict CSP).
  The HTTP server writes only to its own sockets + **stderr — never stdout** (the
  JSON-RPC stream); a standing test asserts stdout stays pure JSON-RPC after a launch.
- **Live updates:** the page short-polls `/api/poll?cursor=` (~1.5s); message ids are the
  sortable `now_timestamp()`, so the cursor is a string compare.
- **Lifecycle:** idempotent **per process** (a second call returns the same URL; two
  instances = two consoles); the server dies when the instance exits — `server.py`'s stdio
  `finally` calls `dashboard.shutdown_dashboard()` (marks the human `away`, frees the port).

**Group polish + reactions + reincarnate (added 0.6.0).**
- **Typed posts:** an optional `post_type` (`decision`/`blocker`/`fyi`/`chatter`) on a
  message record (additive; named `post_type` to avoid colliding with the transcript's
  `kind` and an agent record's `type`). `teammate_group history` filters by
  `post_type`/`sender`/`since` (id cursor) + `reply_to` — the decision trail.
- **@mentions:** `@name` tokens (intersected with group members — no phantoms) become a
  shared `mentions` list on the one fan-out record; each member's OWN watcher checks
  `agent in mentions` and adds a 🔔 line to its wake (content-only, no per-member records,
  no count change).
- **Mute:** `muted_groups` on the member's agent record (the watcher already reads that
  record, cached on the 5s heartbeat). The watcher's `_audible` filter drops muted-group
  messages from the wake/count but keeps them in the inbox; `known_ids` still tracks the
  full unread set so an unmute never retro-nudges. A 1:1 DM (no `group` key) can never be
  muted.
- **Read receipts:** read-only inference — `group_read_positions` reads each member's
  `_read.json` for the max acked group-message id (no write path, no ack change). Surfaced
  via `teammate_group reads` + dashboard ticks. An ack/seen upper bound, groups-only.
- **Threading:** an optional `reply_to` id stored unvalidated (a citation hint), rendered
  flat (`↳ re <id>`); `history reply_to=<id>` pulls a thread. No send-path read.
- **Reactions:** `teammate_react` appends to an always-on (NOT transcript-gated) NDJSON
  `reactions.jsonl` keyed by target message id (covers DMs + groups without mutating the
  append-only records); `aggregate_reactions` folds add/remove. Ambient — the watcher
  ignores reactions, so a 👍 never wakes anyone.
- **Channel wake (changed):** the wake now names WHERE messages came from (DM senders +
  `#groups`); the personality reminder fires only every ~10 received messages (the
  registration return still echoes it), not every wake — cuts per-message token waste.
- **`teammate_reincarnate` + `spawn.py`:** spawns a new `claude` in a terminal with
  `TEAMMATE_AGENT` + `CLAUDE_PROJECT_DIR` in the child env (auto-register handoff).
  Default-off gate `TEAMMATE_REINCARNATE_ENABLED`; list-form exec only (the `prompt` is a
  single trailing argv element — no shell injection); child stdio = DEVNULL; Windows
  `wt.exe` primary / `CREATE_NEW_CONSOLE`+`BREAKAWAY_FROM_JOB` fallback; best-effort
  live-name collision guard (refuse only if already live). The dev channel flag stays in
  the default launch args (custom channel, not allowlisted) — overridable via
  `TEAMMATE_LAUNCH_ARGS`.
- **Dashboard upgrades:** dropped the redundant "Direct messages" section (Teammates is
  the DM entry + shows presence); added an "Observed (read-only)" section (agent↔agent
  DMs) directly under Teammates; a right-hand live activity firehose (FIFO, newly-seen
  records only, scroll-aware, last-200); reaction chips + a clickable emoji bar
  (`POST /api/react`, reactions sub-stream on `/api/poll`); read-receipt ticks; field
  chips for `post_type`/mentions/`reply_to`.

**Observability transcript (added 0.5.0-dev).** To let the console show *all* messaging
(1:1 DMs were previously ephemeral — only in `_unread.json`, gone after ack), both send
cores **tee** every message into one append-only **NDJSON** log
`TeammateComms/[<team>/]transcript.jsonl` (`{id,from,to?,group?,priority,message,kind}`,
`kind` ∈ `dm`/`group`). NDJSON append (O(1), one line) avoids the O(n²) full-rewrite and
whole-team lock serialization a JSON array would impose. The tee is **last and
best-effort** (a short never-raise lock; disabled by `TEAMMATE_TRANSCRIPT=0`) so
observability never precedes or delays the authoritative delivery write. **Privacy note:**
this durably records previously-ephemeral agent↔agent DMs under the (global-by-default)
comms root — intended for the operator overseeing their own agents, opt-out via the env
flag. Reads are tail-bounded (`read_transcript(limit=200)`) so a large log never floods
the browser.

**Profile fields (0.2.0; `project` added 0.3.0).** Stored as plain keys on the agent
registry record via `write_agent_record`'s field-level merge — additive,
backward-compatible, and they survive the 5s heartbeat (the test asserts this).
`validate_profile_field` collapses whitespace/newlines and length-caps per field.
`project` is auto-filled from `basename($CLAUDE_PROJECT_DIR)` at registration
(overridable), so peers see who is working where now that comms are global. An agent's
own profile is echoed in the `teammate_register` return, and the channel wake event
leads with `You are <name>: <personality>`, so it stays reminded of who it is across
waking.

---

## 10. Identity

**As-built:** identity is established at runtime by the **`teammate_register` tool**,
not baked into the launch config. The server starts identity-less; the other messaging
tools return `isError` until registration. `teammate_register` resolves the comms root,
registers the inbox + registry record, and arms the channel watcher.

**Convenience auto-register:** if `$TEAMMATE_AGENT` (and optionally `$TEAMMATE_TEAM`)
is set in the environment, the server auto-registers with it at startup — the
power-user shortcut.

**Diagnostics:** resolved identity, comms root, and a collision warning (if another
live server on this host already owns the same agent name) are logged to stderr →
`~/.claude/debug/<session-id>.txt`. Check `/mcp` for connection status.

> *Originally planned:* per-instance identity via `${TEAMMATE_AGENT}` expansion in the
> plugin's mcp `env`, with the server **exiting "no agent"** if unset. *Shipped:* the
> register-tool model — unset `${VAR}` in the plugin `env` broke the config, and a
> setup-style tool is cleaner. The server **never exits on missing identity**; it
> simply waits to be registered.

---

## 11. `skills/teammate-comms/SKILL.md` & launch

`SKILL.md` documents the tools (§9), the two wake regimes (§1), the reliability
contract (inbox is source of truth; `send` warns on an offline peer), profiles, and
the launch line. Installed from the consolidated marketplace:

```powershell
claude --dangerously-load-development-channels plugin:teammate-comms@coltondyck
# or, for local dev (load straight from a checkout):
claude --plugin-dir C:\Users\colto\Documents\Projects\teammate-comms --dangerously-load-development-channels plugin:teammate-comms@coltondyck
```

Then at session start call `teammate_register(agent: "<name>")` (add `team:` for
namespaced inboxes); the channel arms on registration. **Power-user shortcut:** set
`$TEAMMATE_AGENT` before launch to auto-register.

Prerequisites: Claude Code **v2.1.80+**, `uv` installed, channels enabled (individual
Pro/Max: on by default). Custom channels require `--dangerously-load-development-channels`
(not on Anthropic's allowlist; the flag only bypasses that — org `channelsEnabled`
policy still applies).

---

## 12. Status & follow-ups

**Done:**
1. Scaffolded, implemented, and tested (`tests/test_handshake.py` drives the server
   end-to-end: handshake, tool gating, channel push, profile round-trip, version sync).
2. Pure-stdlib path chosen over the `mcp` SDK (§6).
3. README written (install / dev flow / restart-after-first-sync note).
4. Marketplace consolidated into `colton-claude-plugins` (§4b).
5. Profile fields (0.2.0 → 0.3.1) + global-default comms root (0.3.0).

**Remaining:**
- Migrate the `TestSVN` prototype to consume this plugin and drop its local skill copy.
- (Optional) CLI-parity scripts, if ever wanted.
