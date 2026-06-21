# Changelog

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
- **`teammate_inbox` suppresses already-seen bodies.** A second read in the same
  session no longer re-dumps message bodies you already saw — the header line stays
  (id, sender, group, urgency tags) so the message remains ack-able. A note tells you
  how many were suppressed. Pass `show_all=True` to re-read full bodies (useful after
  context compaction).
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
