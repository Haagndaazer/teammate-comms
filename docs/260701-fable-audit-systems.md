# Fable Audit — Stage 1: Per-system audit — 2026-07-01

> Fact-finding only. Nothing in this document has been fixed; every item is a *potential* task for a future implementation agent. Tasks are filed in the cognition graph under the `fable-audit epic (260701)` (node `291c795d20af`).

## Intended purpose (confirmed with human)

teammate-comms is a Claude Code plugin (pure-stdlib MCP server) that lets independent, human-launched full Claude Code instances message each other and wake each other from idle — closing the gap that built-in `SendMessage` only covers parent→subagent. It provides identity/registration, DMs, groups, reactions, profiles, a local dashboard where the human is a first-class teammate, and opt-in "reincarnate" spawning. **Audit yardstick (human-confirmed): THIRD-PARTY ADOPTERS** — a brand-new Claude Code user/team installing from the marketplace with zero context. Definition of success: install the plugin, register via `teammate_register`, and reliably send/receive/wake across two or more full instances (cross-project, cross-OS) with no polling and no ports.

## Scope of this stage

Each system audited individually, on its own terms, by a dedicated Sonnet 5 subagent (8 total, run in parallel): (1) server core (`server.py`, `instructions.py`); (2) storage (`comms.py`); (3) MCP tools (`tools.py`); (4) watcher + spawn (`channel.py`, `spawn.py`); (5) dashboard + avatars (`dashboard.py`, `static/index.html`, `avatars.py`); (6) hooks/packaging/CI (`hooks/`, `.claude-plugin/`, `pyproject.toml`, `ci.yml`); (7) test suite (`tests/`); (8) documentation accuracy (README, DESIGN, CHANGELOG, SKILL.md, root WP/AUDIT/BACKLOG docs). Every finding was researched against the vibe-cognition graph to separate deliberate decisions from oversights.

## Findings

### Server core

#### S1. Non-dict JSON-RPC message kills the whole server  [severity: critical]  [type: bug]
- **What:** The main loop guards `json.loads` but not `handle(msg, ctx)`. Any syntactically valid JSON line that isn't an object — a bare scalar, `null`, or a spec-legal JSON-RPC batch array — raises `AttributeError` on `msg.get(...)`, uncaught, terminating the process.
- **Evidence:** `server.py:223-252` (`handle` assumes dict), `server.py:346-354` (no try/except around `handle`).
- **History (vibe-cognition):** Oversight. Sibling paths were deliberately hardened — tool dispatch in WP-5 (`1c85d4c07498`), watcher thread in WP-12 (`8c32a9cca5d8`, `0ac62810eeee`) — but the envelope parser never got the same treatment. Matches the project's own recorded blind-spot pattern `8def32b8ff63` ("when one input path is guarded, audit ALL sibling paths").
- **Impact:** This is the component whose whole job is "stay alive to wake the human/agent." One unusual stdin line silently kills the channel; the adopter just stops receiving wakes.
- **Root cause / Fable's read:** The hardening campaign was call-site-driven, not pattern-driven; the outermost seam was skipped precisely because it looked too simple to fail.

#### S2. Registration name-collision is invisible to the caller  [severity: high]  [type: blindspot]
- **What:** Registering a name already owned by another *live* channel only writes a stderr `log(...)`; the tool returns an unconditional success string. Found independently by two auditors (server + tools).
- **Evidence:** `server.py:180-187` (log-only), `server.py:216-220` (unconditional success); `tools.py:524-534` forwards it verbatim.
- **History:** Constraint `0ff2595c61ef` (global unique names) and decision `ef4af8135c03` (global root) are deliberate; the *silence* of the guard has no recorded rationale — oversight.
- **Impact:** Two instances picking the same obvious name ("claude", "dev") is the single most likely first-run mistake for a new team; today it produces silent identity hijack with a success-looking response. Directly breaks the confirmed success bar.
- **Root cause / Fable's read:** Diagnostics were designed for the author-operator (who reads stderr debug logs), not for the in-conversation agent. See theme 3 below — this failure mode recurs across the whole codebase.

#### S3. `protocolVersion` is echoed, not negotiated  [severity: medium]  [type: broken-assumption]
- **What:** `initialize` reflects the client's version string back instead of answering with a version the server actually implements.
- **Evidence:** `server.py:227-237` (`params.get("protocolVersion", "2025-06-18")`).
- **History:** Hand-rolled JSON-RPC was deliberate (`b8f4fab73a85`); version negotiation was an unexamined consequence — oversight.
- **Impact:** Latent forward-compat trap: a future Claude Code with an incompatible protocol gets a false compatibility claim instead of a clean handshake failure.

#### S4. Auto-register from `$TEAMMATE_AGENT` fails silently  [severity: medium]  [type: blindspot]
- **What:** A `CommsError` during env-driven auto-register (the reincarnate child path) is logged to stderr only; the child looks alive to its parent but never appears in the roster.
- **Evidence:** `server.py:255-265`; `instructions.py:19-28` gives no signal that an expected auto-register failed.
- **History:** `$TEAMMATE_AGENT` as convenience auto-register is deliberate (`d6f6652ac59e`); the silent-failure branch is an oversight.
- **Impact:** Undermines the automated spawn path specifically — the one flow with no human watching.

#### S5. Notification-vs-request handling inconsistent across branches  [severity: low]  [type: bug]
- **What:** The unknown-method branch correctly stays silent for id-less notifications; the `initialize`/`ping`/`tools/list`/`tools/call` branches respond unconditionally, emitting a spec-irregular `"id": null` frame if ever hit as notifications.
- **Evidence:** `server.py:227-249` vs `server.py:250-252`.
- **History:** No graph history — incomplete generalization.

#### S6. "No polling loop is needed" overstates the wake guarantee  [severity: low]  [type: unclear-instruction]
- **What:** `instructions.py:27-28` states wake delivery as unconditional fact; the mechanism is actually lossy-push + capped re-nudge (Claude Code drops are real: GH #38736/#61797, node `d5f5553bffb3`).
- **History:** **Deliberate and validated** — caveat language dropped for token cost (`097c03396bd4`), zero interruptions in production soak (`eed4481d364d`). Noted for the third-party lens only; see also X4 (SKILL.md says something stronger and stale).

### Storage (`comms.py`)

#### C1. Hottest read path uses the destructive reader, unlocked  [severity: high]  [type: bug]
- **What:** `teammate_inbox` reads `{agent}_unread.json` — the highest-contention multi-writer file — via `read_json_safe`, which **resets the file to `[]` on any read failure**, with no lock held. The watcher reads the same file via the non-destructive `read_json_readonly`.
- **Evidence:** `tools.py:667` vs `channel.py:266`; reader semantics at `comms.py:440-465`.
- **History:** Oversight — worse, AUDIT-v0.7.0.md's "Verified solid" section explicitly (and incorrectly) claims destructive reads only ever happen on owned files under a held lock. The claim was never true for this call site (git blame: predates the audit, untouched by WP-7).
- **Impact:** On any filesystem where rename isn't truly atomic (network shares, OneDrive-synced `~/.claude` — common on Windows), a transient read failure silently wipes a live agent's queued inbox. Directly attacks "reliably receive."
- **Root cause / Fable's read:** An audit claim was recorded as verified without enumerating call sites; everything downstream trusted it.

#### C2. Unbounded full-scan of global `reactions.jsonl` on the hottest calls  [severity: high]  [type: bug]
- **What:** `_handle_inbox` and group-history call `read_reactions(root, team)` with no `limit`, which bypasses the tail-read fast path and parses the entire file every call. The file lives on the global cross-project root and grows forever (no compaction — see C6).
- **Evidence:** `tools.py:726`, `tools.py:1310`; fast-path gate at `comms.py:1242-1268`.
- **History:** Oversight — WP-7 (C-1/C-2/C-3 cluster, `aa86a43bd329`, `784557979df8`) fixed this exact anti-pattern in `react()`/`resolve_message()` but missed the two most frequent call sites.
- **Impact:** Every `teammate_inbox` call on the machine gets linearly slower for everyone, forever.

#### C3. `append_group_message` rewrites the whole group history per message  [severity: medium-high]  [type: gap]
- **What:** Unlike every other event store (O(1) NDJSON appends), group posts do read-array → append → rewrite-full-file, and `groups/*/messages.json` is never compacted.
- **Evidence:** `comms.py:401-420`.
- **History:** Oversight — the WP-7 scale audit never mentions group message storage.

#### C4. Naive-local-time message ids contradict the documented cross-host mode  [severity: medium-high]  [type: broken-assumption]
- **What:** Ids are naive local timestamps commented "writer and reader are always co-located," while DESIGN.md documents a supported cross-host shared root (with host-gated lock-steal built specifically for it). All cursors/ordering assume monotonic ids across writers.
- **Evidence:** `comms.py:33-35` vs `DESIGN.md:670-676`.
- **History:** Cross-host locking was deliberately hardened (WP-6/A-7, `587f114aabab`); id ordering under clock skew was never analyzed — gap.
- **Impact:** Two machines with modest clock drift on a shared root can permanently, silently skip messages past already-advanced cursors.

#### C5. Global flat namespace: offline collisions have no guard at all  [severity: medium]  [type: broken-assumption]
- **What:** The only name-collision protection is against *currently live* channels; an offline teammate's identity can be silently adopted by an unrelated project's agent.
- **Evidence:** `comms.py:221-250`; constraint `0ff2595c61ef`.
- **History:** The global namespace is deliberate (`ef4af8135c03`); the adoption cost for zero-context newcomers was never revisited against the third-party yardstick.

#### C6. Transcript/reactions logs have no compaction (deferred half of C-2)  [severity: medium]  [type: gap]
- **What:** WP-7 gave deletions real compaction and gave transcript/reactions only read-side tail optimization; disk growth is unbounded.
- **Evidence:** `comms.py:1301-1397` (deletions treatment has no transcript/reactions analog).
- **History:** Partially deliberate deferral of AUDIT-v0.7.0 C-2 — still open.

### MCP tools (`tools.py`)

#### T1. Any teammate can overwrite or clear another teammate's avatar  [severity: high]  [type: bug]
- **What:** `teammate_set_avatar`'s `agent` param defaults to the caller but is never checked against the caller — format-validated only. Inconsistent with `teammate_update` (self-only by schema construction).
- **Evidence:** `tools.py:1010-1029` vs `tools.py:960-971`.
- **History:** WP-14 locked down ingestion security thoroughly (`42885e93a464`, `11176334c5c1`) but never discussed write-target authorization; not in the "known-intentional" list — oversight.
- **Impact:** Silent griefing vector with no audit trail; violates the self-owned-profile expectation the rest of the surface establishes.

#### T2. `unread.json` has no size cap — the C-3 fix only covered the acked log  [severity: medium]  [type: gap]
- **What:** `_READ_CAP` (1000) bounds `_read.json`, but the live unread queue that every sender appends to is never capped.
- **Evidence:** `tools.py:85-87`, `798-806` (cap) vs `tools.py:598-610`, `1088-1105` (uncapped appends).
- **History:** Half-fix; no node discusses an unread cap. The audit's own rationale applies *more* to this file.

#### T3. Authorization model is inconsistent and mostly convention-only  [severity: medium]  [type: blindspot]
- **What:** `teammate_update` is self-only by construction; `teammate_delete` lets any agent remove any offline teammate; `project_register`/`project_delete` accept any `key` with a "by convention" sentence and zero enforcement. An adopter cannot predict which tools are safe defaults from their descriptions.
- **Evidence:** `tools.py:1460-1488`, `1611-1647`, `1770-1782`.
- **History:** Flat local trust is deliberate (`d30a0bc88264` "trust model"); the *inconsistency* and its non-documentation are the finding.

#### T4. Token-efficiency of tool outputs  [severity: low]  [type: gap]
- Already tracked by open task `ee9f6d52b059` (token-efficiency pass #2). Evidence confirmed at `tools.py:933-957`, `809-875`. No new task filed.

### Watcher + spawn (`channel.py`, `spawn.py`)

#### W1. Reincarnate hardcodes the `coltondyck` marketplace; failure is indistinguishable from success  [severity: high]  [type: broken-assumption]
- **What:** `PLUGIN_SPEC`/allowlists/`channel_allowlisted()` all pin `MARKETPLACE = "coltondyck"`. Any fork/rehost (standard enterprise adoption) makes every spawn load a nonexistent plugin ref; child stdio is DEVNULL by design, so the child simply never registers — which the tool's own success text admits "looks identical to success."
- **Evidence:** `spawn.py:30-34`, `56-82`; `tools.py:1550-1554`.
- **History:** Oversight — graph only covers the canonical marketplace consolidation (`468a128ef82d`, `91f6219715f4`); never revisited for forks. The `$TEAMMATE_LAUNCH_ARGS` escape hatch exists but is documented nowhere as the remedy.

#### W2. The reincarnate opt-in gate is defeated by the normal way users persist env vars  [severity: high]  [type: broken-assumption]
- **What:** `TEAMMATE_REINCARNATE_ENABLED` is checked per-process with no session scoping; `setx`/profile `export` (the natural way to make it stick) silently enables process-spawning for every future session machine-wide.
- **Evidence:** `tools.py:1505-1516`; `spawn.py:131-135` strips it only for the child.
- **History:** This exact scenario already happened — incident `c1fa517c047d` (2026-06-10); the fix was OS-level cleanup, no code mitigation was ever added. Deliberate-not-to-fix at the time, but the root cause it names is unresolved.

#### W3. Re-nudge caps at 3 attempts (~14 min) — a lone message under sustained drops goes silent forever  [severity: medium]  [type: gap]
- **What:** `compute_reemit` stops after `REEMIT_MAX_ATTEMPTS=3` and never resets for a still-unseen batch; only a *new* message re-triggers a wake carrying the old ids.
- **Evidence:** `channel.py:49-50`, `139-174`.
- **History:** **Deliberate** (WP-9, `5004a641bd82`) to bound noise/cost; residual risk acknowledged in code, not fully mitigated. Relevant to the "no polling" promise.

#### W4. Watcher identity snapshot and generation read are two separate lock acquisitions  [severity: medium]  [type: bug]
- **What:** `snapshot()` then `get_generation()` each take `Identity._lock` independently; a `set()` landing between them pairs a stale root/inbox with a new generation, firing the WP-12 reset against the wrong inbox for one tick.
- **Evidence:** `channel.py:215-216`; `server.py:73-97`.
- **History:** Generation design deliberate (`f47c2608e76d`); the non-atomic pairing is a residual oversight in an already-firefought area.

#### W5. Spawned children are never reaped — zombie accumulation on POSIX  [severity: medium]  [type: gap]
- **What:** Every launch path fires `subprocess.Popen` and drops the handle; no wait/poll ever. A long-lived server accumulates defunct children between restarts.
- **Evidence:** `spawn.py:141-183` (Popen at 153, 161-162, 174-175).
- **History:** No graph history — oversight.

#### W6. `TEAMMATE_LAUNCH_ARGS` parsed with POSIX `shlex.split` on Windows  [severity: medium]  [type: bug]
- **What:** Default `posix=True` treats backslash as escape; an unquoted Windows path in the override (`C:\Users\name`) is silently corrupted (`C:Usersname`) — in a module whose docstring says "Windows-first," on exactly the path a forked-marketplace adopter needs (compounds W1).
- **Evidence:** `spawn.py:94-98`.
- **History:** No graph history — oversight.

#### W7. Heartbeat write failures are silently dropped  [severity: low-medium]  [type: blindspot]
- **What:** `write_agent_record` returns False on lock timeout; `run_watcher` ignores the return. Under contention a live agent can look stale to `is_channel_alive`, which reincarnate's collision guard trusts.
- **Evidence:** `channel.py:239-243`; `comms.py:801-826`; `tools.py:1524-1529`.
- **History:** No graph history — oversight.

### Dashboard + avatars

#### D1. Two dashboards defaulting to "human" silently merge into one identity  [severity: high]  [type: blindspot]
- **What:** The human name defaults to `$TEAMMATE_HUMAN_NAME` or literally `"human"` with no uniqueness/ownership check; `register_human` field-merges into the shared record. Two dashboard processes (second instance, second human) share one inbox and flap each other's presence on shutdown.
- **Evidence:** `tools.py:1565-1569`; `comms.py:896-914`, `801-825`.
- **History:** Single-human design throughout (`a9594b942f2b`, `d2e839d898bb`); multi-dashboard collision never considered — oversight.
- **Impact:** The multi-instance scenario the plugin targets, failing invisibly for the human's own identity.

#### D2. Avatar sidecars are never garbage-collected; `/avatar` serves without a registration check  [severity: medium]  [type: gap]
- **What:** `remove_teammate` unlinks the record and inboxes but not `avatars/<name>.*`; the dashboard serves whatever file exists for a shape-valid name. A removed teammate's image stays fetchable; a name-reuser can inherit a stranger's old avatar via direct URL.
- **Evidence:** `comms.py:767-778`; `dashboard.py:219-243`.
- **History:** WP-14's known-intentional list doesn't mention removal/GC — oversight.

#### D3. `image_base64` is fully decoded before any size bound  [severity: medium]  [type: gap]
- **What:** `b64decode` materializes the entire buffer before the 50MB check; the base64 *string* is never length-checked, and the stdin JSON-RPC path has no message-size cap of its own (unlike the dashboard's 1MB HTTP cap).
- **Evidence:** `avatars.py:166-180`; `server.py:340-351`; contrast `dashboard.py:29-32`, `198-200`.
- **History:** WP-14 brief says "50MB byte cap before decode" (`de7f0a21781e`) — the shipped placement arguably violates the brief's own wording. Oversight.

#### D4. Dashboard poll/roster failures are swallowed — UI silently freezes  [severity: medium]  [type: blindspot]
- **What:** `poll()` and roster load use bare `catch (e) {}`; a stale token after server restart (or any 401/500) yields a hung-looking console with zero diagnostic. WP-4 added error surfacing for *write* actions only.
- **Evidence:** `static/index.html:505-526`, `238-256`, `529`.
- **History:** WP-4 (`41d37ae0b2b2`) scoped to writes; never generalized — oversight.

#### D5. Verified clean (for the record): WP-14 path-traversal fix present (`dashboard.py:224-228`); XSS clean (all DOM writes via `textContent` + strict CSP); CSRF defused (custom header required); token-in-query known-intentional.

### Hooks, packaging, CI

#### P1. Missing `uv` = cryptic first-contact failure; the friendly warning and the actual spawn are disconnected mechanisms  [severity: critical]  [type: broken-assumption]
- **What:** `plugin.json` spawns via `"command": "uv"`. The SessionStart hook emits a helpful install-uv warning but exits 0 and cannot affect the independent `mcpServers` spawn, which fails with whatever generic error Claude Code shows for a missing binary.
- **Evidence:** `.claude-plugin/plugin.json:9-14`; `hooks/session-start.sh:47-50`.
- **History:** No graph history on the spawn-vs-hook disconnect — the team designed the warning but never traced what the user actually sees. Oversight.
- **Impact:** Blocks the confirmed success definition at step 0 for exactly the target persona.

#### P2. Avatars are dead on arrival for every adopter, and the error text prescribes an impossible fix  [severity: high]  [type: gap]
- **What:** Pillow sits behind the `images` extra; `session-start.sh` runs `uv sync --no-dev` with no `--extra images`, so no adopter ever has it. The `CommsError` says `pip install teammate-comms[images]` — the package isn't on PyPI and the runtime is a uv-managed plugin-cache venv, so the remediation cannot work. README never mentions Pillow or avatars-enablement at all.
- **Evidence:** `hooks/session-start.sh:66`; `avatars.py:150-158`; `pyproject.toml:14-15`.
- **History:** Zero-dep hot path deliberate and correct (`de7f0a21781e`); the adopter install story for the extra was never closed — oversight riding on a good decision.

#### P3. Missing Git Bash on Windows cascades into the same undiagnosable failure  [severity: medium]  [type: broken-assumption]
- **What:** `hooks.json` invokes `bash` directly with no existence check; without it the hook never runs, the venv is never built, and the MCP spawn fails against an unsynced venv. README assumes bash "is on PATH if you already run other bash-hooked plugins" — false for a first-plugin adopter.
- **Evidence:** `hooks/hooks.json:9,19`; `README.md:200-203`.
- **History:** No graph history — oversight.

#### P4. CI has no macOS leg despite darwin-specific code paths  [severity: high]  [type: gap]
- **What:** Matrix is ubuntu+windows only; `spawn.py` has explicit `darwin` branching (managed-settings location) and README documents macOS paths — all unexercised.
- **Evidence:** `.github/workflows/ci.yml:20-27`; `spawn.py:42-51`.
- **History:** No recorded trade-off (WP-3 `d223ba0f38e7` added the two-OS matrix without macOS discussion) — oversight.

#### P5. AUDIT G-4 (hook matcher uncertainty) was recorded MERGED but only half-closed  [severity: medium]  [type: bug]
- **What:** `session-start.sh` got a defensive stdin self-filter; `reinject-instructions.sh` (the `matcher: "compact"` hook) has none and no test proves the matcher contract. A ticket marked closed while the flagged uncertainty is still live.
- **Evidence:** `AUDIT-v0.7.0.md:344-354`; `hooks/session-start.sh:11-15`; `hooks/reinject-instructions.sh` (no stdin read); `tests/test_handshake.py:1806-1816`.
- **History:** BACKLOG marks G-4 merged @ `e9cd571` — tracking/closure gap.

### Test suite

#### Q1. CI runs one of the three test suites  [severity: critical]  [type: gap]
- **What:** `ci.yml` invokes only `test_handshake.py`. The WP-13 (project profiles) and WP-14 (avatars) acceptance suites — 1,183 LOC of tests for the two newest features — run only when a human remembers.
- **Evidence:** `.github/workflows/ci.yml:41-42`; no reference to the other suites anywhere in CI.
- **History:** Known blind spot, compensated by the manual manager diff-gate (`c074742c6570`: four defects "passed CI/unit-tests yet were caught only at the gate"). The gate does not exist for third-party contributors — acknowledged oversight, never closed.

#### Q2. WP-13 suite hardcodes a personal temp path — it cannot run on any other machine  [severity: high]  [type: bug]
- **What:** `tempfile.TemporaryDirectory(prefix="tc-wp13-", dir="C:/cctmp")` — the author's personal scratch root. Fresh clone, contributor box, or CI runner: `FileNotFoundError` before a single assertion.
- **Evidence:** `tests/test_wp13_projects.py:411` (present since first commit `6e4f322`); reproduced live.
- **History:** Never called out anywhere in the graph — oversight, and direct proof of Q1 (nothing runs this file).

#### Q3. Version-sync guards are themselves stale  [severity: high]  [type: bug]
- **What:** Both un-wired suites assert the version equals the literal `"0.10.0"`; the repo shipped 0.11.0. The check designed to catch drift has drifted — live run fails on perfectly-synced files.
- **Evidence:** `tests/test_wp13_projects.py:649`; `tests/test_wp14_avatars.py:441`; reproduced live.
- **History:** Mechanical consequence of Q1. (Note: `test_handshake.py:2895-2902` has a cross-file version assertion that *does* run in CI — the pattern to copy.)

#### Q4. `avatars.py` error/edge paths have zero test coverage  [severity: medium]  [type: gap]
- **What:** Oversize source, invalid base64, corrupt image, zero-dimension, decompression-bomb guard — none tested; only happy paths + Pillow-absent.
- **Evidence:** `avatars.py:169-194` branches vs `test_wp14_avatars.py` (AC-1/2/3 only).

#### Q5. All concurrency coverage is thread-based; the actual product shape (multi-process) is never tested  [severity: medium]  [type: blindspot]
- **What:** Contention tests use `threading.Thread` in one interpreter. Partially reasonable (the mkdir lock is a filesystem primitive, and a thread test did catch a real Windows bug — `f05346eddb2b`), but separate-process contention — the core promise — is inferred, never proven.
- **Evidence:** `test_handshake.py:1265-1276`; `test_wp13_projects.py:223-226`.

#### Q6. Positive notes: WP-15 suppression is thoroughly tested (T1–T8); the timing-tautology anti-pattern was already found and fixed via clock injection (`b91127e`); dashboard HTTP surface well covered; WP-11b fixture contamination remediated.

### Documentation accuracy

#### X1. The 18th tool (`teammate_set_avatar`) is missing from every tool table  [severity: high]  [type: gap]
- **What:** README, SKILL.md, and DESIGN.md all list 17 tools; DESIGN's count line still says "17 tools." WP-14's own plan (§11) committed to these updates and they never happened.
- **Evidence:** `tools.py` (18 definitions) vs `README.md:17-38`, `SKILL.md:13-31`, `DESIGN.md:326`.
- **History:** `11176334c5c1` records "v0.10.0, 18 tools"; no decision to omit — oversight.

#### X2. The current release's headline feature (inbox body-suppression / `show_all`) is absent from README  [severity: high]  [type: gap]
- **What:** Default-on suppression means re-read bodies silently vanish; README's `teammate_inbox` row lists only `count_only?`. Only SKILL.md documents it.
- **Evidence:** README inbox row vs `tools.py` `_handle_inbox` schema; `6410dd919f0d`/`9918603e5d5a` show SKILL-only updates.
- **History:** Oversight (README not touched in the WP-11b/WP-15 doc commits).

#### X3. `teammate_list` project-scoping default undocumented in README/DESIGN  [severity: high]  [type: unclear-instruction]
- **What:** Defaults to caller's-project-only (pass `all=True` for global) — directly contradicting README's "comms are global by default" framing; peers in other projects silently vanish.
- **Evidence:** `tools.py` teammate_list description vs `README.md:25`, `DESIGN.md:334`.

#### X4. SKILL.md still states the superseded wake guarantee  [severity: high]  [type: broken-assumption]
- **What:** SKILL.md's Reliability contract claims "a dropped/missed channel push never loses a message — it is read on the next teammate_inbox," the exact framing DESIGN §7 was deliberately rewritten (WP-9 "honest contract") to retract — an idle agent never calls inbox unprompted, and re-nudge caps at 3.
- **Evidence:** `SKILL.md:106-116` vs `DESIGN.md:265-288`; WP-9 scope (`5004a641bd82`) named DESIGN only.
- **History:** Oversight — SKILL.md was never in the WP-9 rewrite scope. Critically, SKILL.md is the surface actually loaded into adopters' agents.

#### X5. DESIGN.md version framing re-drifted after being fixed once  [severity: medium]  [type: gap]
- **What:** Header says "reconciled to v0.7.1," embedded plugin.json example says 0.7.1; shipped is 0.11.0. Same failure AUDIT E-2 flagged and WP-2 fixed — the fix wasn't durable because no process ties DESIGN to version bumps.
- **Evidence:** `DESIGN.md:8,113` vs `plugin.json:2`.
- **History:** `1d8256a1b1ee` already flags DESIGN as stale; `b3ef555d9ca9` the one-time fix.

#### X6. WP-14/WP-15 have no real DESIGN.md sections (one-line bullets); `avatars.py` absent from DESIGN's repo layout  [severity: medium]  [type: gap]
- **Evidence:** `DESIGN.md` grep "avatar" = 1 hit (line 760); §2 layout lacks avatars.py; WP-14 plan §11 committed to a DESIGN section.

#### X7. No git tags for any release  [severity: low]  [type: gap]
- **Evidence:** `git tag --list` empty vs 8+ CHANGELOG version headers. Adopters can't check out a release; SHAs must be reverse-engineered from pin commits.

#### X8. Root-level WP/BACKLOG/AUDIT/LEDGER files are untracked, mostly without disclaimers  [severity: low]  [type: blindspot]
- **What:** Only AUDIT-v0.7.0.md self-labels as deliberately uncommitted; BACKLOG.md and WP-13/14/15 carry no such banner, so a contributor can't distinguish manager scratch-space from forgot-to-commit. Also duplicates the project's own "graph is the backlog" rule.
- **History:** Deliberate-by-convention, undocumented as such.

## Summary & Recommendations

Seven root-cause themes explain nearly all 40+ findings:

1. **The first-contact path was never walked on a clean machine.** P1 (no uv), P3 (no bash), P2 (Pillow extra never installable), W1 (hardcoded marketplace) are one theme: every install/spawn dependency assumption holds on the author's machines and on no one else's, and every failure is silent or cryptic. This is the single largest threat to the confirmed intent — the adopter is blocked or misled *before the first message is ever sent*. Recommendation: a "clean-machine adoption test" (VM or CI job with no uv/bash/Pillow) plus actionable in-band failure messages.

2. **Diagnostics go where the author looks, not where the user is.** S2, S4, W7, D1, D4, W1's silent spawn — collisions, failed registrations, failed heartbeats, dead dashboards all report to stderr or nowhere. The conversation (tool return values) is the only place a third-party adopter will ever look. Recommendation: a standing rule — any failure or degraded state must surface in the tool's return text.

3. **Hardening was call-site-driven, not pattern-driven.** S1 (envelope unguarded while dispatch/watcher are), C1/C2 (WP-7 fixed the anti-pattern in some call sites, missed the hottest ones), T2 (cap applied to one of two files), P5 (one hook self-filters, its sibling doesn't). The graph itself already names this pattern (`8def32b8ff63`). Recommendation: when a defect class is fixed, sweep every sibling call site and record the sweep.

4. **The verification record can drift from reality — and did.** C1 sits under an explicit "verified solid" audit claim that was never true; Q3's version guards assert stale literals; P5 is closed-but-not-closed. Recommendation: treat "verified" claims as citations (file:line at claim time) and re-verify on structural change.

5. **CI protects one-third of the product.** Q1/Q2/Q3 + P4: two suites never run (and have rotted to non-runnable), macOS never tested. The compensating control — the manager diff-gate — is a process third parties don't get. Recommendation: wire all suites into CI (fixing Q2/Q3 en route), add macOS, and convert version literals to cross-file checks.

6. **Docs update asymmetrically per release; SKILL.md and README/DESIGN have swapped staleness.** X1-X6: SKILL.md got the WP-11b updates the README didn't; DESIGN got the WP-9 honesty rewrite SKILL.md didn't; nobody bumps DESIGN's version framing. Recommendation: a release checklist item — every tool/behavior change touches README + SKILL + DESIGN in the same PR (the WP-14 plan already required this and it silently didn't happen; the gap is enforcement, not intent).

7. **Scale and trust assumptions from the one-team era are load-bearing but undocumented.** C3-C6 (unbounded growth, clock skew), T1/T3 (flat trust, inconsistent enforcement), C5/D1 (name collisions). Some are fine to *keep* — but each should be either enforced or documented as an explicit limit; today an adopter discovers them by breakage.

## Potential tasks (checklist)

- [ ] Guard the JSON-RPC envelope: wrap per-message handling; reject non-dict/batch input gracefully (S1) — priority: critical
- [ ] Restore test-suite integrity: wire WP-13/WP-14 suites into CI; fix `C:/cctmp` hardcode; replace stale version literals with the cross-file check pattern (Q1, Q2, Q3) — priority: critical
- [ ] First-contact diagnostics: detect missing uv/bash and surface an actionable error where the adopter will see it (P1, P3) — priority: critical
- [ ] Surface identity collisions in-conversation: live collision at register, offline cross-project collision, dashboard human-name collision (S2, C5, D1) — priority: high
- [ ] Inbox read safety: read `unread.json` with the non-destructive reader (and/or under lock) in `_handle_inbox` (C1) — priority: high
- [ ] Bound reactions reads: pass `limit` at the inbox and group-history call sites (C2) — priority: high
- [ ] Restrict `teammate_set_avatar` target to caller/operator (T1) — priority: high
- [ ] Make avatars installable: ship or document the `images` extra path; correct the CommsError remediation text; README section (P2) — priority: high
- [ ] Un-hardcode the reincarnate marketplace spec; surface spawn failure to the caller (W1) — priority: high
- [ ] Session-scope the `TEAMMATE_REINCARNATE_ENABLED` opt-in (W2) — priority: high
- [ ] Add macOS to the CI matrix (P4) — priority: high
- [ ] Docs sync: add `teammate_set_avatar` + `show_all` + `teammate_list` scoping to README/DESIGN; fix DESIGN version framing; add a release doc-checklist (X1, X2, X3, X5, X6) — priority: high
- [ ] Rewrite SKILL.md's reliability contract to the honest WP-9 wording (X4) — priority: high
- [ ] Cap `unread.json` like `_read.json` (T2) — priority: normal
- [ ] Group message storage: O(1) append (NDJSON) or compaction (C3) — priority: normal
- [ ] Cross-host id ordering: document the limit or add a per-host sequence component (C4) — priority: normal
- [ ] Avatar lifecycle: GC sidecars on removal; registration check in `/avatar`; base64 length pre-check (D2, D3) — priority: normal
- [ ] Dashboard: surface poll/roster failures in the UI (D4) — priority: normal
- [ ] spawn.py Windows/POSIX fixes: `shlex.split(posix=False)` on Windows; reap spawned children (W5, W6) — priority: normal
- [ ] Watcher: atomic snapshot+generation read; act on heartbeat write failures (W4, W7) — priority: normal
- [ ] Test gaps: avatars error paths; one true multi-process contention scenario (Q4, Q5) — priority: normal
- [ ] `reinject-instructions.sh` defensive self-filter + matcher-contract test (P5) — priority: normal
- [ ] Surface `$TEAMMATE_AGENT` auto-register failure in-conversation (S4) — priority: normal
- [ ] Document the flat-trust authorization model; align the inconsistent tools (T3) — priority: normal
- [ ] Transcript/reactions compaction (C6) — priority: low
- [ ] Re-nudge cap residual: document the manual-recovery affordance (W3) — priority: low
- [ ] Negotiate `protocolVersion` instead of echoing (S3) — priority: low
- [ ] Tag releases in git (X7) — priority: low
