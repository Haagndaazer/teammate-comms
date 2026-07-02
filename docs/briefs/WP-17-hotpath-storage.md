# WP-17 — Hot-path storage: roster normalization + inbox read safety + bounded reaction reads

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: G1 (critical, Stage-2), C1 (high), C2 (high) — see
> `docs/260701-fable-audit-interconnectivity.md` #G1 and `docs/260701-fable-audit-systems.md`
> #C1/#C2. Cognition tasks: `281def4ace27`, `eb1c4c45d82c`, `8585f71607e0`.
> Fix + proof same commit; full harness green on Windows.

## Findings being fixed

- **G1 (critical):** `_handle_list` compares `record["project"]` raw strings while every other
  project-comparison surface normalizes via `validate_project_key` — cross-OS teammates on the
  same project silently vanish from each other's default `teammate_list`. This violates WP-13's
  own acceptance criterion #2 and constraint `3a81e56eb341`.
- **C1 (high):** `_handle_inbox` reads `{agent}_unread.json` — the highest-contention
  multi-writer file — via `read_json_safe` (destructive: resets to `[]` on ANY read failure)
  with NO lock. A transient mid-write read failure wipes a live inbox. Every other toucher of
  this file locks it; the watcher uses `read_json_readonly`.
- **C2 (high):** `_handle_inbox` and group-history call `read_reactions(root, team)` with no
  limit → bypasses the tail fast path and parses the ENTIRE global `reactions.jsonl` on every
  inbox/history call, forever-growing.

## Direction

1. **G1:** add a small helper (e.g. `_norm_project_label(raw)`) → `validate_project_key(raw)`
   or `None` on empty/CommsError. In `_handle_list`, compare normalized keys; when EITHER side
   is unparseable (None), fall back to raw equality so an odd label still matches itself —
   never a false split. Keep the existing "always show self / never filter no-project records /
   all=True" behavior byte-compatible (the harness asserts on current list output).
2. **C1:** take `file_lock(unread_file)` around the unread read in `_handle_inbox` and KEEP
   `read_json_safe` inside it. Rationale to encode in a comment: under the lock no writer can
   be mid-write, so a parse failure is REAL corruption and the destructive self-heal is then
   correct — this preserves the corrupt-file self-heal while killing the
   mistake-a-partial-write-for-corruption race. Do NOT switch to read_json_readonly+raise
   (that would make a genuinely-corrupt inbox permanently unreadable). `{agent}_seen.json`
   stays as-is (self-owned; a reset only costs re-shown bodies). NOTE the lock is NOT
   reentrant — verify no caller path already holds it (none does today).
3. **C2:** module constant `_REACTIONS_TAIL = 1000` with a comment (chips target recent
   messages; an event older than the window stops rendering as a chip, the reaction itself is
   never lost). Pass `limit=_REACTIONS_TAIL` at BOTH call sites (inbox render + group history).

## Acceptance criteria

- AC-1 (G1): hermetic unit block — three agent records: caller `project="Projects\\Foo"`
  (Windows spelling), peer `project="projects/foo"` (Unix spelling), outsider
  `project="other/bar"`. Default list shows the peer, filters the outsider; `all=True` shows
  everyone. Uses a minimal fake ctx identity (see the WP-15 block's `_Id15` pattern at the
  harness tail).
- AC-2 (G1 tautology): the test MUST fail against current main (raw compare splits the two
  spellings).
- AC-3 (C1): source-level tripwire (`inspect.getsource(_handle_inbox)` contains
  `file_lock(unread_file)`) PLUS behavior check: a REALLY corrupt unread file (`"{torn"`)
  still self-heals to `[]` and the call returns "No unread messages".
- AC-4 (C2): tripwire — no unbounded `read_reactions(root, team)` call remains in `tools.py`
  (regex over the source; WP-7 fixed this same anti-pattern elsewhere and it crept back at the
  two hottest call sites).
- AC-5: existing WP-15 suppression tests (T1–T8) and the WP-8 P2 reaction-names block stay
  green — your lock must not change inbox output text at all.
- AC-6: full harness green on Windows.

## Known-intentional — do NOT "fix"

- `read_json_safe` under a held lock on OWNED files (ack, tombstone) is the recorded
  concurrency pattern — this WP brings `_handle_inbox` INTO that pattern, it does not replace
  the pattern.
- ack("all") startup-drain semantics; the last_seen/None sentinel (WP-15 decision
  `6dc06fd9c6b6`) — untouched.
- The transcript tee's droppable lock is deliberate ("observability, not delivery") — only the
  READ sites in this WP change.

## Gate notes

I will run the suites myself at your pinned SHA and run the G1 test against the reverted fix.
Composition check: C1's lock + C2's limit both sit inside `_handle_inbox` — verify the lock
scope covers ONLY the unread read (not the reactions read; holding the inbox lock across a
reactions.jsonl read would couple two stores' contention).
