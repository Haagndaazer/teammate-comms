# WP-25 — unread cap (tagged move-to-read); ack-without-read receipts; group NDJSON storage

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: T2 (medium), M5 (medium), C3 (medium-high).
> Cognition tasks: `60f989d05f0a`, `945b1ad3b274`, `cfe9b9d3e54d`.
> ⚠ The first two findings are COUPLED by peer-review blocker #1 — implement M5's tag FIRST,
> then the cap rides it. Do not sequence them independently.

## Findings being fixed

- **M5:** `ack("all")` cold-start drain moves NEVER-SEEN messages into `_read.json`;
  `group_read_positions` then infers "caught up" for messages the member never read — false
  read-receipts via the startup-drain path.
- **T2:** `_READ_CAP` bounds `_read.json` but the live `{agent}_unread.json` — the file every
  sender appends to and the watcher polls twice a second — is never capped.
- **C3:** `append_group_message` does read-array → append → rewrite-full-file per post;
  `groups/*/messages.json` grows forever and every post rewrites all of it.

## Direction

1. **M5 — `acked_unseen` tag:** in `_handle_ack`'s startup-drain branch (last_seen is None),
   tag each drained record `{"acked_unseen": True}` before extending `read`. In
   `group_read_positions`, EXCLUDE records carrying the tag from the max-id inference. The
   `reads` action's output gains one footnote line when any member's position was computed
   with tagged records present: "(startup-drained messages are not counted as read)".
   Seen-then-acked records stay untagged — receipts unchanged for the normal path.
2. **T2 — unread cap, data-preserving:** `_UNREAD_CAP = 1000`. At BOTH unread-append sites
   (send_dm, send_group fan-out — inside the existing unread lock): after append, if
   `len(unread) > _UNREAD_CAP`, move the OLDEST overflow records into `{to}_read.json`
   **tagged `acked_unseen: True`** (they were never read — the M5 tag is exactly the right
   semantics; peer-review blocker #1) and ALSO tagged `"capped": True` (distinguishes
   cap-overflow from startup-drain in forensics). `_read.json` is rewritten under the SAME
   unread lock (the established tombstone/ack pattern). Then apply the existing `_READ_CAP`
   trim. Chosen policy: preserve-don't-drop (a hard drop of unread = message loss; the
   audit's own rubric calls that critical). Rejected: rejecting new sends at the cap (would
   fail the SENDER for the recipient's neglect). Diff summary must note the quiet visibility
   change: an unread message beyond 1000 is no longer surfaced by teammate_inbox (peer-review
   nit #16) — README's inbox row gets one sentence (this WP, not WP-32: docs ride the
   behavior).
3. **C3 — group NDJSON:** new append path `groups/<g>/messages.jsonl` (one JSON line per
   record, O(1) append under the SAME lock file the current writer uses — key the lock off
   the jsonl path's sibling as today's is keyed off messages.json; simplest: keep locking
   `messages.json`'s lock path for BOTH stores so writers serialize across formats).
   `read_group_messages` returns legacy `messages.json` array (if present) + jsonl records
   appended (legacy is strictly older — order preserved). `tombstone_in_group_messages`
   rewrites BOTH stores under that same lock (tombstone is rare; O(n) there is fine —
   peer-review nit #11). `delete_group`'s rmtree already covers both. NO migration rewrite of
   legacy files (read-both indefinitely; note it in DESIGN §storage when WP-32 lands).

## Acceptance criteria

- AC-1 (M5): cold-start ack("all") over unseen messages → `reads` shows "(none acked)" for
  that member; a seen-then-acked message still advances the position. Tautology: current
  main reports the drained id as the position.
- AC-2 (T2): append the 1001st message → oldest lands in `_read.json` with both tags, unread
  holds exactly 1000, nothing lost (union of both files = all sent ids); group
  read-positions ignore the moved record; WP-15 suppression tests stay green (the seen-file
  prune already tolerates ids leaving unread — verify T4 still passes).
- AC-3 (T2 boundary): exactly-at-cap append does not trigger a move (strictly-greater gate).
- AC-4 (C3): post to a group → messages.jsonl gains one line, messages.json (if absent)
  stays absent; a group with a legacy array + new lines reads back in order; tombstone by id
  hits records in EITHER store; group history + dashboard read path unchanged in output.
- AC-5 (C3): concurrent-ish append safety — two sequential appends under contention produce
  two intact lines (reuse the harness's lock-contention style from the WP-6 blocks).
- AC-6: full harness green (three suites) on Windows.

## Known-intentional — do NOT "fix"

- ack("all") startup-drain DRAINS everything — stays (v0.4.x decision `d771ab975ca3`); M5
  only changes how receipts INTERPRET the drained records.
- Group transcript canonical-first ordering stays.
- `_window`/id-collision paging semantics stay (WP-26 touches ids, not this WP — keep the
  WPs composable: no id-format changes here).
