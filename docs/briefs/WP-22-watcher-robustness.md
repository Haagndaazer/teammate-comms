# WP-22 — Watcher: atomic snapshot, heartbeat-failure handling, durable-seen seeding, deferred fan-out recovery

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: W4 (medium), W7 (low-medium), M3 (medium), M2 (high) —
> `docs/260701-fable-audit-{systems,interconnectivity}.md`.
> Cognition tasks: `4ca460444b4d`, `c2ab8b30771d`, `0929bb4149d8`. Depends on WP-19's
> read-before-write heartbeat restructure (build on it, don't duplicate).

## Findings being fixed

- **W4:** `snapshot()` then `get_generation()` are two separate lock acquisitions — a `set()`
  landing between them pairs a stale root/inbox with a new generation for one tick.
- **W7:** heartbeat `write_agent_record` returns False on lock timeout; `run_watcher` ignores
  it — a live agent can look stale to `is_channel_alive` (which reincarnate's guard trusts).
- **M3:** watcher `unseen_ids` subtracts only the in-session `last_seen` (populated by the
  first inbox READ) — a wake arriving before that first read over-counts messages whose
  bodies WP-15 suppression already delivered in a prior session ("3 new" when 2 are stale).
- **M2 (high):** a group fan-out member whose inbox lock was contended goes into `deferred`
  and NOTHING ever tells them — no retry, no marker, no wake. The no-polling promise silently
  reintroduces polling in the flagship group feature, under load.

## Direction

1. **W4:** add `Identity.snapshot_with_generation()` → `(agent, team, root, unread_file,
   generation)` under ONE lock acquisition; watcher uses it. Keep the old methods (other
   callers exist).
2. **W7:** capture `write_agent_record(...)`'s return in the heartbeat tick; on False do NOT
   advance `last_hb` (the next 0.5s poll retries instead of waiting 5s) and stderr-log at most
   once per consecutive-failure episode. Composes with WP-19's demotion-skip: a DELIBERATE
   skip (foreign claimant) DOES advance `last_hb` (we're not retrying those).
3. **M3 — durable-seen seeding (design constraint, MANDATORY):** do NOT touch
   `Identity._last_seen` / its None sentinel (recorded WP-15 decision — ack("all")
   startup-drain rides on it). Instead: at registration (`register_identity`), read
   `{agent}_seen.json` ∩ current unread ids and store it on the Identity object as a separate
   `durable_seen` set (new field + accessor, same lock). The watcher's unseen computation
   becomes `(unread - muted) - last_seen - durable_seen`. The inbox read path keeps
   maintaining the seen file exactly as today (it already prunes to current unread).
   Wake counts stop over-reporting; ack semantics byte-identical.
4. **M2 — pending-file recovery:** on fan-out `CommsError` for a member, the sender appends
   the record to `inboxes/{member}_pending.json` under THAT file's own `file_lock` (rarely
   contended; if THIS also fails, fall back to today's deferred accounting — never raise).
   The watcher (inbox owner side): each poll tick, if own `{agent}_pending.json` exists and
   is non-empty → merge into own unread under the unread lock, DEDUPED BY ID against current
   unread (idempotent if a crash lands between merge and clear), then truncate the pending
   file to `[]` (same lock ordering: three sequential critical sections — read pending →
   lock+merge unread → lock+clear pending; NEVER nest the two file locks, they're
   non-reentrant mkdir locks). The merged records then flow through the normal fresh-wake
   path (they're new ids → they nudge). Sender's return text for a pended member changes from
   "will catch up via history" to "queued for retry — their watcher will pick it up".
   Cost note: the existence check is one os.path stat per 0.5s poll — acceptable (the poll
   already reads the unread file each tick); use a cheap `Path.exists()` gate before opening.

## Acceptance criteria

- AC-1 (W4): `snapshot_with_generation` exists, single lock (inspect the source — no
  double-acquire), watcher no longer calls snapshot()+get_generation() back-to-back.
- AC-2 (W7): a False heartbeat write leaves last_hb unadvanced (pure-function extract or a
  monkeypatched write in a hermetic tick test).
- AC-3 (M3): hermetic — seen file holds id A; unread holds A + new B; fresh registration +
  one watcher tick emits a wake with count 1 (B only). Tautology: current main counts 2.
- AC-4 (M3): ack("all") with no prior read this session STILL drains everything including A
  (startup-drain preserved — the WP-15 T3 test must stay green untouched).
- AC-5 (M2): hermetic — write a record into probe's pending file, run the poll-side merge
  (or one watcher tick): record lands in unread exactly once (run the merge twice → still
  once), pending file emptied, and a wake fires for it. Sender side: simulate a held member
  unread lock → pending file gets the record; text says queued-for-retry.
- AC-6: no new wake noise: a pending-merged id that was ALREADY in the seen file must not
  re-nudge (compose M2 with M3 — write this exact test).
- AC-7: full harness green (three suites) on Windows.

## Known-intentional — do NOT "fix"

- Group transcript stays canonical-first (fan-out remains best-effort; pending is a RECOVERY
  lane, not a delivery guarantee — the transcript write already succeeded before fan-out).
- POLL_SECONDS / HEARTBEAT_SECONDS cadences stay.
- The re-nudge cap (REEMIT_MAX_ATTEMPTS=3) stays — WP-9 decision `5004a641bd82`.
