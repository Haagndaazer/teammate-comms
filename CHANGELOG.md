# Changelog

## v0.15.0

> **WP-42 — reading is acking.** `teammate_inbox` now immediately moves every message it
> shows into your read inbox; the unread inbox only ever contains messages you have never
> seen, so restarts no longer resurface stale "unread" messages. The `teammate_ack` tool is
> removed (breaking). `show_all` is replaced by `show_read: N`, which reads back your N most
> recent read messages (post-compaction recovery). Group read receipts are now true "read up
> to here" positions. Legacy per-agent `seen.json` files are swept into the read inbox on
> first contact and deleted. Accepted transient: between a post-upgrade registration and an
> agent's first `teammate_inbox` read, wake counts may over-count legacy previously-shown
> ids that haven't gone through the sweep yet — this self-heals on that first read.

## v0.14.0

**WP-41 — registration is now opt-in; the auto-register nudge is removed.** The MCP
`initialize` handshake no longer carries a standing-instructions block, and the
post-compaction re-injection hook (`hooks/reinject-instructions.sh`) is deleted along with
`teammate_comms.instructions`. Agents are no longer told to call `teammate_register` at
session start, so they no longer auto-register under arbitrary names. The toolset is fully
intact — register explicitly (`teammate_register`) or set `TEAMMATE_AGENT` to join. The
load-bearing venv-build SessionStart hook is unchanged.

## v0.13.1

- **WP-39 — `teammate_request_compact` alerts the requester's manager on a self-compact.**
  A self-compact request now best-effort DMs (sender `compact-broker`) the requester's
  registered manager, if one is set and resolves to a registered teammate — closing the
  gap where a subordinate's self-compact was silent to the manager who owns them. No
  change to the manager-compacting-subordinate leg or the frozen v1 request-file contract.

## v0.13.0

**Compaction-broker plugin support.** Three work packages (WP-36–WP-38) add the
plugin-side hooks a non-MCP compaction-broker daemon needs, fix-forward on a single
branch, fix+proof in the same commit throughout.

- **WP-36 — registration capture.** `register_identity` now captures `pane_id` /
  `wezterm_socket` from the WezTerm pane environment and an optional `manager` field
  (an explicit env var takes precedence over the param when the env value is valid;
  a malformed env value falls through to the param instead of vetoing it), exposed in
  `teammate_profile` / `teammate_list`. Passing an empty string explicit-clears a
  previously-set field; omitting the argument leaves it untouched.
- **WP-37 — `teammate_request_compact` tool (19th tool).** A new tool for requesting a
  compaction, with a server-stamped requester (never client-supplied), write-time authz
  (self or manager-of-target only), and an atomic write against a frozen cross-repo v1
  request-file JSON contract. The sender name `compact-broker` is now reserved at
  registration — an agent can no longer claim it, closing an in-band forgery path
  against the broker's own audit/completion DMs.
- **WP-38 — broker delivery CLI.** `python -m teammate_comms.deliver` wraps `send_dm`
  in-process so the non-MCP broker daemon can deliver completion/expiry notices as real
  teammate-comms DMs, with every storage guarantee (lock, unread cap, atomic write,
  transcript tee) for free. Server startup now drops a `plugin-runtime.json` invocation
  pointer (python executable, plugin root, version) for the broker to discover the CLI.

## v0.12.0

**Fable-audit hardening pass.** A full internal audit across protocol correctness,
identity/registration races, compaction correctness (reactions/deletions/transcript),
avatars lifecycle, spawn/marketplace detection, dashboard diagnostics, tool-surface
consistency, harness/hook contracts, and test-gap closure — 18 work packages
(WP-16–WP-33) fixed on a single branch, fix-forward, fix+proof in the same commit
throughout. **No breaking changes to the tool surface** (still 18 tools); two internal
correctness fixes cause one-time, one-way behavior shifts for existing installs — see
the "Changed" notes below.

### Fixed / hardened, by area
- **Protocol core (WP-16).** Malformed stdin no longer crashes the server (an envelope
  guard rejects non-object JSON-RPC frames before dispatch); `initialize` always answers
  with the server's OWN supported `protocolVersion` instead of echoing back whatever the
  client requested; notification-vs-request dispatch discipline tightened.
- **Storage & scale (WP-17, WP-25).** `teammate_list`'s project-scoping comparison is
  normalized (was comparing raw strings); the inbox unread read is now lock-protected;
  bounded reaction reads; the 1000-message unread cap now tags overflow with
  `acked_unseen` instead of silently dropping it; ack-without-a-prior-read receipts
  fixed; group storage moved to an NDJSON dual-store (matching the transcript's
  append-only design).
- **Test-suite integrity (WP-18).** CI now runs the harness on a 3-OS matrix
  (ubuntu/macos/windows); the harness's own crash-tolerance was hardened (a deliberately
  induced crash no longer silently passes).
- **Identity ownership (WP-19).** A new `instance_id` + monotonic `epoch` model closes a
  registration TOCTOU race between two processes claiming the same agent name; a
  same-name flap (rapid re-register) now kills the stale channel instead of both
  competing; a guard prevents an agent's auto-register from clobbering a human operator
  record of the same name.
- **Deletion correctness (WP-20).** `remove_agent` is now locked and VERIFIED — it
  reports which files failed to delete instead of silently claiming success on a
  Windows sharing-violation no-op; `teammate_reincarnate` carves out the human operator
  as never-reincarnate-able.
- **Presence & naming (WP-21).** Human dashboard presence now detects staleness (killing
  the terminal no longer leaves you "online" forever); **agent/group names are now
  case-folded at register/create (G2 — see Changed, below)**; Windows reserved device
  names (`con`, `prn`, `nul`, `com1-9`, `lpt1-9`) are now rejected on every OS.
- **Watcher robustness (WP-22).** Atomic identity+generation snapshot (closes a window
  where a mid-tick re-register used half-old/half-new state); heartbeat-failure retry;
  durable-seen seeding survives a restart; a recovery lane for fan-out messages that
  landed while the recipient's inbox was locked.
- **Spawn platform hardening (WP-23).** Fixed a Windows `shlex` quoting bug that
  corrupted paths in `$TEAMMATE_LAUNCH_ARGS`; spawned child processes are now reaped
  (no zombie accumulation); marketplace resolution and launch-args overrides are now
  named in the reincarnate response.
- **Reincarnate gate (WP-24).** Detects when `TEAMMATE_REINCARNATE_ENABLED` is set
  DURABLY (registry/user env, Windows-only, best-effort) and warns on every call, naming
  the fix; an auto-register failure is now surfaced through `teammate_whoami` instead of
  looking like plain non-registration.
- **Message ids & react (WP-26).** Message ids gained a collision-proof disambiguator
  (pid + a per-process counter) — a bare microsecond timestamp could collide across
  writers, silently merging two distinct messages under one id; `teammate_react` now
  uses the same 3-tier resolution (transcript → group files → inboxes) as
  `teammate_delete`, instead of only scanning the transcript.
- **Compaction & unbounded growth (WP-27).** `reactions.jsonl` and `transcript.jsonl` no
  longer grow forever — reactions gain a stateful compacted baseline (correctly folds a
  removed reaction, unlike a naive append-only union) and the transcript gets size-gated
  rotation. A gate-review fix (folded into the WP-31 commit) reordered the rotation to
  gate-THEN-append, so the record that triggers rotation always lands in the file that
  survives — it no longer risks silently dropping into the unread `.1` grace copy.
- **Avatars (WP-28).** `teammate_set_avatar` is now self-only (any other target is
  rejected); `teammate_delete(teammate:...)` now also removes the offline teammate's
  avatar sidecars (a name-reuser could otherwise inherit a stranger's image); an
  over-cap base64 payload is now rejected before decoding, not after; Pillow is now
  installable via `TEAMMATE_AVATARS_ENABLED=1` (auto re-syncs the plugin venv with the
  `images` extra) instead of a stale, non-working `pip install` instruction.
- **Dashboard diagnostics (WP-29).** A connection-status banner now distinguishes a
  dead session token (restart required) from a transient network hiccup (auto-retries);
  an empty transcript with `TEAMMATE_TRANSCRIPT=0` now says so instead of looking like a
  bug; `teammate_whoami(verbose:true)`'s doctor report gains `root_mismatches` (peers
  who resolved a different comms root — previously indistinguishable from "just
  offline"); **the default project label now prefers a git remote over the path (G3 —
  see Changed, below)**.
- **Harness/hook contracts (WP-30).** A first-time install now tells you to restart once
  (previously prose-only, invisible in-session); `reinject-instructions.sh` gained the
  same defensive stdin self-filter its sibling hook already had; the server and the
  reinjected instructions are now both version-stamped, so a stale-vs-current mismatch
  after a mid-session plugin update is diagnosable; the managed-settings schema's
  verification vintage is now logged when the allowlisted launch path is chosen.
- **Tool-surface polish (WP-31).** `teammate_inbox` and `teammate_group(history)` now
  share one message-block renderer (was two near-identical, drifting grammars);
  required-argument errors now say `'<param>' is required` instead of a confusing
  generic "Invalid ... None"; the 64 KB message cap and the `@mention` behavior are now
  disclosed in the tool schemas (previously SKILL.md only); all 10 `teammate_group`
  actions are now named in its description (was 7); groups are now documented as open
  (no privacy) instead of implying Slack-style membership secrecy.
- **Test coverage (WP-33, test-only).** `avatars.py`'s error paths (oversize source,
  invalid base64, corrupt image bytes, a decompression bomb) now have dedicated
  coverage with specific-reason assertions; a new multi-PROCESS lock-contention test
  proves cross-process exclusion (all prior concurrency coverage was thread-based in
  one interpreter).

### Changed — one-time, one-way shifts for existing installs
- **Case-variant names are now rejected at register/create (G2, WP-21).** Existing
  records that already differ only by case (e.g. two teammates named "Bob" and "bob"
  from different OSes) are NOT retroactively merged, but a *new* register/create using a
  case-variant of an existing name is now rejected (the error names the existing
  spelling) instead of silently creating a divergent duplicate. Re-registering the exact
  existing spelling is unaffected.
- **Default project label now prefers the git remote (G3, WP-29).** An agent
  re-registering inside a git repo with an `origin` remote gets a *new* default project
  label (`owner/repo`, from the remote URL) instead of the old path-derived
  `parent/name` label — a one-time roster shift the first time each agent re-registers
  after upgrading. `list_projects`' near-miss section surfaces any resulting stray;
  re-key an existing project profile with `project_register(key:...)` if needed.
  Explicit `project` args at register time always win and are unaffected.

### Housekeeping
- **DESIGN.md:** version framing bumped to v0.12.0; a new "Release doc-checklist" note
  (the enforcement that was always missing — every tool/behavior change touches
  README+SKILL+DESIGN in the same commit); the trust model section gains the
  convention-gated-tools enumeration (which tools are self-only vs. open-by-convention);
  a proper Avatars design section; a dashboard bookmark-403 lifecycle clarification;
  `avatars.py`/`instructions.py` added to the repo-layout tree.
- **README.md:** new Quickstart (a copy-pasteable two-instance walkthrough),
  Troubleshooting table (symptom → cause → fix), per-OS reincarnate-enablement
  guidance, an Uninstall & upgrade section, and a Cross-host transports section
  (supported: NTP-synced hosts on a real shared filesystem; unsupported: OneDrive/
  Dropbox-style cloud sync — a documented data-loss mode). `teammate_set_avatar` added
  to the tool table (missing since v0.10.0).
- **SKILL.md:** the reliability contract is rewritten to the honest wake-delivery
  guarantee — channel drops are real (known upstream Claude Code issues), re-nudge
  recovery is capped not guaranteed, matching `DESIGN.md` §7 instead of the previous
  "never loses a message" overclaim. Cross-linked with README in both directions.
  `teammate_set_avatar` added to the tool table (also missing since v0.10.0).

## v0.11.0

**Durable cross-session inbox body-suppression.** `teammate_inbox` now persists the
set of message bodies it has shown to a `{agent}_seen.json` file alongside the inbox.
On a new session (server restart / re-register), the startup drain no longer re-dumps
full bodies for messages already read in a prior session — suppression survives the
restart. `ack("all")` with no prior read still drains the whole inbox (the
`last_seen=None` sentinel is unchanged). The suppression count-line now names the
senders (`"3 already delivered (from: Alice×2, Bob)"`) so a context-wiped agent can
triage who to `show_all` without dumping everything. Default-on, zero new deps.

### Changed
- **`teammate_inbox` body-suppression is now durable across sessions.** A per-agent
  `{agent}_seen.json` in the inboxes directory persists the shown-set. On the first
  inbox read of a new session, bodies for prior-session messages are suppressed using
  this file; brand-new arrivals (NEVER-MISS) always render full. The file is pruned
  to the current unread set on every load — stale ids (acked/removed) can never
  resurrect. `show_all=True` still re-dumps all bodies.
- **Suppression count-line includes sender names.** All three suppression messages
  (`all-suppressed`, `windowed all-suppressed`, partial footer) now render
  `"N message(s) already delivered (from: Alice×2, Bob)"` instead of the bare
  `"already read this session"` phrasing — accurate for cross-session suppression
  and useful as a post-compaction triage hint.

## v0.10.0

Optional **profile avatar images** for teammates. Pre-rendered at ingest time so the
dashboard and statusline CLI pay zero render cost. Adds 1 new tool (17 → 18 total).
Pillow is an optional extra (`pip install teammate-comms[images]`) — the hot path
stays zero-dependency.

### Added
- **`teammate_set_avatar`** — ingest, resize, and pre-render an avatar for any registered
  agent. Accepts a local filesystem `path` or `image_base64`. Images are normalised to
  256 × 256 RGB on a black canvas (non-square images are fitted and padded). Pass
  `clear=true` to remove an existing avatar. Returns the ASCII preview strip on success.
- **`avatars.py`** — new module: `ingest_avatar` (lazy Pillow import), `read_avatar_strip`
  (pure stdlib, zero render cost). Pre-renders three sidecars per agent under
  `TeammateComms/[team/]avatars/`: `<name>.png` (256 × 256 RGB), `<name>.ansi`
  (xterm-256 half-block, 8 × 8 cells), `<name>.txt` (mono ASCII, 8 × 8).
- **`comms.get_avatars_dir`** — team-scoped helper mirroring `get_agents_dir`.
- **`comms.write_bytes_atomic`** — binary counterpart to `write_json_atomic`
  (temp file + `os.replace`; used for PNG/ANSI/ASCII sidecars).
- **`GET /avatar`** — dashboard route serving the pre-rendered PNG by agent name.
  Token via query string (img tags can't set headers), `validate_agent_name` before path
  construction (422 on fail), `Content-Length` + `ETag` for HTTP/1.1 keep-alive.
- **`teammate-comms avatar`** subcommand — reads and prints a cached ANSI or ASCII strip
  to stdout. No Pillow, no network. Suitable for statusline integration:
  `teammate-comms avatar --name <Name>` or `--self` (reads agent from stdin JSON).
- **`teammate_profile`** enrichment — ASCII strip appended to the profile block when an
  avatar is present.
- **Dashboard** — `GET /api/conversations` roster rows include `avatar` (hash or null);
  `navItem` renders `<img class="avatar">` when a hash is present (online/offline
  conveyed via opacity); CSP relaxed from `img-src 'none'` to `img-src 'self'`.

### Changed
- `pyproject.toml` gains `[project.optional-dependencies] images = ["Pillow>=10"]`.
  Core `dependencies = []` is unchanged.

## v0.9.0

First-class **project profiles** layered on the existing per-agent `project` field.
Adds 4 new tools (13 → 17 total) and fixes a silent cross-OS roster split.

### Added
- **`project_register`** — create or update a project profile (summary, description,
  tech_stack, repo_url, name, status, path). Merge-upsert under a blocking lock so
  concurrent first-creates serialize cleanly. `path` auto-fills from
  `$CLAUDE_PROJECT_DIR` on first register if not supplied.
- **`list_projects`** — concise global view: display name + live teammate roster +
  summary per project. Trailing aggregate surfaces undocumented project labels and
  near-miss agents (raw project field differs from canonical key but normalizes to it).
- **`project_profile`** — full detail for one project: all stored fields, provenance
  (created_by/at, updated_by/at), and the live-derived teammate roster with liveness.
- **`project_delete`** — remove a project profile by key.
- **`validate_project_key`** — normalizes `\` → `/`, lowercases, collapses `//`, strips
  leading/trailing slashes, rejects unsafe chars (`%` included to keep slug encoding
  injective). Fixes a silent cross-OS roster split: Windows `Projects\Foo` and Unix
  `projects/foo` now converge to the same key and the same roster.
- **Dashboard enrichment** — `GET /api/conversations` normalizes each roster entry's
  `project` field (so the JS `byProject` sidebar grouping is OS-agnostic) and includes
  a `projects` dict keyed by normalized project key; `renderNav` enriches project
  subheads with the profile summary and status.

### Fixed
- **Silent cross-OS roster split.** Agents registering from Windows (`CLAUDE_PROJECT_DIR`
  uses backslashes) and Unix (forward slashes) previously landed in different roster
  buckets. `validate_project_key` normalization (applied at roster-derivation time and
  in the dashboard payload) unifies them without any agent-side change.

## v0.8.2

A comms-stability fix — restores reliable wakes that degraded mid-session since v0.8.1.
**No tool-surface change.** Re-nudge again covers group posts (≤3 capped terse wakes,
only while genuinely unread) — reliability over a handful of tokens.

### Fixed
- **Watcher crash on re-nudge (TypeError).** `emit_channel_event` lost its `personality`
  positional parameter in WP-11a. The fresh-emit path was updated; the re-nudge path was
  not and still passed a stray `None,` → `TypeError: got multiple values for argument
  'groups'`. The watcher loop had no `try/except`, so the first re-nudge of an unacked
  DM/urgent/@mention killed the daemon thread, heartbeat stopped, agent went offline,
  and all further wakes ceased. This is why wakes "work at first, then degrade."
- **Group messages had no dropped-emit recovery.** WP-11a gated re-nudge to
  DM/urgent/@mention via `_renudge_ids`. Re-nudge exists to recover dropped emits
  (GH #38736/#61797) — gating out ambient group posts meant a dropped group emit was
  permanently unrecovered. Re-nudge is now content-agnostic; `_renudge_ids` is deleted.
- **Same-name re-registration kept stale watcher state.** After a compaction the agent
  re-registers under the same name, but the watcher only reset on a name-change; stale
  `known_ids` suppressed new-arrival wakes. Fixed via an `Identity._generation` counter
  (bumped on every `.set()`) and a `get_generation()` getter; the watcher resets when the
  generation changes, not just when the name changes.
- **Defensive hardening.** The watcher loop body is now wrapped in `try/except Exception
  → stderr + continue` so no future emit bug can silently kill all comms.

## v0.8.1

A token-efficiency pass — leaner channel events, leaner tool output, and leaner tool
schemas — plus a standing rule for authority coordination and CI hardening.
**No breaking changes** to the tool surface (new optional params `all` / `show_all`
added; all existing calls behave identically without them).

### Lean channel wakes
- **Terse wake events.** The `notifications/claude/channel` event is now signal-only:
  the boilerplate phrases ("You have unread messages", "Check your teammate_inbox")
  are gone. Typical wake: `"📬 2 new message(s) from alice, #design. Reply to
  to:'#design' not the sender."` (~10–25 tok vs. the previous ~80–170 tok).
- **Persona-reminder dropped.** The `"You are <name>: <personality>"` prefix on every
  wake is gone — the persona is durable in the agent's session context and echoed
  back at registration; per-wake repetition was redundant token spend.
- **Re-nudge scoped to DM / urgent / @mention.** Ambient group chatter no longer
  burns the re-nudge budget. If a group post goes unread but isn't a DM, isn't urgent,
  and doesn't @mention you, the watcher skips the re-nudge for it specifically — while
  still advancing the backoff clock so a later DM/mention can still fire.

### Lean surface & outputs
- **`teammate_list` drops personality.** The per-agent personality block is removed
  from list output (use `teammate_profile` for full details). Across a team of 26
  agents this saves ~4,000 chars per call.
- **`teammate_list` scopes to your project by default.** With global comms, a list
  of all agents across every project is noisy. The default view now shows only
  teammates in your project. Human operators and agents with no project set are always
  shown (they're global). Pass `all=True` to see everyone; a footer notes how many
  were filtered and warns that cross-project authority owners may be hidden.
- **`teammate_inbox` suppresses already-seen bodies.** A second read no longer
  re-dumps message bodies you already saw — the header line stays (id, sender, group,
  urgency tags) so the message remains ack-able. A note tells you how many were
  suppressed. Pass `show_all=True` to re-read full bodies. (Suppression was
  in-session only in WP-11b; v0.11.0 makes it durable across sessions.)
- **Trimmed tool-def schemas.** The `personality` field description in
  `teammate_register` and `teammate_update` shrank from ~950 to ~195 chars (full
  guidance moved to `SKILL.md`). Net: −1,000 chars per-request tool schema overhead.

### Agent coordination
- **Authority-coordination standing rule.** `teammate_register` now includes a
  standing instruction to check `teammate_list` for authority holders before touching
  an area — and to send a coordination message before modifying anything owned by
  another teammate. The rule also appears in the `SKILL.md` Profiles section and
  reaches the channel reinject path so it persists across idle wakes.

### Under the hood
- CI action pins updated to Node-24 release commits with a force-Node24 trip-wire
  (`FORCE_JAVASCRIPT_ACTIONS_TO_NODE24`), eliminating the Node-20 deprecation warning
  that previously appeared on every CI run.
- A-7 lock-steal contention test de-flaked: the assertion now targets the production
  property (`file_lock` exclusion — at most one holder and at least one acquirer)
  rather than a Windows-rmtree-fragile claim count.

## v0.8.0

A reliability + scale hardening pass over the whole tool, plus dashboard and diagnostics polish —
a full internal audit of the toolset, fixed across nine work packages. **No breaking changes** to
the tool surface: one new optional argument, richer return text, and new safety caps. (Earlier
history: git log + the "Build order" section in `DESIGN.md`.)

### Reliability — you won't lose messages or wakes
- **Wake re-nudges.** If Claude Code drops a channel notification mid-turn or at idle (known CC
  issues #38736 and #61797), the watcher now re-nudges on a capped backoff while unread messages
  persist, instead of
  marking you "nudged" forever after a single emit. A dropped push no longer means a silently
  missed message until the next unrelated one arrives. Every wake emit is logged to stderr so
  server-emitted-vs-client-dropped is diagnosable.
- **No dropped dashboard records.** The dashboard poll cursors (transcript / reactions / deletions)
  page a burst across polls instead of skipping past records when more than the window's worth
  arrives between two polls.
- **Durable reactions & deletions.** A reaction or a deletion event is written under a blocking
  lock now (they're features, not best-effort observability) — no lost reaction chip, no missed
  tombstone under write contention.
- **Robust file locking.** A lock left by a crashed instance is reclaimed only when the holder is
  verifiably dead (pid + host gated, atomic claim, age-based self-heal) — a slow-but-alive holder
  is never stolen from, so no lost writes.

### Scale — it stays fast as logs grow
- **Faster live dashboard.** The records stream uses a byte cursor: a live poll reads only the
  bytes appended since the last poll instead of re-scanning the entire transcript every ~1.5s.
- **Deletions never reappear.** Past the tail window, deletions compact into a target-keyed set
  file, so an old deleted message can't render *undeleted* on a fresh dashboard load. A
  long-suspended browser tab that has fallen behind is rescued in its next poll.
- **Inbox paging.** `teammate_inbox` gains `limit` / `since` arguments (like group history), and
  the acked-message history file is capped so it can't grow without bound.
- **Out-of-order messages are shown.** A message teed to the dashboard out of order is now
  displayed (in arrival order) instead of being silently skipped past the cursor.

### Dashboard
- **Compose parity.** Reply to a message, tag a post type (decision / blocker / fyi / chatter), and
  send urgent — all from the dashboard compose row. Send and react failures now surface to the
  operator (as delete already did).

### Diagnostics & identity
- **Doctor mode.** `teammate_whoami(verbose: true)` adds a read-only diagnostics report — comms
  root, each agent's heartbeat liveness, sub-stream file sizes, unread counts, and any leftover
  lock directories. Reach for it when comms feel stuck.
- **Clearer project labels.** The auto-filled `project` is now `parent/name`, so two repos that
  share a basename (e.g. two `api` checkouts) are distinguishable in `teammate_list`.
- **Unregistered-recipient warnings.** Sending a DM to — or creating a group with — a name that has
  no agent record still queues the message (open membership is intentional), but now tells you so:
  a typo'd recipient no longer queues silently into a phantom inbox.
- **Reincarnate provenance & honesty.** A spawned teammate records who spawned it (`spawned_by`),
  and the launch response is explicit that it confirms *launch, not registration* — naming the
  expected registration window and the headless trust-prompt case that can look like success.
- **Inbox shows reactor names.** The inbox now lists *who* reacted (matching group history), not
  just a count.

### Safety & hardening
- **Size caps.** Message bodies are capped (64 KB) and dashboard POST bodies are capped (1 MB).
- **Token never logged.** The dashboard redacts query strings (where the bootstrap token rides)
  from its request logs.
- **Trust model documented.** `DESIGN.md` now states the single-trust-domain model explicitly:
  on a cooperative single-user localhost tool, `from` is advisory and the author-only delete check
  is anti-footgun, not authorization — so no future feature builds on it as if it were.
- **Reincarnate gate is not inherited.** A spawned child can't itself re-spawn unless its operator
  explicitly opts the gate back in.

### Under the hood
- CI (ubuntu + windows) now runs the test harness. The dashboard HTTP layer, lock recovery,
  auto-register, comms-root resolution, group join/leave/members, and more gained hermetic test
  coverage; the test harness reports a stdout-purity break first as the likely root cause and
  deadline-polls the heartbeat instead of fixed sleeps. Session hooks fail closed cleanly, and the
  docs (DESIGN.md / README) were refreshed to the current state.
