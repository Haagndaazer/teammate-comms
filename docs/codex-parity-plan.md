# Codex Parity Plan — teammate-comms v0.16.0 (+ v0.17.0 root move), built for maintainability

Status: PLAN rev 3 — APPROVED-WITH-CHANGES (sonnet verification pass 2026-09-08; the
four text corrections it asked for are applied and marked `[V<n>]`). History: rev 1 review
REJECT (14 findings, `[R<n>]`); rev 2 delta review REJECT (9 findings, `[F<n>]`). Colton's
rulings (§9) still open; WP-43/44 implemented against this rev.
Author: Colton Dyck (solo session, comms name Silvie)
Date: 2026-09-08
Inputs: `docs/codex-parity-findings.md` (rev 1 + §6a live GO results); rulings
`3a9d66c920cc` (codex-queue self-wake), `621a222132d6` (thread-id handoff),
`d6d67b9a9c49` (comms root → `~/.teammate-comms`), `b14a54018f45` (independent, converge
packaging), `e0f182f9a6b3` (mirror vibe-cognition once shipped); vibe-cognition v0.35.0 as
shipped and field-verified at commit `5b58b07` (`docs/wp-codex-plugin-plan.md`, Gate C1
episode `6c3c15e3a0a8`, `adapters/codex/*`, `.codex-plugin/plugin.json`,
`.agents/plugins/marketplace.json`, `tests/test_codex_hooks_shell.py`), its follow-on
`docs/codex-parity-plan.md` rev 1 (harness module + parity registry principles, adopted in
lightweight form; its template renderer is NOT adopted, §2.4), and Vince's live gotchas
(discovery `478a56b57042`).

## 1. Goal

A Codex user installs teammate-comms the same way a Claude Code user does (marketplace +
one restart), registers, and from then on messages any teammate on the machine — Claude Code
or Codex — and is woken when messaged. Claude Code behaviour is unchanged except for the
version string and one new `harness:` line in roster/profile output (§4.3).

Maintainability contract `[R5]` — adding harness #3 touches exactly:
1. one row in the harness table (`harness.py`);
2. one `adapters/<harness>/` directory;
3. one column in `docs/PARITY.md`;
4. **only if** the harness needs a new wake mechanism: one `Waker` class registered by name
   in `channel.py`'s `WAKERS` table; **only if** it needs a new spawn command: one builder
   function registered by name in `spawn.py`'s `SPAWN_BUILDERS` table.
Nothing else may name a harness. `[F4]` Enforcement is a **literal ratchet**, not an
allowlist: `tests/test_codex.py` holds a committed per-file baseline of case-insensitive
`codex`/`claude` occurrences in `src/teammate_comms/*.py` (today: `server.py` 9, `comms.py`
8, `spawn.py` 22, `channel.py` 6, `tools.py` N — counted when the test lands; `harness.py`
exempt). The test fails if any file's count EXCEEDS its baseline; lowering a baseline is
free, raising one requires editing the test in the same commit (a reviewable act). Growth
is expected only in `harness.py` (exempt), the `WAKERS`/`SPAWN_BUILDERS` tables and the
classes/functions they register (their files' baselines are raised in the WP that adds
them). Protocol literals (`notifications/claude/channel`, `experimental.claude/channel`),
env-var names (`CLAUDE_PROJECT_DIR`, `CLAUDE_CONFIG_DIR` until v0.17.0) and the existing
Claude-only launch helpers in `spawn.py` are simply part of the baseline — honest, sized,
and shrinking as they move behind the table.

Two releases:

- **v0.16.0 — Codex parity** (WP-43…WP-46 + Gate TC-C1). No storage changes.
- **v0.17.0 — comms root move** (WP-47). Breaking; deliberately separate so the Codex
  release cannot be blamed for a storage migration and vice versa.

## 2. Architecture principles

1. **Policy lives in the server; adapters stay thin.** Every harness-specific fact lives in
   ONE Python table (`harness.py`): wake strategy name, whether wakes are durable, skill
   invocation syntax, display name, the session-id hint sentence, the launch-args override
   variable, install/update CTA, debug-log hint, spawn builder name. Everything else asks
   the table.
2. **One env var selects the harness.** `TEAMMATE_HARNESS` (default `claude-code`), set by
   the Codex hook's `codex mcp add --env` exactly as vibe-cognition sets `VIBE_HARNESS`.
   No `clientInfo` sniffing, no env-absence heuristics. (`clientInfo.name` is logged to
   stderr for diagnostics only.)
3. **Adapters are copies of vibe-cognition's at commit `5b58b07`, names swapped**
   (`adapters/codex/hooks.json`, `hooks/session-start.{sh,cmd}`, `find-git-bash.cmd`,
   `.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`). Identical copies are the
   agreed convention with Vince until a divergence justifies a shared repo. We have no
   compact reinjection hook (removed in WP-41), so no `reinject.*`.
4. **No template renderer.** One 160-line skill, no agents. A harness-neutral SKILL.md with a
   "Harness notes" section plus a forbidden-literal lint over the rest gives the same drift
   guarantee. If vibe-cognition's renderer is ratified and grows a second consumer, we adopt
   it then — the source split (neutral body + harness block) is already the same shape.
5. **Parity is a table with a completeness test.** `docs/PARITY.md`: one row per tool, hook
   event, skill, and CLI × harness, values `full | partial | waived | n/a`, mandatory note
   for anything not `full`; a test asserts every `TOOL_DEFINITIONS` name and every
   `hooks/*.sh` has a row.
6. **Conformance over installation.** CI never installs Codex: the adapter hook runs with a
   fake `codex`/`uv` on PATH; the wake path runs through an injected runner. The human field
   gate (§8) covers what only a real Codex can show.

## 3. Verified facts the design rests on (findings §6a + Vince's live gotchas)

- `codex queue --thread <uuid> --message <text>` wakes an idle plain-TUI Codex session
  near-instantly, defers correctly mid-turn, leaves no process behind, needs no daemon.
- The Codex agent knows its thread id from `CODEX_THREAD_ID` in its exec env; the SessionStart
  hook payload's `session_id` is the same id and its `cwd` is the project dir. The MCP child
  receives neither (env allowlist) — the agent must pass them at `teammate_register`.
- Codex's MCP child env is allowlist + the entry's static `env`: per-spawn env handoffs do not
  flow on Codex.
- A newly installed Codex hook does not run in the session that trusts it.
- `codex app-server daemon` is Unix-only in 0.153.4; irrelevant (embedded path works).
- Windows: Codex wraps the whole `commandWindows` line in its own quotes → the value must be
  ONE unquoted path to a `.cmd`; `${VAR}` substitution applies to both commands; bare `bash`
  is the WSL launcher (`find-git-bash.cmd`). Manifest paths must start with `./`;
  `author`/`repository`/`license` are ignored; Codex never creates the plugin data dir for a
  hooks+skills plugin; `codex mcp add` overwrites an existing entry and replaces its env block;
  concurrent sessions share one data dir with unlocked `config.toml` writes; `codex plugin
  remove` needs `name@marketplace`; inside Codex's sandbox call `codex.cmd`, expect "Could
  not find home directory", and process enumeration is denied.
- Liveness math that constrains the wake design `[R1]`: `HEARTBEAT_SECONDS = 5`,
  `HEARTBEAT_STALENESS_SECONDS = 30`, and `run_watcher` is a single serial loop with no
  subprocess calls today.

## 4. Design

### 4.1 `src/teammate_comms/harness.py` (new, ~70 lines)

```
@dataclass(frozen=True)
class Harness:
    name: str              # "claude-code" | "codex"
    display: str           # "Claude Code" | "Codex"
    wake: str              # key into channel.WAKERS: "channel" | "codex-queue"
    durable_wake: bool     # True → no re-nudge backoff
    needs_session: bool    # wake requires harness_session on the record
    session_hint: str      # sentence appended to the register result when needs_session and none given
    skill_invoke: str      # "/teammate-comms" | "$teammate-comms"
    launch_args_var: str   # "TEAMMATE_LAUNCH_ARGS" | "TEAMMATE_LAUNCH_ARGS_CODEX"   [R4]
    spawn_builder: str     # key into spawn.SPAWN_BUILDERS: "claude" | "codex"
    install_cta: str
    debug_hint: str

HARNESSES = {"claude-code": Harness(...), "codex": Harness(...)}
DEFAULT = "claude-code"
def current(env=os.environ) -> Harness   # unknown value → DEFAULT + one stderr warning
```

`[R6]` there is no `plugin_root_var` field — both harnesses set `CLAUDE_PLUGIN_ROOT` for
hooks and nothing in Python consumes it. `[R5]` the "Codex:" register sentence is the
table's `session_hint`, not prose in `server.py`.

### 4.2 Wake strategy (`channel.py`)

Split "build the content" (pure, unchanged) from "deliver it":

```
class ChannelWaker:      wake(content, meta) → send_message({"method": "notifications/claude/channel", …})
class CodexQueueWaker:   wake(content, meta) → spawn a daemon thread that runs
                           runner(["codex","queue","--thread",session,"--message",content],
                                  stdin/stdout/stderr=DEVNULL, timeout=20) and logs one line
WAKERS = {"channel": ChannelWaker, "codex-queue": CodexQueueWaker}
def make_waker(harness, session, send_message, runner=subprocess.run) → Waker | None
```

- `[R1]` **Never block the watcher loop.** `CodexQueueWaker.wake` returns immediately; the
  subprocess runs on a per-call daemon thread. At most ONE wake thread per waker is in
  flight (`threading.Lock` + `is_alive` check). Worst case the watcher loop is delayed by
  thread start (microseconds), so heartbeats stay on their 5 s cadence and the 30 s
  staleness window is untouched.
- `[F1]` **A wake that cannot be dispatched is NOT retired.** `Waker.wake()` returns
  `True` (dispatched) or `False` (dropped: busy). `run_watcher` advances the bookkeeping
  (`known_ids |= unread_ids`, `last_emit_mono`, `reemit_attempts`; for reactions
  `known_reaction_ids`/`reaction_cursor`) ONLY when `wake()` returned `True`. `[V2]` The
  reaction bookkeeping is withheld only on needed-and-dropped: on a tick with no reaction
  wake needed (empty `fresh_rx`) the cursor and known set commit as today, so the read
  window keeps paging (audit A-2 stays fixed). On `False`
  the fresh set stays fresh and the very next 0.5 s tick recomputes the content from the
  CURRENT unseen set (so senders/groups/reply targets are never stale) and tries again;
  the retry succeeds as soon as the in-flight subprocess finishes (≤ 20 s). `ChannelWaker`
  always returns `True` (synchronous `send_message`, as today) so Claude bookkeeping is
  unchanged. A drop logs `dropped-busy` once per busy episode, not per tick.
- `[R2]` **Waker refresh does not reset nudge state.** `Identity` gains
  `harness_session` and a separate `waker_generation` counter, bumped only when
  `harness_session` changes. `run_watcher` re-resolves the waker when EITHER generation
  changes, but resets `known_ids`/clocks only on the identity generation as today. `[F9]` A
  `waker_generation` change ALSO sets `last_hb = 0` (immediate heartbeat + muted-cache
  refresh), so a same-identity re-register loses none of today's incidental refreshes —
  only the `known_ids` reseed. A same-name re-register whose only change is
  `harness_session` bumps `waker_generation` alone (§4.3), so a two-call Codex registration
  cannot swallow a message. `[F7][V3]` Normal case (per the findings §6a "Design
  consequence", which supersedes §4.2's pre-live recommendation): the agent reads `$CODEX_THREAD_ID` itself
  (primary, per the findings §4.2) and passes it on its FIRST `teammate_register`; the hook
  line is the fallback reminder and the carrier for `project_dir`.
- `make_waker` returns `None` when `harness.needs_session` and `session` is empty: the
  watcher logs once per registration ("wake disabled: register with harness_session") and
  skips emits; inbox delivery is unaffected.
- Re-nudge: `compute_reemit` is consulted only when `not harness.durable_wake`. The pure
  function is untouched.
- Reaction wakes and re-nudges go through the same waker instance (the one-in-flight rule
  applies to all three emit sites: fresh, re-nudge, reaction).
- Every emit logs `[teammate-comms] wake-emit kind=<fresh|renudge|reaction> harness=<name>
  unseen=<n> attempt=<n>`; the Codex thread adds `rc=<n>|timeout|dropped-busy` on completion.
- `initialize` keeps advertising `experimental.claude/channel` on every harness.

### 4.3 Registration handoff (`server.py`, `tools.py`, `comms.py`)

- `teammate_register` gains optional `harness_session` (the harness's own session/thread id)
  and `project_dir` (a path). `[R12]` `project_dir` is NOT validated for existence: it is
  whitespace-collapsed and fed to the same `_project_label_from_git_remote` →
  `_project_label` degrade path `CLAUDE_PROJECT_DIR` uses today (explicit arg wins over env,
  env over nothing); a bad value yields a bad label, never a failed registration.
- `register_identity` always writes `harness` (= `harness.current().name`) and ALWAYS
  passes `harness_session` — `None` when omitted `[R9][F5]`, exactly the `manager`
  precedent (`server.py` stores `None` to clear; `write_agent_record` has no empty-string
  semantics — that lives only in `write_project_record`). The id is session-scoped, never
  inherited. Stored via the existing field-merge alongside `type`/`spawned_by`; the WP-19
  epoch/instance logic and the "re-register preserves profile" contract are untouched.
- `[R2][V4]` `register_identity` calls `Identity.set(...)` (bumps the identity generation)
  only when agent/team/root differ from the current identity or the caller is unregistered;
  it ALWAYS calls `Identity.set_harness_session(...)`, which bumps `waker_generation` only
  when the value changed.
  KNOWN-INTENTIONAL: this changes one existing behaviour — a same-name re-register with no
  changes no longer re-seeds `known_ids`. Post-compaction re-registration (the reason for the
  reset) still works because compaction does not change the unread set; a test pins that a
  message arriving between two same-name registers still nudges.
- Register result: when `harness.needs_session` and no `harness_session` was given, append
  `harness.session_hint` ("Re-register with harness_session set to your thread id (from the
  session-start context or $CODEX_THREAD_ID) so teammates can wake you."). Claude Code:
  nothing appended (`needs_session=False`).
- `[R11][F6]` `teammate_list`/`teammate_profile`/`teammate_whoami` render a `harness:` line
  for every NON-human record (`type != "human"` — the human operator runs no harness and is
  created by `register_human`, never `register_identity`), showing the record's `harness`
  field or `(not set)` when absent (pre-upgrade records; the surrounding convention for
  absent optional fields). No fabricated default. WP-43's acceptance is "byte-identical to
  v0.15.0 except the added `harness:` lines", asserted precisely by the transcript diff.
- Tool description for `teammate_register` documents the two args in harness-neutral words.
  `[F8]` Both `project_dir` descriptions disambiguate: on `teammate_register` it is a label
  source and need not exist; on `teammate_reincarnate` it must exist (unchanged).
- `TEAMMATE_SPAWNED_BY` provenance stays env-only. No `spawned_by` register arg.

### 4.4 Codex adapter (`adapters/codex/`, `.codex-plugin/`, `.agents/`)

Copied from vibe-cognition `5b58b07`, names swapped (`VIBE_` → `TC_` in the `.cmd`
variables); differences called out:

- `adapters/codex/hooks.json`: one SessionStart entry, matcher `startup|resume|clear|compact`
  (the shared script self-filters compact), `command` →
  `bash "${CLAUDE_PLUGIN_ROOT}/adapters/codex/hooks/session-start.sh"`, `commandWindows` →
  `${CLAUDE_PLUGIN_ROOT}\adapters\codex\hooks\session-start.cmd` (one unquoted path),
  timeout 120, `additionalContextLimit` 2000.
- `adapters/codex/hooks/session-start.sh`: reads stdin into `HOOK_INPUT` (only when not a
  tty); `mkdir -p "$CLAUDE_PLUGIN_DATA"`; exports `TEAMMATE_HARNESS=codex` and
  `UV_PROJECT_ENVIRONMENT=<data>/.venv`; registers the MCP server exactly as vibe-cognition
  does (stamp + mkdir-lock; `codex mcp add teammate-comms --env TEAMMATE_HARNESS=codex --env
  UV_PROJECT_ENVIRONMENT=<venv> -- uv run --no-sync --project <root> python -m
  teammate_comms.server`; `cygpath -m` paths; restart note on first registration); extracts
  `session_id` and `cwd` from `HOOK_INPUT` with an escape-aware `sed` `[R8]` (value pattern
  `(\\.|[^"\\])*`, then unescape `\"` and `\\`); `[V1]` EXPORTS `TEAMMATE_HOOK_NOTE="teammate-comms:
  harness=codex thread_id=<id> project_dir=<cwd>. When registering, pass harness_session=<id>
  and project_dir=<cwd>. <restart note if any>"`; then `printf '%s' "$HOOK_INPUT" | exec bash
  "${PLUGIN_ROOT}/hooks/session-start.sh"` (the shared script's `[ ! -t 0 ]` stdin read and
  compact self-filter keep working because stdin is a pipe with the original JSON).
- `hooks/session-start.sh` (shared) — two harness-neutral tweaks: (a) `VENV_DIR` honours
  `UV_PROJECT_ENVIRONMENT` when set; (b) `[R7][F2]` `emit_context` gains a `_json_escape`
  helper (`sed`: `\` → `\\`, `"` → `\"`, tab → `\t`; CR/LF stripped — the note is one line)
  and prepends `"$(_json_escape "$TEAMMATE_HOOK_NOTE") "` when the variable is non-empty, so
  ALL three exit paths carry it — the uv-missing warning, the first-install restart message,
  and the tail, which becomes `emit_context ""` when the note is set and stays `echo '{}'`
  otherwise. Callers' existing literals stay pre-escaped as today; only the dynamic note is
  escaped, at the single point where it enters the heredoc. With both variables unset the
  output is byte-identical to today (golden test). WP-45 asserts the emitted JSON parses and
  round-trips a Windows path with backslashes and a POSIX path containing `"`.
- `adapters/codex/hooks/session-start.cmd` + `find-git-bash.cmd`: verbatim copies with the
  variable prefix and message text swapped.
- `.codex-plugin/plugin.json`: name, version, description, keywords, `skills:
  ["./skills/teammate-comms"]`, `hooks: ["./adapters/codex/hooks.json"]`, `interface`.
- `.agents/plugins/marketplace.json`: dev marketplace `teammate-comms-dev`, source `url` +
  `ref: main`, vibe-cognition's policy block.
- `.claude-plugin/plugin.json`: version bump only.
- Release: I send Loki the Codex marketplace entry
  (`{"name":"teammate-comms","source":{"source":"url","url":"https://github.com/Haagndaazer/teammate-comms","sha":"<code commit>"},"policy":{"installation":"AVAILABLE","authentication":"ON_INSTALL"},"category":"Other"}`)
  in the same request as the Claude pin, after the code commit is final. Ping Vince before
  touching `colton-claude-plugins` or vibe-cognition.

### 4.5 Skill (`skills/teammate-comms/SKILL.md`)

Body stays harness-neutral; Claude-specific wording moves into `## Harness notes` with one
bullet list per harness (Claude Code: channel wake, `--channels` launch, subagents woken by
SendMessage, debug log path. Codex: invoke as `$teammate-comms`; register with
`harness_session`/`project_dir` from the session-start line; wake arrives as a queued user
turn; one restart after install/update; the reincarnate gate must be set on the MCP entry
via `codex mcp add --env`). Lint (WP-45): outside that section no `Claude Code`,
`SendMessage`, `/mcp`, `~/.claude/debug`, `--channels`, `$teammate-comms`, `codex` literals.
Frontmatter `description` becomes harness-neutral.

### 4.6 Reincarnate on Codex (`spawn.py`, `tools.py`)

- `spawn.py` gains `build_codex_command(prompt, project_dir)` →
  `["codex", "-C", project_dir, "-a", "never", "--dangerously-bypass-approvals-and-sandbox", prompt]`
  and `SPAWN_BUILDERS = {"claude": build_claude_command, "codex": build_codex_command}`.
  `_handle_reincarnate` picks the builder from `harness.current().spawn_builder`, overridable
  by a new optional `harness` arg (validated against `HARNESSES`).
- `[R4]` Launch-args override is harness-scoped: each builder reads the variable named in
  its harness row (`TEAMMATE_LAUNCH_ARGS` for Claude — unchanged name and semantics;
  `TEAMMATE_LAUNCH_ARGS_CODEX` for Codex). A Claude override can never be shlex-split into a
  Codex argv.
- `[R3]` `spawn_in_terminal(argv, cwd, env)` checks `shutil.which(argv[0])` and raises
  `FileNotFoundError("<argv[0]> CLI not on PATH …")` — the executable name comes from the
  argv, never a literal. Existing tests pass `["claude", …]` and keep passing.
- Identity handoff on Codex: the prompt carries it — "You are teammate `<name>`. First call
  `teammate_register(agent='<name>', harness_session=<your CODEX_THREAD_ID>,
  project_dir='<dir>')`, then `teammate_inbox`." `build_child_env` is unchanged (its env is
  harmless on Codex).
- KNOWN-INTENTIONAL: a Codex child's `spawned_by` is not recorded; the reincarnate gate on
  Codex is set on the MCP entry env, not the shell.

### 4.7 Comms root move (v0.17.0, WP-47) — ruling `d6d67b9a9c49`

Resolution becomes: explicit arg → `TEAMMATE_COMMS_DIR` → **`~/.teammate-comms`**.
`CLAUDE_CONFIG_DIR` is dropped from the chain (a Claude-only signal the Codex server can
never see; keeping it guarantees a split roster on any machine that sets it — ruling §9.2).
Migration = an explicit one-shot CLI `python -m teammate_comms.migrate_root` (script
`teammate-comms migrate-root`):

- `[R10][F3]` holds an exclusive `file_lock` on `<old root>/TeammateComms/.migrate.lock`
  for the WHOLE operation (check → move → marker). This serialises concurrent `migrate-root`
  invocations only — no store write path is made migration-lock-aware (that would touch
  every hot writer for a one-shot manual op). Residual, documented: an OLD-version server
  started during the seconds-long window could recreate `<old>/TeammateComms` after the
  move; the CLI re-checks liveness immediately before the move (ledger 9), prints a
  post-move warning if the old tree reappears, and the README states "run with every
  teammate stopped";
- refuses if ANY agent record under the old root satisfies `is_channel_alive` (pid check on
  the same host, heartbeat staleness otherwise) OR has a heartbeat younger than 60 s
  (double the staleness window, because an old-version server may still be mid-shutdown);
  with `[R1]` fixed, a live instance's heartbeat can no longer look stale because of wakes;
- moves `<old>/TeammateComms` → `<new>/TeammateComms` with `os.replace` on one volume,
  copy + size/hash verify + delete otherwise; the whole directory moves, which carries every
  sub-store (inboxes, agents, groups, projects, avatars, compact-requests, reactions,
  reactions_state, deletions, transcript, `plugin-runtime.json`) without an itemised list;
- leaves `<old>/TeammateComms/MIGRATED.json` naming the new root — for humans and for the
  `teammate_whoami verbose` doctor, which reports "legacy root present, not migrated" when
  the old tree exists without a marker and "migrated to <root>" when it does;
- is idempotent (second run: nothing to do, exit 0).
Also updated: broker `--comms-dir` contract note (WP-38) + a note to Wellington,
`deliver.py` help text, README/DESIGN/SKILL paths, CHANGELOG (breaking).
**Alternative considered — automatic move on first start:** rejected (rolling-upgrade
hazard, ledger 19; no clean owner for a half-copied cross-volume tree). Ruling §9.1.

## 5. Work packages (v0.16.0)

**WP-43 — Harness module + registration handoff.** `harness.py`; `Identity.harness_session`
+ `waker_generation` + `set_harness_session`; `register_identity` same-identity path;
`teammate_register` args; record fields (always-written `harness`, always-passed
`harness_session`); list/profile/whoami `harness:` line; `session_hint` in the register
result; `clientInfo.name` stderr breadcrumb. Tests (`tests/test_codex.py`, script style,
own CI step `[R13]`): table lookup incl. unknown → default + warning; register stores both
fields; omitted `harness_session` clears a previous one `[R9]`; `project_dir` arg beats env
and a nonexistent path still registers `[R12]`; same-name re-register bumps
`waker_generation` not the identity generation, and a message landing between two same-name
registers still nudges `[R2]`; transcript diff vs v0.15.0 golden = exactly the `harness:`
lines `[R11]`. ≈ 1 day.

**WP-44 — Codex wake.** `Waker` split + `WAKERS`; `make_waker`; watcher re-resolve on either
generation; one-in-flight daemon thread `[R1]`; durable-wake re-nudge gate; reaction wakes
via the waker; breadcrumbs. Tests with an injected runner and a fake clock: fresh unread on
codex → exactly one argv `["codex","queue","--thread",<id>,"--message",<content>]`; the
watcher loop's next heartbeat is not delayed by a runner that sleeps 5 s (assert heartbeat
timestamp cadence); `[F1]` a second fresh message while the first runner is blocked is
reported `dropped-busy`, its id is NOT added to `known_ids`, and it wakes on the first tick
after the runner returns with content naming the NEW sender (fails against a version that
retires on drop); same for a reaction arriving during a busy wake; no re-emit after 480 s
on codex, re-emit on claude; no session → zero
runner calls + one log line; runner rc≠0/timeout → no raise, one log line; Claude path
notification payload equals the v0.15.0 golden. Ledger 12/20: every test named for the branch
it pins and asserted on the exact argv/log text. Depends on WP-43. ≈ 1.5 days.

**WP-45 — Codex packaging + hooks + skill + parity + literal ratchet.** §4.4 + §4.5 +
`docs/PARITY.md` + completeness test + SKILL lint + `src/` literal ratchet with committed
per-file baselines `[R5][F4]` + manifest-parity test (`.claude-plugin` vs `.codex-plugin`:
name/version/description equal, every skill/hook path exists and starts with `./`) + hook
shell test with fake `codex`/`uv` (first run: `mkdir` data dir, registers once with the exact
argv, writes the stamp; second run spawns no `codex`; stdin JSON → note contains the thread
id and the unescaped cwd for a Windows path with spaces AND a POSIX path containing `\"`
`[R8]`; uv-missing and first-install exits both carry the note `[R7]`; both vars unset →
`{}` byte-identical) + README "Codex CLI" section + CHANGELOG + version 0.16.0 in three
manifests + CI step. Test file: the hook shell checks live in `tests/test_codex.py` next to
the WP-43/44 blocks, bash-guarded like the existing hook tests `[R13]`. No real edge to
WP-44 (arg names fixed here); runs in parallel. ≈ 1.5 days.

**WP-46 — Reincarnate on Codex.** §4.6 incl. `[R3]`/`[R4]`. Tests through the Popen seam:
harness=codex builds the codex argv with the prompt carrying the register call;
`which` check names `codex` and a missing `codex` raises before any Popen (fail-condition
test with an empty PATH `[R3]`); `TEAMMATE_LAUNCH_ARGS` set + harness=codex → Claude
override ignored `[R4]`; claude path argv equals today's golden. Depends on WP-43. ≈ 0.5 day.

**Gate TC-C1 — human field verification** (Windows native, Colton): brief mirrors
vibe-cognition's `codex-test-brief.md` phases 0–4 plus Phase 5 (wake): install from the dev
marketplace; session 1 registers the server + restart note; session 2 shows the hook line,
`/mcp` connected, register with `harness_session` in ONE call, `teammate_list` shows
`harness: codex`; from a Claude Code session send a DM → Codex wakes within 15 s with no
manual `codex queue`; group post both directions; mid-turn DM deferred; a wake while a wake
is in flight (two DMs 1 s apart) → one queued turn, `dropped-busy` breadcrumb; reincarnate a
Codex teammate from Claude Code; two Codex sessions in two projects → two server trees;
uninstall leaves nothing. Then release + Loki pin (both marketplace files, one request).

Real-edge graph: WP-43 → {WP-44, WP-46}; WP-45 ∥ all; Gate after all four. Critical path:
WP-43 → WP-44 → Gate. Solo ≈ 4.5–5 days + gate. Worktree per WP; fix + proof in the same
commit; sonnet peer review per WP before merge (solo contract).

## 6. Work packages (v0.17.0)

**WP-47 — Comms root move.** §4.7. Tests: resolution order; `CLAUDE_CONFIG_DIR` ignored;
migrate refuses on a planted live record (fresh heartbeat + own pid — ledger 3) and on a
60 s-young heartbeat; moves on a stale one; a second concurrent `migrate-root` blocks on
the lock and then finds nothing to do `[F3]`; the old tree reappearing after the move
produces the post-move warning; writes the marker; idempotent; cross-volume path exercised via a monkeypatched `os.replace` raising
`EXDEV`; doctor reports both legacy states. Docs + CHANGELOG + version. Human gate: upgrade a
machine with live Claude + Codex agents on `~/.claude`: agents keep working until all are
stopped, migrate, restart, roster intact on both harnesses. ≈ 1.5 days.

## 7. KNOWN-INTENTIONAL (settled — not bugs to fix during implementation)

- `TEAMMATE_HARNESS` is the only harness signal; no clientInfo detection.
- `experimental.claude/channel` is advertised on every harness.
- Codex wake latency is whatever `codex queue` gives; no re-nudge on Codex; a wake requested
  while one is in flight is dropped (the queued message covers it).
- The wake text arrives as a user turn on Codex; content stays the WP-11a signal-only line.
- A same-name re-register that changes nothing (or only `harness_session`) no longer
  re-seeds nudge state `[R2]`; it still forces an immediate heartbeat + muted refresh `[F9]`.
- `harness_session` is cleared (stored `None`) by any register call that omits it `[R9][F5]`.
- Every non-human roster/profile entry shows a `harness:` line, `(not set)` for pre-upgrade
  records `[R11][F6]`.
- A Codex wake dropped because one is in flight is retried on the next tick, never retired
  `[F1]`; the `migrate-root` lock serialises migrations only, not store writes `[F3]`.
- Code review (2026-09-08, APPROVE-WITH-CHANGES): identity + `harness_session` are now
  applied and snapshotted under ONE lock (`Identity.apply`/`snapshot_all`); a
  `harness_session` starting with `-` is rejected; the one-in-flight rule is per waker
  INSTANCE — a session change mid-wake may briefly run two `codex queue` processes
  targeting different threads (accepted); a custom `runner` that ignores its timeout would
  jam the waker (the default `subprocess.run` honours it); `_json_field` relies on the hook
  payload carrying each key once at top level (pinned by the decoy test).
- A Codex child's `spawned_by` is not recorded.
- No template renderer; the SKILL harness section + lints are the drift guard.
- Codex first session after install/update has no server; first session after a hook change
  runs without the hook. Documented, not worked around.
- `codex mcp add` re-registration clobbers user-added env keys on the entry until
  vibe-cognition's WP-P3 preserve-env step exists; we copy it when it lands.
- Hooks write `~/.codex/config.toml` only through `codex mcp add`.

## 8. Risks

1. Codex churn: `codex queue` is three weeks old. README pins "verified on 0.153.4"; the
   wake breadcrumb makes a silent CLI change diagnosable.
2. `codex` not on the MCP child's PATH → `rc≠0`/not-found breadcrumb + a doctor line
   "wake CLI not found" in `teammate_whoami verbose`.
3. Agent omits `harness_session` → no wake, inbox still works; mitigated by the hook line,
   the `session_hint`, and the SKILL note.
4. Marketplace dedup (dev + coltondyck): first-seen wins; README says remove the dev one.
5. Windows `cmd.exe` hook quoting — inherited fix; shell test on Git Bash + human gate.
6. Divergence from vibe-cognition's eventual renderer — accepted; convergence is mechanical.
7. `[R2]` behavioural change to same-name re-register: pinned by a test; if the field gate
   shows a post-compaction case that relied on the reseed, revert to bumping both generations
   and accept the swallow window instead.

## 8a. Rulings (Colton, 2026-09-08) — supersede §4.5, §4.7, §5/§6 sequencing

1. **Root move is AUTOMATIC** (decision `a4f07bf8007f`): no `migrate-root` command. The
   first new-version server to start moves `~/.claude/TeammateComms` →
   `~/.teammate-comms/TeammateComms` when NO agent under the old root is live
   (`is_channel_alive`) or heartbeat-young (< 60 s), under a `.migrate.lock`; while that
   guard refuses, new-version servers keep USING the old root (existing-root-wins, so a
   rolling upgrade never splits the team) and retry on later starts; a `MIGRATED.json`
   marker at the old location records the move and the doctor reports "legacy root present,
   move deferred (live agents)" vs "migrated to <root>".
2. **`CLAUDE_CONFIG_DIR` dropped** (decision `b31ca28ffd89`): explicit arg →
   `TEAMMATE_COMMS_DIR` → `~/.teammate-comms`; `$CLAUDE_CONFIG_DIR/TeammateComms` is
   consulted only as a legacy migration SOURCE when the variable is present.
3. **Renderer adopted now** (decision `ddc42c241326`): `tools/render_harness.py` copied from
   vibe-cognition `8fc5230` minus its agent-role render targets (no `agents-src` here; the
   overlapping code is byte-identical and will be re-synced from Vince's WP-P3 commit);
   its lints carried into `tests/test_codex.py`; `skills-src/teammate-comms/SKILL.md`
   is the source; renders to `skills/teammate-comms/SKILL.md` (Claude) and
   `adapters/codex/skills/teammate-comms/SKILL.md` (Codex, manifest points there); the
   harness table's field names track vibe-cognition's (`display_name`, `skill_prefix` +
   `skill_invoke()`, `spawn_tool`, `default_models`). §4.5's harness-notes section becomes
   `{{harness:…}}` blocks; the literal lint moves onto the source.
4. **One combined release** (decision `34a6e0f1329c`): v0.16.0 ships Codex parity AND the
   root move; the `harness:` line convention stands. The human gate gains a migration phase.

## 9. Decisions needed from Colton (RULED — kept for the record)

1. WP-47 migration shape: explicit `migrate-root` command that refuses while agents are live
   (recommended) vs. automatic move on first new-version start.
2. Drop `CLAUDE_CONFIG_DIR` from root resolution in v0.17.0 (recommended) vs. keep it as a
   Claude-only fallback.
3. Approve the no-renderer skill approach (harness section + lints) for this repo.
4. Release split v0.16.0 (Codex) then v0.17.0 (root move) — or one combined release.
5. `[R11][F6]` Accept the new `harness:` line in roster/profile output for every non-human
   record, `(not set)` until an agent re-registers (recommended) vs. hide it for the default
   harness.
