# Codex Parity Findings — teammate-comms on OpenAI Codex, and Claude Code ↔ Codex messaging

Status: RESEARCH rev 1 (findings only — no implementation authorized)
Peer review: sonnet adversarial pass 2026-09-08 — APPROVE-WITH-CHANGES; both corrections
applied (§4.2 hook-payload citation + root-session-id caveat; §4.1 queue-service gate).
Author: Colton Dyck (solo session), 2026-09-08
Baseline: `E:\E Drive Projects\vibe-cognition\docs\codex-parity-findings.md` rev 2 (+ §7a live
results) and its graph nodes `9408e8e451c5`, `fdada6938389`, `004232949d8c`, `faa1ffbf0ff6`,
workflow `13d7fc563103`. Everything vibe-cognition already verified about packaging, hooks,
skills, MCP env, timeouts and Windows quirks is REUSED here, not re-derived.

Evidence tags: **[doc]** official Codex docs (learn.chatgpt.com), **[src]** openai/codex `main`
read 2026-09-08 via `gh api`, **[bin]** strings/`--help` of the installed `codex-cli 0.153.4`
on this machine, **[vc]** vibe-cognition's findings (doc/src/live as tagged there),
**[live]** observed running. Per ledger 23 the live smoke (WP-TC-C1a below) is what turns
any of this into a brief.

## Headline

teammate-comms is a much smaller port than vibe-cognition: the message store, registry,
groups, reactions, profiles, dashboard and broker CLI are file-based and harness-neutral.
Exactly ONE thing in the plugin is Claude-Code-specific in a load-bearing way — the **idle
wake** (`notifications/claude/channel`) — plus a handful of cosmetic Claude-isms (spawn
command, env-var names, skill/tool text, launch flags).

Codex has **no MCP channel equivalent** and the upstream requests for one are open with no
maintainer response (#15299 inbound MCP notifications, Mar 2026; #20312 event-driven wake,
Apr 2026; #35542 TUI wake by same-user tooling, Jul 2026) **[doc]**. But since
**codex-cli 0.149.0 (2026-08-20)** Codex ships a durable, cross-process **user-message
queue** with an idle-dispatch watcher: `codex queue --thread <uuid> --message <text>`
(app-server method `thread/queue/add`) **[src][bin]**. A queued message on an **idle**
thread starts a new turn; on a busy thread it waits and becomes the next user turn — never
dropped **[src]**. That is a strictly better wake primitive than the Claude channel (which
Claude Code drops, GH #38736/#61797), so the port's wake regime is:

- **Claude Code instance** → channel push (unchanged).
- **Codex instance** → the recipient's OWN watcher thread runs `codex queue` against its own
  thread id. The sender never needs to know the recipient's harness; the inbox file is the
  only shared contract. **Cross-harness messaging therefore needs zero protocol changes** —
  a Claude agent and a Codex agent on the same machine already share `~/.claude/TeammateComms`.

Two new pieces of design are required (§4): (1) the server must learn its own Codex **thread
id** (Codex gives the MCP child no session/thread env, exactly like the project-dir gap
vibe-cognition hit), and (2) the Claude-only env handoffs (`TEAMMATE_AGENT`,
`CLAUDE_PROJECT_DIR`) must get a Codex path because Codex's MCP child env is an allowlist.

## 1. What is harness-neutral today (no work)

| Surface | Why it already works on Codex |
|---|---|
| Storage: inboxes, `_read.json`, groups/transcripts, reactions, registry, project profiles, avatars, compact-request files | Pure stdlib file protocol under the comms root; no harness API involved. |
| `comms.py` comms-root resolution | `TEAMMATE_COMMS_DIR` → `CLAUDE_CONFIG_DIR` → `~/.claude`. Under Codex neither env var reaches the MCP child (allowlist **[vc]**), so it lands on `~/.claude` — the SAME default a Claude Code instance uses. Cross-harness visibility is free as long as `CLAUDE_CONFIG_DIR` is not relocated (see §5 decision 3). |
| Heartbeat / liveness / `teammate_list` | Produced by our own watcher thread, not the harness. |
| MCP `initialize` `instructions`, tool list, tool descriptions | Codex reads `instructions` **[vc live]**; tools are plain MCP. |
| `python -m teammate_comms.deliver` (broker CLI) | Local CLI; harness-agnostic by construction. |
| Dashboard (`teammate_dashboard`) | Local HTTP server + human-operator registration; harness-agnostic. |
| SessionStart hook (`hooks/session-start.sh`) | Codex uses the same `hooks.json` shape, same `additionalContext` contract, sets `CLAUDE_PLUGIN_ROOT` for compat, and the script already self-filters `source=compact` and emits `{}` **[vc]**. Only the Windows launch quirk applies (§3). |
| SKILL.md | Loads unchanged under Codex's agentskills loader; invoked as `$teammate-comms` **[vc]**. Text needs a de-Claude overlay (§3). |

## 2. What is Claude-specific (inventory, from grep of `src/`, `hooks/`, `skills/`)

| # | Item | Where | Codex verdict |
|---|---|---|---|
| C1 | `notifications/claude/channel` push + `capabilities.experimental["claude/channel"]` | `channel.py` `emit_channel_event`/`emit_reaction_event`; `server.py` initialize | **Replace on Codex** with `codex queue` (§4.1). Keep on Claude. |
| C2 | Re-nudge backoff (`compute_reemit`, 120/240/480 s) | `channel.py` | **Disable on Codex**: the queue is durable and dispatches deterministically once idle; a re-queue would double-post. |
| C3 | `${CLAUDE_PLUGIN_ROOT}` in `plugin.json` mcpServers + hooks.json | `.claude-plugin/plugin.json`, `hooks/hooks.json` | Codex: Agent Plugins v1 `plugin.json` + `mcp.json` with `${PLUGIN_ROOT}`; hooks get `CLAUDE_PLUGIN_ROOT` injected anyway **[vc]**. |
| C4 | `"channels": [{"server": …}]` manifest key | `.claude-plugin/plugin.json` | No Codex analog; Claude manifest keeps it, Codex manifest omits it. |
| C5 | `CLAUDE_PROJECT_DIR` → auto-fill `project` label (+ `project_register` path) | `server.py` `register_identity`, `tools.py` | Absent on Codex (no project-dir var, no MCP roots **[vc]**). Needs the §4.2 handoff. |
| C6 | `TEAMMATE_AGENT` / `TEAMMATE_SPAWNED_BY` / `TEAMMATE_TEAM` / `TEAMMATE_COMMS_DIR` env handoffs (reincarnate child auto-register) | `spawn.py` `build_child_env`, `server.py` auto-register | Reach the MCP child only via `[mcp_servers.x].env_vars` (non-plugin install) or static `env` (plugin `mcp.json`) **[doc]** — per-spawn dynamic values do NOT flow. Needs §4.3. |
| C7 | `claude --channels …` / `--dangerously-load-development-channels` / managed-settings allowlist / `--permission-mode bypassPermissions` | `spawn.py` | Codex launch is `codex -C <dir> [flags] "<prompt>"`; no channel gate exists. Bypass analogs: `-a never`, `--dangerously-bypass-approvals-and-sandbox`, `--dangerously-bypass-hook-trust` **[bin]**. |
| C8 | `WEZTERM_PANE` binding + broker typing `/compact` into the pane | `server.py`, broker (external) | Harness-neutral IF the Codex TUI lives in a WezTerm pane; Codex also exposes `thread/compact` over app-server **[bin]** as a keystroke-free alternative (not needed for v1). |
| C9 | Skill/tool prose: "Claude Code", `SendMessage`, `/mcp`, `~/.claude/debug`, `Agent tool`, `subagent` | `skills/teammate-comms/SKILL.md`, `tools.py` descriptions | Text overlay per vibe-cognition WP-H0d; server can key CTA text on harness. |
| C10 | `--no-sync … python -m teammate_comms.server` via `uv run --directory` | manifests | Under Codex use `--project` not `--directory` (chdir footgun **[vc live]**) — irrelevant for us only because we don't read cwd, but keep consistent. |

Nothing else in 6.5k lines of `src/` references the harness.

## 3. Packaging & hooks — inherit vibe-cognition's verified layout

Adopt the identical root layout vibe-cognition's findings §2 prescribe (Agent Plugins v1
`plugin.json` + `mcp.json` at root, `.codex-plugin/plugin.json` overlay for `paths.hooks`,
`.claude-plugin/plugin.json` untouched, shared `hooks/` and `skills/`), the second marketplace
manifest `.agents/plugins/marketplace.json` in `colton-claude-plugins`, and one Loki pin per
release in both files **[vc]**. Windows specifics that bit vibe-cognition and bite us
identically **[vc live]**: hooks run under `cmd.exe /C`, inline `set "VAR=…" &&` is mangled,
bare `bash` is the WSL launcher — so ship a `commandWindows` pointing at a bare-path `.cmd`
wrapper that calls Git Bash by full path. Hook output must stay within
`hookSpecificOutput.{hookEventName, additionalContext}` (`deny_unknown_fields`) **[vc]**.

Two facts specific to this plugin:

- **Cold-start race is a non-issue here.** teammate-comms is pure stdlib with no heavy
  imports; the MCP handshake yields in well under a second, so Codex's 30 s startup timeout
  (source) / 10 s (docs) both clear, and we have no SessionStart `mcp_tool` hook to race.
- **Plugin `mcp.json` `env` is static.** Anything per-machine (e.g. `TEAMMATE_COMMS_DIR`
  overrides, `TEAMMATE_REINCARNATE_ENABLED`, `TEAMMATE_AVATARS_ENABLED`) must go in the user's
  `[mcp_servers.teammate-comms].env`/`env_vars` (option B install) or a documented plugin
  data-dir config file; the shell env does not reach the server on Codex.

## 4. The three design items

### 4.1 Wake on Codex = self-queue via `codex queue` (source-verified mechanism)

How the primitive works **[src]** (`codex-rs/ext/queue/src/service.rs`,
`app-server/src/request_processors/thread_queue_processor.rs`, `tui/src/session_queue_commands.rs`,
`state/src/sqlite.rs`):

1. `codex queue --thread <UUID|name> --message <text>` resolves the target, then calls
   `thread/queue/add`. With a running local app-server daemon it goes through the daemon;
   otherwise the CLI starts an **embedded** app-server in its own process. Either way the item
   is written to the durable **`$CODEX_HOME/queue_1.sqlite`** (present on this machine **[live]**).
   `require_thread` accepts a thread that is NOT loaded in the calling process as long as it
   exists in the thread store and is not archived / not an unloaded spawned sub-agent.
2. Every Codex process that hosts threads — the plain interactive TUI runs an in-process
   app-server (`AppServerTarget::Embedded`, `tui/src/startup_orchestration.rs`) and installs the
   queue extension (`app-server/src/extensions.rs`; `message_processor.rs` constructs the
   `queue_service` whenever a state DB opened AND `experimental_thread_store` is `Local`, the
   default — an in-memory thread store silently disables queue wakes) — runs
   `QueuedItemService::watch_external_messages`: a **10 s poll** of the queue DB's
   `change_version`; on change it emits `ThreadQueueChanged` and, if the thread's `AgentStatus`
   is not Running/Interrupted/Shutdown/NotFound, calls `wake_if_loaded` →
   `emit_thread_idle_lifecycle_if_idle` → `on_thread_idle` → `dispatch_if_idle` → a **new turn**
   with the queued text as the user message. If a turn is in flight the item stays queued and
   dispatches on the next idle (`ThreadIdleCause::Completed`).
3. Same-process adds (`codex queue` via the daemon while the TUI is attached to that daemon)
   wake immediately; cross-process adds wake within ≤10 s.

Consequences for `channel.py`:

- The watcher keeps ALL of its gating logic (`known_ids`, muted groups, unseen set, reaction
  wakes). Only the emit changes: on Codex, `emit_channel_event` runs
  `[codex, "queue", "--thread", <thread_id>, "--message", <same signal-only content>]` as a
  list-argv subprocess with DEVNULL stdio (the `spawn.py` rules), from the watcher thread.
- Disable re-nudge on Codex (C2). Optionally guard with `thread/queue/list` later; v1 relies
  on the durable queue.
- The queued text is a **user turn**, not a system notification: the agent will see
  "📬 2 new message(s) from bob. …" as if typed. Keep the content signal-only and end it with
  the explicit instruction to call `teammate_inbox` (already the case). Codex caps queued text
  at `MAX_USER_INPUT_TEXT_CHARS`; our content is a few dozen chars.
- `codex` must be on PATH for the server process (Codex's MCP child env includes `PATH`
  **[vc]**; the npm shim `codex.cmd`/`codex.ps1` resolves via `PATHEXT` on Windows).

Rejected alternatives:
- **Write `queue_1.sqlite` directly** — private schema, versioned filename (`_1`), migrator-owned;
  would fork Codex's storage protocol (same reasoning as WP-38's rejection of a second inbox
  writer).
- **Speak JSON-RPC to the daemon socket ourselves** — needs the daemon running (not default),
  the experimental-capability opt-in, and our own client; `codex queue` already handles
  daemon-vs-embedded and the incompatibility errors.
- **Stop hook `decision: block` loop** — fires only at turn end, cannot wake an idle session,
  and #37937 shows the trap.
- **`async: true` hooks** — output is delivered "at the next safe turn point or user turn";
  cannot start a turn **[doc]**.
- **WezTerm keystroke injection** (the broker's technique) — works only inside WezTerm and
  types into a composer that may hold a draft; keep as an emergency fallback, not the design.
- **Wait for #15299/#20312/#35542** — no maintainer engagement in 3–6 months.

### 4.2 The server must learn its Codex thread id (and project dir)

`codex queue` needs the thread UUID (names are unreliable: no `--session-name` flag exists in
0.153.4 **[bin]**; names come from `/rename`/`thread/name/set` and must be unique). The MCP
child gets no thread env; ordinary tool calls carrying `_meta.threadId` is an unproven
spike item **[vc]**. But the Codex **SessionStart hook payload's `session_id` IS a thread id**
(`SessionStartRequest.session_id: ThreadId` in `codex-rs/hooks/src/events/session_start.rs`,
populated from `sess.session_id()` in `codex-rs/core/src/hook_runtime.rs` **[src]**), and
hooks also receive `cwd` **[doc]**. Caveat (peer review): `session_id()` is the **root**
thread's id, shared by descendant threads — it equals the thread's own id for a top-level
interactive `codex` session or an independently launched process (our cases), but NOT for a
Codex-native spawned sub-agent or fork; never reuse this handoff for those.

Recommended handoff (race-free, no MCP dependency, no new file protocol): the SessionStart
hook (all sources) emits `additionalContext` telling the agent
`teammate-comms: harness=codex thread_id=<session_id> project_dir=<cwd>`; the agent passes
these to `teammate_register` (new optional args `harness_session`, `project_dir`; on Claude
Code the hook can emit the same line — `session_id` exists there too — and the server simply
doesn't need `harness_session`). The register record stores `harness` and `harness_session`
so `teammate_list`/`teammate_profile` show which harness each teammate runs on.

Fallbacks, in order, if the spike shows the agent fails to pass them: (a) `_meta.threadId`
on tool calls if present; (b) hook writes `${PLUGIN_DATA}/sessions/<session_id>.json` and the
server correlates via parent-pid (both the hook and the MCP child are children of the same
`codex.exe`); (c) `session_index.jsonl` most-recent-by-cwd heuristic (last resort, ambiguous
with two sessions in one repo).

Harness detection itself: read `params.clientInfo.name` on `initialize` (spike records the
exact strings for Claude Code and Codex) with an explicit `TEAMMATE_HARNESS` env override.

### 4.3 Spawning a Codex teammate (`teammate_reincarnate`)

Per-spawn env handoffs cannot reach a Codex MCP child (C6). Codex path: launch
`codex -C <project_dir> [bypass flags] "<prompt>"` where the prompt itself carries the
identity ("You are teammate `<agent>`; call `teammate_register(agent='<agent>', …)` first"),
and `TEAMMATE_SPAWNED_BY` provenance becomes a register arg rather than env. A `harness`
argument on `teammate_reincarnate` (default: the caller's own harness) picks the command
builder. Codex's bypass analogs exist **[bin]** but a spawned child cannot answer approval or
hook-trust prompts, so the spike must confirm which flags a DEVNULL child actually needs.

## 5. Decisions needed from Colton

**RULED 2026-09-08 (Colton):** (1) wake design APPROVED as written; (2) thread-id handoff =
hook `additionalContext` → agent → `teammate_register`; (3) comms root: **MOVE to
`~/.teammate-comms` with a one-time migration** (against the recommendation below — both
harnesses must land on the new compiled-in default; broker `--comms-dir` and external tooling
update in the same release); (4)+(5) independent spike now via non-plugin `codex mcp add`,
converge packaging when vibe-cognition's WP-C1b lands. The spike itself still awaits an
explicit go. Original questions kept below for the record.

1. **Approve the wake design**: Codex wake = recipient self-queues via `codex queue`
   (§4.1), re-nudge disabled on Codex. (Alternative: wait for an upstream channel — not
   recommended.)
2. **Thread-id handoff**: hook `additionalContext` → agent → `teammate_register` (§4.2
   recommended) vs. hook-written session file + pid correlation.
3. **Comms root default**: keep `~/.claude` as the shared default for both harnesses (zero
   config, works today) vs. introduce a harness-neutral `~/.teammate-comms` with a one-time
   migration. Recommendation: keep `~/.claude` for now; revisit when a Codex-only machine
   shows up.
4. **Install form for the spike**: non-plugin `codex mcp add` (option B, the rig vibe-cognition
   already has; allows `env_vars`, `required`, `startup_timeout_sec`) first, plugin form second
   — same order vibe-cognition took.
5. **Sequence relative to vibe-cognition's Codex port**: share its H0b restructure/marketplace
   work (one Loki pin ceremony) or run independently. Recommendation: independent spike now
   (the plugin is small), converge packaging when vibe-cognition's WP-C1b lands.

## 6. Proposed work packages (not authorized — for planning)

- **WP-TC-C1a — live spike (go/no-go, ledger 23).** On this machine, codex-cli 0.153.4,
  Windows native: (1) install via option B (`codex mcp add teammate-comms -- uv run --no-sync
  --project <repo> python -m teammate_comms.server`), register, confirm `teammate_*` tools and
  server instructions load; (2) from a second terminal, `codex queue --thread <uuid> --message
  "📬 test"` against the idle TUI — measure wake latency (expect ≤10 s), confirm a turn starts
  and the message renders; repeat mid-turn to confirm deferral; (3) confirm `codex queue`
  side effects: does it fire hooks, spawn MCP servers, take a writer lock, and how long does
  the embedded-app-server path take; (4) record `clientInfo.name`, whether tool calls carry
  `_meta.threadId`, and whether hook `session_id` == the id `codex queue` accepts; (5) Claude
  Code ↔ Codex round-trip DM and group post both directions; (6) the elevated-sandbox case
  (`[windows] sandbox = "elevated"` is set here; daemon probe enforces non-elevated peers).
- **WP-TC-C1b — harness abstraction + Codex wake.** `harness` detection, `harness_session`
  register arg + record fields, `channel.py` emit strategy (channel vs. queue), re-nudge gate,
  `teammate_list` harness column, tests (pure decision functions stay hermetic; the subprocess
  seam is injected like `spawn.py`).
- **WP-TC-C1c — packaging.** Root `plugin.json`/`mcp.json`, `.codex-plugin` overlay,
  `commandWindows` wrapper, marketplace manifest, manifest-parity lint, README/SKILL overlay,
  `teammate_reincarnate` Codex builder.
- **Gate TC-C1 — human field verification** (Windows native; POSIX if available), including
  the mixed-harness team case.

Critical path: C1a → C1b → C1c → Gate. C1b and C1c are parallelizable after C1a (real-edge
test: C1c reads nothing from C1b except the register arg names, which the brief fixes up
front).

## 6a. Live results — WP-TC-C1a, 2026-09-08 (codex-cli 0.153.4, Windows native, no daemon)

Rig: workflow `a9a367ca93ea` (option B `codex mcp add`, skill copied, SessionStart hook
wrapper). Claude side = this session registered as `Fable`; Codex side = `Codex1`.

| Item | Result |
|---|---|
| Install + register | `teammate_*` tools load; `Codex1` registers; `project` = **(not set)** (C5 confirmed). |
| Channel push → Codex | **No wake** (expected). Codex1: "I did not independently wake and check the inbox." |
| `codex queue` → idle TUI | **GO.** Woke **near-instantly** (not the 10 s poll ceiling), turn started, agent called `teammate_inbox`. No extra `teammate_comms.server` process left behind. |
| `codex queue` → busy TUI | **Deferred correctly** — waited for the turn to finish, then became the next turn. |
| Claude ↔ Codex DM | **Both directions work**, threaded via `reply_to`, zero protocol changes. |
| Daemon path | `codex app-server daemon start` → "only supported on Unix platforms" in 0.153.4 (main has a Windows backend; not shipped yet). Embedded path suffices. |
| Thread id source | Codex1 read **`CODEX_THREAD_ID`** from its exec/shell env (`core/src/unified_exec/process_manager.rs`) — NOT from our hook. The MCP child does not receive it (`rmcp-client` `env_clear()` + allowlist) and the parent `codex.exe` env lacks it, so `env_vars` whitelisting can't forward it. |
| SessionStart hook | **Works from the second session on**: fresh session quoted `harness=codex thread_id=… project_dir=…` and `hook.log` matched (hook `session_id` == the id `codex queue` targets; hook `cwd` is a usable project dir). The first session ran without it — most likely the hook-trust gate (a new hook is skipped until trusted; trust lands at that session's first turn). README rule: expect one hook-less session after installing/changing a hook. |

Design consequence: the thread-id handoff gets a simpler primary path than ruling 2 —
`teammate_register`'s Codex-facing guidance asks the agent to pass `harness_session` from
`CODEX_THREAD_ID` (it already knows it); the hook route becomes the fallback and the carrier
for `project_dir` if the exec env has no cwd analog (the hook `cwd` does). Everything else in
§4.1 stands as verified.

## 7. Risks

- **Codex churn**: weekly releases; `thread/queue/add` is experimental-flagged in the
  protocol (`experimental_required_message`), though the CLI handles that. Pin the verified
  version in the brief; conformance test asserts `codex queue --help` shape.
- **10 s wake latency** on the cross-process path (vs. ~0.5 s channel). Acceptable for
  agent-to-agent nudges; the daemon path is immediate if Colton runs `codex app-server
  daemon start`.
- **Queued text is a user turn**: a poorly worded wake could be misread as a task. Keep it
  signal-only (already the WP-11a contract).
- **Elevated Windows sandbox** may block the daemon probe (`ensure_non_elevated_peer`); the
  embedded path still works. Spike item.
- **Two sessions in one repo** make name-based targeting ambiguous — we never use names.

## Sources

- Codex docs: learn.chatgpt.com/docs/hooks, /docs/app-server, /docs/config-file/config-reference.
- openai/codex `main` 2026-09-08: `codex-rs/tui/src/{lib.rs,startup_orchestration.rs,session_queue_commands.rs,named_session_lookup.rs,session_archive_commands.rs}`,
  `codex-rs/cli/src/queue_cmd.rs`, `codex-rs/app-server/src/{extensions.rs,message_processor.rs,request_processors/thread_queue_processor.rs}`,
  `codex-rs/ext/queue/src/{lib.rs,service.rs}`, `codex-rs/thread-store/src/queue_store.rs`, `codex-rs/state/src/sqlite.rs`,
  `codex-rs/hooks/src/types.rs`, `codex-rs/app-server-daemon/src/lib.rs`, `codex-rs/uds/src/lib.rs`; PR #39092 (merged 2026-08-17).
- Upstream issues: #15299, #20312, #35542, #21551 (co-presence RFC), #25914, #37450, #38297, #37937, #11415 (closed not planned).
- Installed: codex-cli 0.153.4 (`codex --help`, `codex queue --help`, `codex app-server daemon --help`, binary string scan).
- vibe-cognition: `docs/codex-parity-findings.md` rev 2 + §7a, `docs/hermes-parity-plan.md` (Harness Capability Contract, WP-H0a–d).
