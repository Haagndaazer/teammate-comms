# teammate-comms — Design (as-built)

> A Claude Code **plugin** bundling an **MCP server** that gives independent full
> Claude Code instances agent-to-agent messaging plus **channel-based idle wake**.
> **Pure-stdlib** Python (zero runtime dependencies), shipped as a marketplace plugin.

This document began as a pre-build blueprint; it has been **reconciled to the
as-built implementation (v0.7.1)**. Where a design decision reversed during the
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
│   ├── session-start.sh        # builds the venv before the server spawns
│   └── reinject-instructions.sh # re-injects the standing instructions after a compact
├── skills/teammate-comms/SKILL.md
├── tests/test_handshake.py     # end-to-end server test (handshake + tools + channel + dashboard HTTP + hooks)
├── .github/workflows/ci.yml    # CI: run the harness on ubuntu + windows (added 0.7.x)
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
- **Hook hardening (0.7.x / WP-3):** both hooks **fail closed but VISIBLE** — an unset
  `CLAUDE_PLUGIN_ROOT` emits valid `{}` and exits 0 instead of dying silently under
  `set -u` before any JSON. session-start.sh writes the sync stamp **only on a successful
  `uv sync`** (a failed sync no longer stamps a half-built venv as done → the next session
  retries instead of failing silently). Its entry is matcherless (fires on every
  SessionStart source); rather than rely on unverified `hooks.json` matcher syntax it
  **self-filters on the stdin `{"source":...}`** and fast-exits `{}` on a `compact` (the
  venv already exists mid-session) — contract-independent and hermetically tested.
- **First-session UX:** on a fresh install the hook builds the venv and the server
  connects after a **restart** — identical to vibe-cognition. Documented in the README.
- A **second SessionStart hook** (`hooks/reinject-instructions.sh`, `matcher:"compact"`)
  re-emits the server `INSTRUCTIONS` as `additionalContext` after a `/compact` (added
  0.7.0). The MCP `initialize` handshake surfaces them at session start, but those aren't
  known to survive a compaction; the text is single-sourced in `teammate_comms.instructions`
  (stdlib-only `main()` → SessionStart JSON, run via the same `uv run --no-sync` launch) so
  server + hook never drift. Mirrors vibe-cognition's pattern.

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
  "version": "0.7.1",
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

**Wake-reliability contract (honest, v0.8.x / WP-9 + WP-12).** The inbox JSON is the durable source
of truth, but a dropped channel push is **not** auto-recovered: an idle agent never reads its
inbox unprompted, so "drained on the next `teammate_inbox`" only holds once *something else*
wakes it. Claude Code is known to drop channel notifications — GH **#38736** (mid-turn
notifications dropped, not queued, despite the docs) and **#61797** (sporadic silent drops at
idle), both unresolved — so after one emit the watcher **re-nudges** still-unseen unread with
capped exponential backoff: if `unseen = (unread − muted) − last_seen` persists, re-emit the
same wake at 120 s, then 240 s, then 480 s, capped at 3 attempts. Re-nudge is
**content-agnostic** — a dropped emit is a dropped emit regardless of message type (DM, group,
urgent, @mention). Re-nudge gates on the **exact same `unseen` set** the first nudge uses, so
a read-but-unacked or muted message can never re-nudge (the v0.4.2 no-noise contract, preserved
verbatim). Crucially the backoff clock is **armed only by a real fresh emit** — when `unseen`
empties (caught up) the clock is **disarmed** (not re-stamped), so a batch that was never
first-nudged (a message that arrived while its group was muted and is later unmuted, or the
registration seed window) stays permanently re-nudge-silent until a genuinely new message
re-arms via the fresh path — closing the retro-nudge-after-unmute hole. (Accepted edge,
consistent with fresh-wake semantics: if the clock IS armed and a muted message is unmuted
*into* a still-unseen batch, the re-nudge count includes it — the same "count reflects all
unseen" rule a fresh wake uses.) Every emit (fresh / re-nudge / reaction) logs one greppable
stderr line (`[teammate-comms] wake-emit kind=… unseen=… attempt=…`) so server-emitted can be
told from client-dropped for an upstream bug report. `compute_reemit` is a pure,
hermetically-tested decision; a still-lost wake after the cap is no worse than the prior
one-and-done behavior. The watcher loop body is wrapped in `try/except Exception → stderr +
continue` so no future emit bug can silently kill the daemon thread.

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
long-lived server. **17 tools (13 original + 4 project-profile tools added v0.9.0):**

| Tool | Args | Behavior |
|------|------|----------|
| `teammate_register` | `agent`, `team?`, `comms_dir?`, profile? (`project`/`role`/`personality`/`status`/`authority`) | Establish identity, register the inbox, arm the channel. Optionally set a profile (`project` is auto-filled). Re-registering only re-establishes identity + channel and **preserves** the existing profile. |
| `teammate_send` | `to`, `message`, `priority?` (`normal`\|`urgent`), `post_type?` (`decision`/`blocker`/`fyi`/`chatter`), `reply_to?` | Append a message to `to`'s inbox (atomic write). Report whether `to`'s channel is live (auto-nudge) or offline (queued). Self-send rejected. **A `#`-prefixed `to` posts to a group** (fan-out); `@name` tokens to group members become `mentions`. |
| `teammate_inbox` | `count_only?` | Read this agent's unread messages (or just the count). Shows group tag, `post_type`, `🔔(@you)`, `↳ re`, and reaction summaries. |
| `teammate_ack` | `id` (or `"all"`) | Move a message from unread → read. |
| `teammate_list` | — | List registered agents with type + liveness (**always shows `project`, `status`, `authority`**; `role`/`personality` when set), plus a Groups section. Humans show `🧑 (operator)` + `presence`. |
| `teammate_whoami` | `verbose?` | Resolved identity, team, comms dir, and own profile (diagnostics). `verbose:true` adds a read-only **doctor** section — comms root, per-agent heartbeat freshness/liveness, sub-stream file sizes, unread counts, leftover lock dirs (G-5). |
| `teammate_update` | `project?`/`role?`/`personality?`/`status?`/`authority?` | Update own profile fields (self-only field-merge; empty string clears a field). |
| `teammate_profile` | `agent?` | Read a teammate's full profile (defaults to self). |
| `teammate_group` | `action` (`create`/`delete`/`join`/`leave`/`add`/`members`/`history`/`mute`/`unmute`/`reads`), `group`, `members?`, `limit?`, `sender?`/`post_type?`/`since?`/`reply_to?` (history filters) | Manage group chats (see below) + mute/unmute a group's wakes + `reads` (read receipts). |
| `teammate_react` | `to_message`, `emoji` (`thumbsup`/`rofl`/`smile`/`cry`/`100`/`fire`), `remove?` | React to a message by id (recorded in `reactions.jsonl`, shown in inbox/history/dashboard). **Wakes only the author** of the reacted-to message (never the group, never on remove). |
| `teammate_reincarnate` | `agent`, `project_dir`, `prompt?`, `team?`, `comms_dir?` | Spawn a NEW Claude instance in a terminal as a named teammate (auto-registers via env). **Gated** by `TEAMMATE_REINCARNATE_ENABLED`; confirms launch, not registration. |
| `teammate_dashboard` | `port?`, `open_browser?`, `human_name?` | Launch the local web console + register the human as a teammate (see below). |
| `teammate_delete` | `message?` (a message id) **or** `teammate?` (an agent name) — exactly one (XOR, enforced in the handler) | Delete a message (**tombstone**) or remove an **offline** teammate (see below). |
| `project_register` | `key?`, `summary?`, `description?`, `tech_stack?`, `repo_url?`, `name?`, `status?`, `path?` | Create or update a project profile (merge-upsert under blocking lock). `key` defaults to caller's normalized `project` label. `path` auto-fills from `$CLAUDE_PROJECT_DIR` on first create. |
| `list_projects` | — | Concise directory: display name + live roster + summary per project. Trailing aggregate: undocumented project labels + near-miss agents. |
| `project_profile` | `key?` | Full project detail: all fields, provenance, live roster (liveness per member). |
| `project_delete` | `key?` | Remove a project profile file. |

Every tool's error text wraps the underlying cause with a one-line action sentence.

**Deletion (added 0.7.0).** `teammate_delete` covers two destructive ops, in a codebase
that is otherwise append-only:
- **Message = tombstone, not erase.** `tombstone_fields()` rewrites a record in place —
  body → `"— message deleted —"`, `deleted:true`, `deleted_by` — while keeping `id`,
  `from`, `to`/`group`, `reply_to`, `post_type`. A group post shares ONE id across the
  group `messages.json`, every member's inbox copy, and the transcript, so the tombstone is
  applied to each durable store (the inbox helper locks the **unread** file and rewrites
  both `_unread`/`_read` under it — same lock discipline as `teammate_ack`). Keeping the id
  + `reply_to` means citations and thread/group continuity survive. Permission is
  **author-or-operator** (`is_operator=True` only on the dashboard path).
- **Teammate = hard remove, offline only.** Deletes the registry record + inbox files +
  strips the name from every group's `members`; their authored messages stay attributed. A
  **live** teammate is refused (its ~5s heartbeat would re-create the record while its inbox
  was deleted — dropping queued messages); self / the human operator are refused too.
- **`resolve_message`** finds a message's author + locations via the transcript first, with
  a scan fallback over group transcripts + inboxes when `TEAMMATE_TRANSCRIPT=0`.
- **`transcript.jsonl` is NOT rewritten** (the append-only firehose invariant is kept).
  Instead deletions flow through a dedicated **`deletions.jsonl`** event stream — the same
  shape as `reactions.jsonl`: append-only, its own dashboard poll cursor (`dcursor`), folded
  client-side into a `deleted` set (idempotent, keyed by target). That's how an open console
  reflects a mutation the firehose can't (it's id-keyed and append-only): the message flips
  to its placeholder within a poll tick; a `kind:"group"` event drops the channel from the
  sidebar. Whole-group `teammate_group(action="delete")` now also hard-removes the group's
  fan-out copies from member inboxes (resolved **before** the `rmtree`) and emits a
  `kind:"group"` deletion event — fixing the old "deleted group's messages lingered" bug.
  **Append durability (v0.7.1):** a *message* deletion's event uses a **blocking** lock and
  surfaces a lock failure to the caller — the firehose still carries the live message, so a
  lost event there is *persistent* dashboard inconsistency (no self-heal even on reload), and
  the caller's retry re-tombstones idempotently. *Group* and *teammate* deletions keep a
  **best-effort** append (`block=False`): they destroy the group dir / agent record **before**
  the event (the recorded "emit event LAST so a partial `rmtree` can't desync" ordering), so a
  raised append would be lost permanently on retry — and they self-heal anyway, since a fresh
  load simply omits the absent group/teammate.
- **Deletions compaction (v0.7.1, C-2).** `deletions.jsonl` is append-only, so a fresh console
  load used to replay only its newest `limit` (1000) events — past that window an old tombstone
  was lost and the message **reappeared**. Fix: events older than the newest `DELETIONS_RETAIN`
  (1000) are folded into a **target-keyed set-file** `deletions_set.json` (`{target: event}`,
  deduped — deletions are monotonic, there is no undelete). A fresh load now reads the
  **complete** deleted-set = that baseline **unioned with the ENTIRE live `jsonl`** (a full read,
  *not* a bounded tail — see the invariant below); both halves fold idempotently by target.
  - **Compaction (`_compact_deletions_locked`)** runs inline under the appender's already-held
    lock (a cheap `getsize` gate on each append; both the blocking and best-effort paths), and is
    **always best-effort — it never fails a delete** (the event append is the durable op;
    compaction is pure size relief). Ordering is load-bearing: **write the set-file FIRST**
    (atomic temp+rename), **THEN trim the jsonl** (atomic temp+rename). A crash/failure in between
    leaves the jsonl still holding everything *and* the set holding the folded head (idempotent
    overlap) → a fresh load reads a superset, never a gap; the reverse order could drop tombstones.
    The mkdir-lock keys off the path *string* (a sibling `.lock` dir); `os.replace` swaps the file
    inode only, so re-reading + atomically replacing the jsonl under its own lock is deadlock-free.
  - **Completeness invariant (gate-independent).** The live jsonl holds every not-yet-folded
    event; the set-file holds every folded (older) target; their union is every deletion ever —
    *no matter when or whether the size-gate fired*. The byte gate (`DELETIONS_COMPACT_BYTES`)
    therefore only bounds the live-file **size**, it is **not** a correctness input — which is
    exactly why the fresh-load read must stay a **full** `read_deletions(limit=None)` and must
    **never** be "optimized" back to the bounded newest-N tail (that would skip an un-folded middle
    event sitting between the retained tail and the gate).
  - **Lagged-cursor rescue (no reload needed).** A **cursored** client (live console mid-poll)
    normally replays *no* baseline. But if it lags so far that its `dcursor` is **older than the
    jsonl's oldest surviving id** (`floor`) — a suspended tab resuming after `>DELETIONS_RETAIN`
    deletions — the events in `(dcursor, floor)` were compacted into the set-file and it never saw
    them. The poll detects `dcursor < floor` and replays the **complete baseline∪jsonl union that
    one poll** (the rescue), so a message deleted while the tab slept can't silently render
    *undeleted* until the user happens to reload ("recovers on reload" is self-*concealing*, not
    self-healing). It fires once — `dcursor` then advances past `floor` and the steady state falls
    back to the cheap incremental walk, so live polls pay nothing extra.
  - **Accepted ECs (self-healing).** (1) A group
    hard-deleted then **recreated with the same name** has its old `kind:"group"` tombstone (target
    is the group *name* `#x`, not a unique id) replayed on every fresh load, hiding the new group
    until its first message re-pushes it — pre-existing, now with a wider replay window. (2) Under
    sustained `file_lock_optional` contention a group/teammate delete may skip both its append and
    compaction; the jsonl stays correct (full-file fresh read), just larger.
- **Intentional, documented behaviors:** dashboard reflection requires the firehose
  (`TEAMMATE_TRANSCRIPT=1`); a tombstoned message's reactions persist in MCP reads
  (inbox/history) though the dashboard hides them; reacting to a deleted message still wakes
  its author (the tombstone preserves `from`).
- **Delete races are eventually-consistent, not atomic (A-4/A-5, documented).** Per-store
  locking has no cross-store atomicity (a recorded v0.7.0 decision), so two narrow windows are
  accepted rather than closed: (A-4) a member who **joins during** a group-message tombstone
  fan-out can keep a live copy of that message — the fan-out reads `members` once; (A-5) a
  whole-group delete purges member inboxes by the **`group == sigil` predicate** (catching a
  fan-out copy that lands before the purge — strictly better than the old id-snapshot), but a
  `send_group` that passes its meta-exists check before the `rmtree` yet fans out **after** the
  purge can still orphan an inbox copy. Both are bounded and self-heal on the consumer side: a
  fresh dashboard load omits the absent group, and `resolve_message`'s group-dir fallback finds
  nothing. Closing either would require holding the group meta lock across the inbox fan-out —
  the cross-store atomicity deliberately not built. The MCP inbox read is the only surface that
  briefly shows the orphan, until it's acked.

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
human's display name is the `human_name` arg, else **`$TEAMMATE_HUMAN_NAME`**, else
`human`. The
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
  record, cached on the 5s heartbeat). The watcher drops muted-group messages from the
  wake/count via an inline set-difference (`(unread_ids - muted_ids) - known_ids - last_seen`
  in channel.py — there is no separate `_audible` helper) but keeps them in the inbox;
  `known_ids` still absorbs the muted ids so an unmute never retro-nudges. A 1:1 DM (no
  `group` key) can never be muted.
- **Read receipts:** read-only inference — `group_read_positions` reads each member's
  `_read.json` for the max acked group-message id (no write path, no ack change). Surfaced
  via `teammate_group reads` + dashboard ticks. An ack/seen upper bound, groups-only.
- **Threading:** an optional `reply_to` id stored unvalidated (a citation hint), rendered
  flat (`↳ re <id>`); `history reply_to=<id>` pulls a thread. No send-path read.
- **Reactions:** `teammate_react` appends to an always-on (NOT transcript-gated) NDJSON
  `reactions.jsonl` keyed by target message id (covers DMs + groups without mutating the
  append-only records); `aggregate_reactions` folds add/remove. A reaction stamps the
  reacted-to message's author (`target_from`, resolved from the transcript) so the watcher
  wakes **only that author** (a low-volume check on the 5s heartbeat tick) — never the
  group, never the reactor, never on remove (v0.6.1; was ambient/no-wake in v0.6.0).
  Unlike the observability transcript, the reaction append is **durable, not droppable**
  (a reaction is a feature — a drop loses a wake + a chip): a short blocking lock that
  surfaces failure to the caller, who retries (adds fold idempotently). **Missed-wake
  hardening (v0.7.1):** the wake check is driven by a **high-water cursor** over
  `reactions.jsonl` (the pure `compute_reaction_wakes` decides; `known_ids` holds only the
  previous tick's ids for exact boundary dedup), so a burst larger than the read window
  pages **forward** across heartbeats instead of scrolling past a fixed tail and silently
  missing an author's wake. A very large burst drains at the read-window size per ~5s tick
  — bounded extra latency, never a drop (the same missed-event guarantee the message-wake
  path got in v0.4.x).
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
  `TEAMMATE_LAUNCH_ARGS`. **Launch ≠ registration (F-5):** the return states an expected
  registration window (~10–20s) + a `teammate_list` recheck, and is explicit that a headless
  trust-prompt-absent child may never register (which would look identical to success). The
  child's register record carries a `spawned_by` provenance breadcrumb: `build_child_env` sets
  `TEAMMATE_SPAWNED_BY` to the parent **unconditionally** (never inherited — a grand-child gets
  its immediate parent, the same never-inherit discipline as the stripped reincarnate gate, F-1),
  read at register and stored as a registry-record field (not a profile field) that survives the
  heartbeat merge.
- **Dashboard upgrades:** dropped the redundant "Direct messages" section (Teammates is
  the DM entry + shows presence); added an "Observed (read-only)" section (agent↔agent
  DMs) directly under Teammates; a right-hand live activity firehose (FIFO, newly-seen
  records only, scroll-aware, last-200); reaction chips + a clickable emoji bar
  (`POST /api/react`, reactions sub-stream on `/api/poll`); read-receipt ticks; field
  chips for `post_type`/mentions/`reply_to`. **Read receipts refresh on the 5s roster tick, not
  the 1.5s poll** (so they can lag ~5s): the poll is deliberately **conversation-agnostic** (the
  server is stateless — it doesn't know which thread the browser has open), so live per-thread
  receipts would need a new poll parameter + member-gating. That's audit **B-4**, deferred as a
  documented known-limit — a protocol change isn't worth ~3.5s of freshness on a cosmetic
  indicator (if real users feel the lag it returns as its own WP with a `&conv=` design).
- **Compose send-parity (0.7.x / WP-4):** `_api_send` now passes `reply_to` + `post_type`
  through to the cores (was DM/group `to`+`message`+`priority` only), and the compose row
  gained the matching write-side affordances: a `↳` reply chip (click a message's `↳` →
  pending `reply_to`, clearable), a `post_type` select, and an `urgent` toggle. Send/react
  failures now `alert` the server's reason (B-3) instead of silently swallowing. All new
  DOM is `textContent`-only (no `innerHTML`), preserving the strict-CSP discipline.

**Observability transcript (added 0.5.0-dev).** To let the console show *all* messaging
(1:1 DMs were previously ephemeral — only in `_unread.json`, gone after ack), both send
cores **tee** every message into one append-only **NDJSON** log
`TeammateComms/[<team>/]transcript.jsonl` (`{id,from,to?,group?,priority,message,kind}`,
`kind` ∈ `dm`/`group`). NDJSON append (O(1), one line) avoids the O(n²) full-rewrite and
whole-team lock serialization a JSON array would impose. The tee is **last and
best-effort** (a short never-raise lock; disabled by `TEAMMATE_TRANSCRIPT=0`) so
observability never precedes or delays the authoritative delivery write. `TEAMMATE_TRANSCRIPT=0`
gates **only this firehose** — `reactions.jsonl` and `deletions.jsonl` are always written
(they're features, not observability), and with the firehose off a reaction (DM or group
post) can't resolve its `target_from` so its author-wake is skipped. **Privacy note:**
this durably records previously-ephemeral agent↔agent DMs under the (global-by-default)
comms root — intended for the operator overseeing their own agents, opt-out via the env
flag. Reads are window-bounded (`read_jsonl_tail(200)` on a fresh load) so a large log never floods
the browser. **Records BYTE cursor (P3).** The dashboard's records stream is paged by a **byte
offset**, not an id: the opaque poll cursor is `"<offset>|<generation>"` (a byte position + a
crc32 of the file's first line). A fresh load takes the newest tail and mints the cursor at ~EOF
via a **stat-then-tail** order — the offset is the size captured *before* the tail, so it is `≤`
the tail's size; a record appended between the two observations is shown by the tail *and*
re-streamed next poll, where the browser's id-dedup folds the repeat (the reverse order would
drop it). A cursored poll then reads **only the bytes appended since** that offset (one bounded
read vs the old O(file) `since` scan), advancing the offset by **raw newline geometry** so it
always lands on a `\n` boundary; a partial trailing line is left for the next poll (torn-tail
safe), and a burst larger than `limit` pages out across polls. **Validity / transparent re-tail:**
if the offset exceeds the file size (truncation/recreation-smaller) or the first-line
`generation` no longer matches (recreation), the byte position is meaningless, so the reader
**re-tails** (newest `limit`) and re-mints — transparently, since the browser's `seen` id-set
dedups any re-served record (no reload signal, no frontend change). **Generation contract (for
WP-10 rotation):** `generation` changes iff the bytes of the first line (byte 0 .. first `\n`)
change — append-stable (byte 0 never moves on the append-only log), recreation-sensitive; a
future WP-10 NDJSON **rotation** MUST bump the generation explicitly (it replaces byte 0) rather
than rely on the crc, and the one out-of-contract hole — a recreated file whose first line is
byte-identical *and* whose size ≥ the old offset — is unreachable in practice (it needs the same
first message down to its microsecond-timestamp id). A second, even narrower unreachable window
(P3 review note): `transcript_tail_and_cursor` stats the size, then reads the first line for the
generation — a recreation landing *between* those two reads mints offset-from-old + generation-
from-new; if the new file then grows past the old size the next seek lands mid-line and the
partial-line discard drops one fragment, self-correcting at the next `\n` boundary. Like the hole
above it needs an *in-flight* transcript recreation, which nothing in-product does. The
**reactions/deletions** sub-streams keep
their **id**-based `oldest_first` pagination — a different cursor family with NO shared reset
semantics (a transcript recreation re-tails records but does not reset those streams). (Audit C-2 — a fresh
deletions load replaying only the newest `limit` events, so a very old deletion rendered
undeleted — is **fixed in v0.7.1** by deletions compaction: the fresh load unions a target-keyed
`deletions_set.json` baseline with the full live jsonl. See the deletions-compaction bullet in §8.)

> **Out-of-order tee (audit N-1) — FIXED in P3 by the byte cursor.** A message's `id` is
> stamped at *send* (before the inbox lock) and is **load-bearing** — it's the inbox-record id,
> the ack id, and the react/delete resolution key — so it CANNOT be re-stamped under the
> transcript lock the way reactions/deletions were (WP-1 CR-1); moving it would desync the
> transcript id from the ack/react identity. Two concurrent sends can therefore tee out-of-order
> (`[T2, T1]`). The old **id** cursor (`id >= since`) skipped a record teed *after* the cursor
> had advanced past its id: it never appeared in the **firehose** (the message was still
> delivered, acked, reacted, and deletion-resolvable — only the observability view lost it). The
> **byte cursor makes id order irrelevant**: a late tee still lands at **EOF** (`open "a"`), so
> byte-streaming emits it — it then renders in **arrival order** in the feed. **Shown late beats
> never shown.** **Audit C-1** (`react()`/`resolve_message` scanning a bounded recent tail) is
> likewise resolved in P1's `read_jsonl_tail` `_scan_transcript_for_id`; NDJSON **rotation**
> itself stays deferred to **WP-10**, with the generation contract above written to receive it.

**Profile fields (0.2.0; `project` added 0.3.0).** Stored as plain keys on the agent
registry record via `write_agent_record`'s field-level merge — additive,
backward-compatible, and they survive the 5s heartbeat (the test asserts this).
`validate_profile_field` collapses whitespace/newlines and length-caps per field.
`project` is auto-filled as a two-component `parent/name` from `$CLAUDE_PROJECT_DIR` at
registration (F-4 — so two repos sharing a basename like `api` are distinguishable in
`teammate_list`; falls back to the bare name at a drive/UNC root, and is pre-truncated to the
field cap so a deep path can never raise out of validation and break registration), overridable,
so peers see who is working where now that comms are global. An agent's
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

**Trust model — the boundary it ISN'T.** teammate-comms is a **single-user, cooperative,
localhost** tool; its security posture is exactly that — *not* authentication. Stated plainly
so no future feature mistakes a convenience for a boundary:

- **`from` is caller-asserted, never authenticated.** A sender supplies its own name (the
  sender-explicit cores are what enable the human-as-teammate dashboard — decision
  `a9594b942f2b`); any local process can author a record as any name. `from` is **advisory**.
- **The author-only delete check and reaction/mention wake-routing consume `from` as a
  convenience, not authorization.** "Only the author (or operator) can delete" is an
  **anti-footgun** guard against deleting someone else's message by mistake — not access
  control; a process asserting a different `from` bypasses it trivially, and a forged
  `target_from`/`mentions` can wake an arbitrary victim.
- **Any local process of this OS user can read any inbox.** The comms root is a shared
  directory tree (global by default — decision `ef4af8135c03`) with default OS-user file
  permissions; there is no per-agent secret. The trust domain is *all processes of this OS
  user*, full stop.

This is **correct and intentional** for the threat model — your own agents, on your own
machine, cooperating. The hard rule: a future cross-host / multi-user feature must **NOT**
build on the author-check or `from` as if they were real authentication; it would need an
actual auth layer added first. (The dashboard's per-launch token *is* a real secret —
loopback-bound, constant-time-compared, query-string-redacted in logs — but it guards the
**HTTP console**, not the file-level comms.)

**Cross-host note (WP-6 / A-7).** On a *shared* comms root that spans hosts, the `file_lock`
dead-holder steal is **host-gated**: the lock dir records the holder's pid **and host**, and a
contender steals only when the holder is on *its own* host (`socket.gethostname()`) and that
pid is verified dead — `_pid_alive` is purely local, so a remote pid is unknowable (mirrors
`is_channel_alive`'s host-gated pid trust). Consequence: a **dead remote** holder's lock is
never auto-stolen (it's recovered only manually, bounded by the lock timeout's raise/drop) —
strictly safer than stealing a possibly-live remote and getting two writers.

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
policy still applies). To skip that flag (and its prompt), pre-trust the channel via a
machine-wide managed-settings allowlist and launch with `--channels` instead — see the
README's "Trusting the channel" section; `spawn.py` auto-detects this file for reincarnate.

---

## 12. Project profiles (v0.9.0, WP-13)

**Goal:** first-class project entities layered on the existing free-text `project` agent
field — let a teammate discover *which projects exist*, *who is on each*, and *what
each does* without having to piece it together from scattered profiles.

**Data model:**
- Profiles stored as `TeammateComms/<team?>/projects/<slug>.json` (one file per project).
- Stored fields: `summary` (80), `description` (600), `tech_stack` (400), `repo_url` (200),
  `name` (100) + `status` enum (`active`/`paused`/`archived`) + `path` (uncapped,
  whitespace-collapsed) + provenance (`created_by/at`, `updated_by/at`). Slug is
  `urllib.parse.quote(normalized_key, safe="")` — injective because `%` is forbidden in keys.
- **Membership is derived, never stored.** Roster = agents whose normalized `project`
  matches the profile key, excluding `type=="human"`. O(N) scan on `list_projects` and
  `project_profile` — same complexity as `teammate_list`, noted in §12 as a known trade-off.

**Key normalization (`validate_project_key`):** the load-bearing correctness fix. Normalizes
`\` → `/`, lowercases, collapses repeated `/`, strips leading/trailing `/`, rejects `%` (slug
injectivity) and other filesystem-unsafe chars. Applied at roster-derivation time AND in the
dashboard `_api_conversations` payload — so all comparison paths use the same normalized form
and the cross-OS split cannot re-emerge at any layer.

**Lock discipline:** `write_project_record` uses `file_lock` (BLOCKING), not
`file_lock_optional`. Two simultaneous first-creates serialize; neither silently clobbers the
other. Mirrors `append_reaction` (reactions are a feature, not observability — a drop is
permanent feature-data loss). `file_lock_optional` stays for heartbeats only.

**Dashboard:** `_api_conversations` normalizes each roster entry's `project` to the canonical
key (None on failure, never raw string — a raw-string fallback would re-split the sidebar).
Returns a `projects` dict keyed by normalized key; `renderNav` enriches existing `byProject`
subheads with the profile `summary`/`status`. No second grouping structure.

**Known trade-off:** `list_projects` scans all agent files (O(N)). Acceptable at current scale
(matches `teammate_list`). An index could be added as a follow-up if agent count grows.

---

## 13. Status & follow-ups

**Done:**
1. Scaffolded, implemented, and tested (`tests/test_handshake.py` drives the server
   end-to-end: handshake, tool gating, channel push, profile round-trip, version sync).
2. Pure-stdlib path chosen over the `mcp` SDK (§6).
3. README written (install / dev flow / restart-after-first-sync note).
4. Marketplace consolidated into `colton-claude-plugins` (§4b).
5. Profile fields (0.2.0 → 0.3.1) + global-default comms root (0.3.0).
6. Group chat via `#sigil` fan-out + typed posts + @mentions + mute + read receipts (0.4.x–0.5).
7. `teammate_dashboard` web console + human-as-teammate + NDJSON observability transcript (0.5.0).
8. Reactions (author-wake) + `teammate_reincarnate` + managed-settings channel auto-detect (0.6.x).
9. `teammate_delete` (message tombstones + offline-teammate removal) + deletions sub-stream (0.7.0).
10. Missed-event hardening (0.7.1): poll-cursor forward pagination, reaction-wake high-water
    cursor, durable (blocking) reaction/deletion appends — see §7/§8.
11. Lean wakes + lean surface + authority coordination rule (0.8.1 WP-11).
12. Watcher crash + group recovery + same-name re-register compaction reset (0.8.2 WP-12).
13. Project profiles + cross-OS roster fix + 4 new tools (0.9.0 WP-13).

**Remaining:**
- Migrate the `TestSVN` prototype to consume this plugin and drop its local skill copy.
- (Optional) project roster index if agent count grows large.
- (Optional) CLI-parity scripts, if ever wanted.
