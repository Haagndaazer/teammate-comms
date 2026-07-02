# WP-27 — Reactions compaction (state baseline) + transcript rotation

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit finding: C6 (medium — the deferred half of AUDIT-v0.7.0 C-2). Cognition task:
> `21ce53019f93` (priority LOW — if any step here turns risky, STOP and ask; a documented
> deferral beats a subtle regression on a low-priority item).

## Findings being fixed

- **C6:** deletions got real compaction in WP-7; `transcript.jsonl` and `reactions.jsonl`
  grow forever. Read-side tails mitigate cost but disk growth is unbounded, and the
  dashboard's fresh reaction seed (newest 500) already silently drops old chips.

## Direction

1. **Reactions — fold into a STATE baseline (mirror the deletions design, with one critical
   difference):** reactions have `op: remove`, so the fold is STATEFUL, not an append-only
   union — seed `aggregate_reactions`' fold from the baseline, then continue folding live
   events (peer-review #10: a naive union would resurrect removed reactors). Concretely:
   - `reactions_state.json`: the aggregated `{target: {emoji: [reactors]}}` for every event
     folded out of the live jsonl.
   - `_compact_reactions_locked` (called under the reactions.jsonl lock via a size gate on
     append, like `_maybe_compact_deletions`): parse all events; fold `all-but-newest-RETAIN`
     INTO the existing state (state-first atomic write, then trim the jsonl to the tail —
     same crash-ordering rationale as deletions: overlap-idempotent? NO — reaction folds are
     NOT idempotent under re-fold of remove events? They ARE: folding the same event sequence
     into an already-folded state re-applies add/discard with identical results. State-first
     then trim is safe: a crash between leaves the head both in state and in jsonl; re-fold
     re-applies identically. Encode this reasoning in the docstring.)
   - New read helper `read_reaction_state(root, team)` → baseline dict; a companion
     `aggregate_reactions_with_baseline(baseline, events)` seeds the fold state from the
     baseline (per-(target,emoji) sets initialized from it), then folds events.
   - Update the aggregate consumers: inbox render + group history use
     baseline + `read_reactions(limit=_REACTIONS_TAIL)`. RETAIN must be ≥ _REACTIONS_TAIL
     (use 2000/_REACTIONS_TAIL=1000) so the tail window always sits INSIDE the retained
     jsonl — no gap between baseline and tail. Assert that relationship with a comment AND a
     test (a future constant edit must trip something).
   - Dashboard: `_api_poll` fresh load (no rcursor) prepends SYNTHETIC add-events derived
     from the baseline (`{target, emoji, from: reactor, op: "add", id: ""}`) before the live
     tail — the frontend's existing client-side fold renders them; cursored polls unchanged.
     Synthetic events carry id "" so they can never advance rcursor (verify: new_rcursor
     picks reactions[-1]["id"] — order synthetic FIRST so a real tail id wins; if the tail is
     empty, rcursor must stay "" — check that branch).
2. **Transcript — size-gated rotation:** on append (inside `append_transcript`'s existing
   lock+try), if `transcript.jsonl` exceeds `TRANSCRIPT_ROTATE_BYTES` (16 MB), `os.replace`
   it to `transcript.jsonl.1` (clobbering any previous .1) and start fresh. The byte-cursor
   generation (crc of first line) CHANGES on recreation → the dashboard transparently
   re-tails (that machinery exists precisely for this). `resolve_message`/react keep their
   3-tier fallbacks for rotated-away ids (group files + inboxes). Read paths do NOT read .1
   (document: rotation bounds the live file; .1 is a grace copy for manual forensics).
   Doctor (`whoami verbose`) reports both files' sizes.

## Acceptance criteria

- AC-1: append > gate → reactions.jsonl trimmed to RETAIN, state file holds the folded head;
  a reactor REMOVED in the folded head does not reappear in inbox/history chips nor in the
  dashboard fresh load (the peer-review resurrection case — write this exact test).
- AC-2: re-running the compaction fold over an overlapping range yields identical state
  (idempotence proof).
- AC-3: chips for a message whose events are entirely in the baseline still render in inbox +
  group history + dashboard fresh poll (synthetic events).
- AC-4: transcript > gate → rotated; a running byte-cursor poll across the rotation re-tails
  (reuse WP-7 P3's test style: reset flag True, no crash, no duplicate flood beyond the
  documented tail re-serve); react/delete on a rotated-away id still resolve via fallbacks.
- AC-5: RETAIN ≥ _REACTIONS_TAIL relationship asserted in a test.
- AC-6: full harness green (three suites) on Windows.

## Known-intentional — do NOT "fix"

- TEAMMATE_TRANSCRIPT=0 semantics unchanged (rotation only when the file exists/grows).
- The deletions compaction design is the template — do not refactor it, mirror it.
- reactions.jsonl stays ALWAYS-written (not gated by TEAMMATE_TRANSCRIPT) — feature stream.
