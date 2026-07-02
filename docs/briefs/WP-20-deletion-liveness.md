# WP-20 — Verified deletion + ghost prevention; reincarnate human carve-out

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: I3 (high), I4 (medium-high), I2 (high) —
> `docs/260701-fable-audit-interconnectivity.md`. Cognition tasks: `374bd3a6d4ee`,
> `1022f45a83f4`. Depends on WP-19 (instance_id exists on records).

## Findings being fixed

- **I3:** `remove_agent` hard-unlinks registry + inbox files with no lock and swallows
  `OSError` unconditionally — on Windows a sharing violation (concurrent heartbeat write)
  silently no-ops the deletion, while `remove_teammate` returns "Removed teammate …" anyway.
- **I4:** delete-then-heartbeat resurrects a permanent `type`-less ghost: a live-but-
  hb-swallowed teammate reads offline → gets deleted → its watcher's next 5s tick re-creates
  the record via the empty-dict merge path WITHOUT `type` (only register sets it). Every
  type-gated consumer then misbehaves (send says "no teammate named X" for a live agent).
- **I2:** `teammate_reincarnate`'s collision guard cannot see a live human
  (`register_human` never sets `channel` → `is_channel_alive` always False) — reincarnating
  the operator's name spawns a child that merge-writes `type=full` over the human record.
  (WP-19's register-time human guard blocks the CHILD's auto-register; this WP blocks the
  SPAWN itself — defense at both ends.)

## Direction

1. **I3 — locked, verified removal:** rework `remove_agent(root, team, name)` to:
   (a) take `file_lock` on the registry record path and unlink it inside the lock;
   (b) unlink the two inbox files (their own unread-file lock, same as tombstone does);
   (c) return the list of paths that could NOT be removed (empty = full success) instead of
   swallowing everything. `remove_teammate` then reports honestly: full success keeps the
   current string; partial failure returns "Removed teammate X (registry) but N file(s) were
   locked — retry teammate_delete to finish: <names>" — and does NOT append the deletion
   event when the REGISTRY record itself failed to unlink (the teammate isn't actually gone;
   the current emit-event-LAST ordering makes this a natural early return).
2. **I4 — no type-less ghosts:** the watcher's heartbeat write gains `type="full"` — truthful:
   run_watcher only ever runs for a registered full instance — **with the mandatory guard
   (peer-review blocker #3): read the current record FIRST (WP-19 already folded a
   read-before-write into the heartbeat tick) and OMIT the type field when the existing record
   reads `type == "human"`** (never stomp an operator record; composes with WP-19's skip-write
   which already covers the foreign-instance case). Result: a post-deletion resurrection
   carries type=full + instance_id and behaves like a normal live agent.
3. **I2 — reincarnate carve-out:** in `_handle_reincarnate`, after reading the existing
   record: if `record.get("type") == "human"` → raise CommsError naming the human operator
   (mirror `remove_teammate`'s wording at tools.py:1471-1472). Place it BEFORE the
   is_channel_alive live-check (the live-check is structurally blind here — that's the bug).

## Acceptance criteria

- AC-1 (I3): hermetic — hold the registry record's lock (or open the file with an exclusive
  handle on Windows) and call remove_teammate: return text admits the partial failure, no
  deletion event appended. Release + retry: clean success + event appended.
- AC-2 (I3 tautology): on current main the same setup returns the unconditional success
  string — the new test must fail there.
- AC-3 (I4): simulate the ghost: create a registered record, delete it, run one watcher
  heartbeat tick (or call the extracted write path) → the re-created record has
  `type == "full"` and send_dm to it no longer claims "no teammate named X is registered".
- AC-4 (I4 guard): a record with `type=human` keeps `type=human` after a heartbeat-shaped
  write for the same name (unit-level; unreachable in practice after WP-19 but the guard must
  hold on its own).
- AC-5 (I2): reincarnate targeting a human-typed record raises; error names the operator.
  Gate-off path (no TEAMMATE_REINCARNATE_ENABLED) still short-circuits FIRST — don't reorder
  the cheap gate.
- AC-6: full harness green (three suites) on Windows.

## Known-intentional — do NOT "fix"

- Offline-teammate removal being open to ANY registered teammate stays (recorded "blast
  radius: open" decision).
- `append_deletion(block=False)` best-effort on the teammate-removal path stays (emit-LAST
  ordering is deliberate) — item 1 only gates WHETHER it's emitted on actual success.
- Heartbeat-only liveness in remove_teammate's live-check stays (`c362e41c838f`).
