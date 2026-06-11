# Changelog

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
