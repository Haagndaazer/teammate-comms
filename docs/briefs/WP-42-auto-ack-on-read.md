# WP-42 — Auto-ack on read: reading a message IS acking it; remove the ack surface

**Manager/author:** Silvie · **Implementer:** Svetlana · **Branch:** `wp/auto-ack-on-read` off `main`
**Ships as:** v0.15.0 (breaking: tool removed) · **Release:** merge to `main`, then human release + Loki marketplace pin.

## Goal & decision

Human directive (Colton, 2026-07-16): stale "unread" messages that an agent has already
seen keep re-surfacing after restarts, wasting context and misleading agents into treating
old messages as live. Fix the model at the root: **there is no ack step anymore.** When
`teammate_inbox` shows a message, it is immediately and automatically moved from the agent's
unread inbox to their read inbox. Reading IS acking.

**Decision (Colton, 2026-07-16), supersedes the WP-15 rejection of full auto-ack
(decision node `6dc06fd9c6b6`):** the two reasons full auto-ack was rejected in WP-15 —
receipt-timing change and the leave-unacked affordance — are consciously overruled.
Receipts becoming true "read up to here" positions is an improvement, and the
leave-unacked affordance is not worth the stale-unread problem it causes.

**Settled sub-decisions (Colton, via Q&A — do not re-open):**
- **Read trigger = `teammate_inbox` only.** Channel wake payloads (count/sender nudges — they
  carry no bodies) and the dashboard do NOT count as reads and must not move messages.
- **Re-read path = `show_read`** (rename of `show_all`, new semantics — see spec step 3).
- **`teammate_ack` is removed entirely** — no deprecation stub.
- **No migration script.** Old backlogs drain naturally on each agent's next inbox read
  (with the legacy `seen.json` transitional handling in spec step 4 so the drain does not
  re-dump old bodies into context).

## Known-intentional — do NOT touch (deliberate, not bugs to "fix")

- **NEVER-MISS invariant:** a message whose body was never shown to the agent never leaves
  unread (sole documented exception: the legacy `seen.json` sweep, step 4 — those bodies
  WERE shown, in a prior session).
- **`_cap_unread`** (tools.py ~862) and its `acked_unseen`+`capped` overflow tagging stay
  exactly as-is (T2 is data-preserving). After this WP, cap-overflow is the ONLY source of
  `acked_unseen` — reword its describing text (step 6), don't change its mechanism.
- **C1 locking** (`file_lock` on `unread_file` for every read-modify-write) and the
  **T2/C-3 caps** (`_UNREAD_CAP`, `_READ_CAP`) stay.
- **WP-41 opt-in surface:** no new session-start nudges or standing instructions sneak back
  in via reworded descriptions.
- **`teammate_react`** stays — reactions are not the ack mechanism being removed (but see
  step 7: its description calls itself "a lightweight acknowledgement"; reword to avoid the
  now-meaningless term, e.g. "a lightweight response").
- **`compute_reemit`'s 3-attempt cap** (channel.py ~146) stays — it is what already prevents
  a woken-but-never-reads agent from being re-nudged forever.

## One direction per fix (settled — implement exactly this)

1. **Lock scope:** the auto-ack in `_handle_inbox` runs the ENTIRE select→move→write
   sequence inside ONE `file_lock(unread_file)` acquisition with a FRESH read of
   `unread_file` taken under that lock. Do NOT reuse the existing early-released read at
   tools.py ~784-785 (its lock scope deliberately covers only the read; bolting a write-back
   onto that stale snapshot silently drops messages that arrive in the gap — this is the
   plan's #1 identified failure mode; the P4 test in step 9 exists to catch it).
2. **Write order:** append the moved messages to `{agent}_read.json` FIRST, then rewrite
   `{agent}_unread.json` (the `_cap_unread` ordering). The current `_handle_ack` writes in
   the opposite order (tools.py ~942-943) — a crash between the two writes loses the message
   from both files. Do not copy that ordering onto a path that now runs on every inbox call.
   (Worst case under the correct order is a duplicate across the two files after a crash,
   never a loss.)

## File-by-file spec

1. **`src/teammate_comms/tools.py` — `_handle_inbox` (~775):**
   - Restructure per "one direction" 1 & 2: single lock, fresh read, move-what-you-show,
     read-file-first write order. Stamp each moved record with a read timestamp (e.g.
     `read_at`, ISO-8601 UTC) when appending to `_read.json`. Apply the `_READ_CAP` trim on
     this write path (it now happens here, not in the removed ack handler).
   - `count_only=True` moves nothing and shows nothing (unchanged contract).
   - With `since`/`limit`, ONLY the messages actually shown move to read; the rest stay
     unread untouched.
   - Remove the seen-set machinery from this handler: the `{agent}_seen.json` read/prune/
     write (~786-819), the `_prev_seen`/suppression split (~826-857), and the
     `Identity.set_last_seen`/`get_last_seen` plumbing it feeds. Keep step 4's transitional
     read of a legacy `seen.json` (that path deletes the file when done).
   - Update the tool description: reading moves messages to the read inbox immediately;
     there is no ack step. Document `show_read` (step 3) including the `_READ_CAP=1000`
     horizon (the read file churns on every inbox call now, so the recovery window is
     bounded — say so rather than letting it surprise).

2. **`src/teammate_comms/tools.py` — remove `teammate_ack`:** the schema entry (~315-323)
   and `_handle_ack` (~885 onward), plus its dispatch-table entry and any helper used only
   by it. The `ack("all")` startup-drain and "Kept N" race-guard semantics die with it
   (their job is now done automatically and more precisely by move-what-you-show).

3. **`show_read` (replaces `show_all`):** integer N ≥ 1. When passed, the call returns ONLY
   the N most recent messages from `{agent}_read.json` (newest last, same rendering as
   normal messages, clearly headed as read history) — it does NOT show unread and does NOT
   move anything. Purpose: post-compaction recovery / re-reading old messages. Combining
   `show_read` with `count_only`/`since`/`limit` is a usage error (raise `CommsError` with a
   one-line explanation). `show_all` disappears from the schema.

4. **Transitional legacy `seen.json` sweep (in `_handle_inbox`):** if
   `{agent}_seen.json` exists, then on the next non-`count_only` inbox call: ids listed in
   it that are currently in unread are moved to read (same lock, same write order) but
   rendered only as the existing one-line summary ("N message(s) already delivered (from:
   …)"), NOT full bodies. This sweep is independent of `since`/`limit` (it clears the whole
   legacy backlog in one visit) but is gated off entirely on `count_only=True`. Afterwards
   delete `{agent}_seen.json`. This is the sole sanctioned exception to "only shown bodies
   move" — these bodies were genuinely shown in a prior session; do NOT tag them
   `acked_unseen`.

5. **`src/teammate_comms/server.py` — Identity:** remove the now-dead seen-set plumbing:
   `set_last_seen`/`get_last_seen` (~130-135) and `set_durable_seen`/`get_durable_seen`
   (~140-145), plus their state fields and every caller. If reincarnation/registration
   copies or seeds seen state anywhere, remove that too (grep for `seen`).

6. **`src/teammate_comms/channel.py` — watcher simplification:** in `run_watcher`, the
   unseen-set (~437-439) collapses from `(unread_ids - muted_ids) - last_seen -
   durable_seen` to `unread_ids - muted_ids` — valid ONLY because of the lock/order fixes in
   step 1 (in unread now literally means never-shown). Remove the `last_seen`/`durable_seen`
   inputs and any plumbing that fed them. `compute_reemit`, the 0.5 s poll, the baseline
   seeding, and `merge_pending_into_unread` are untouched.

7. **Group receipts + surrounding text:**
   - Mechanics of `teammate_group reads` / `group_read_positions` /
     `group_read_has_unseen_acks` (comms.py ~1224) keep working unchanged — positions still
     derive from `_read.json`, and excluding `acked_unseen` records is still correct.
   - Reword the `reads` footnote (tools.py ~1526-1527, "startup-drained messages are not
     counted as read") and the `group_read_has_unseen_acks` docstring (comms.py ~1224-1235):
     `acked_unseen` now comes only from cap-overflow. Receipts are now true "read up to
     here" positions — say so.
   - `teammate_react` description: replace "a lightweight acknowledgement" (tools.py ~422)
     per known-intentional note.

8. **Versioning:** bump to `0.15.0` in **all three** of `src/teammate_comms/__init__.py`,
   `pyproject.toml`, `.claude-plugin/plugin.json` (the version-sync test asserts three-way
   agreement), and regenerate `uv.lock`.

9. **`tests/test_handshake.py`:**
   - **Port, don't delete, the P4 SILENT-LOSS guard** (~2164-2219, `ack("all")` after
     `limit=N`): rewrite it against the new path — a message arriving concurrently with /
     after a windowed `teammate_inbox` read must survive in unread. It is the one test that
     catches the "one direction" failure mode 1.
   - Rewrite the WP-15 cross-session suppression block (~2857-2973) into tests of the new
     contract: shown → moved to read; not-shown → stays unread; `count_only` inert; windowed
     reads move only the window; legacy `seen.json` sweep (summary line, file deleted,
     records NOT tagged `acked_unseen`).
   - Rewrite the WP-9 re-nudge assertions keyed on `last_seen` (~1339-1377) for the
     simplified watcher: no re-nudge for messages consumed via inbox (they left unread);
     re-emit backoff/cap behavior unchanged for genuinely-unread.
   - Rewrite/trim the WP-25 `acked_unseen` cold-drain assertions (~4103-4145): the
     startup-drain source is gone; cap-overflow tagging still asserted.
   - Remove all `teammate_ack` invocations/assertions; add a test that `tools/list` does not
     contain `teammate_ack`.
   - New: crash-ordering test if cheaply testable (read-file append precedes unread rewrite
     — e.g. assert on write sequence via monkeypatched atomic-write recorder), else assert
     ordering structurally (both files' contents after a normal move: no message absent from
     both). Tests must be tautology-proof: assert the specific new behavior such that the
     reverted code fails them.

10. **Docs:**
    - `DESIGN.md` — §7 (~238-299: v0.4.2 nudge-gating contract, two-layer
      `Identity._last_seen`/`{agent}_seen.json` suppression) rewritten as the new
      architecture: auto-ack on show, unread == never-shown, watcher unseen-set simplified.
      Tool table (~362) drops `teammate_ack`; sweep remaining references (~390, 534, 851).
      Leave historical/changelog-style past-tense entries alone — they record what shipped.
    - `README.md` — remove ack from the flow (register → send → wake → inbox); describe
      auto-ack + `show_read`.
    - `skills/teammate-comms/SKILL.md` — same; remove any "ack your inbox" guidance.
    - `docs/history/WP-15-*.md` — untouched (history).
    - `CHANGELOG.md` — `## v0.15.0` entry (draft below).

11. **MCP tool-surface audit (required final step — human directive):** after all code
    changes, read EVERY tool description and schema field description the server ships
    (`tools/list` surface) end-to-end, as an LLM would, and fix any text that misinforms
    usage under the new model — stale mentions of ack/acking/unread persistence, the old
    `show_all`, "startup drain", suppression ("bodies of messages already shown are
    suppressed"), `teammate_reincarnate`/`teammate_request_compact` bootstrap prompts, error
    strings that reference acking, and the WP-31 schema-grep test expectations. List every
    description you changed (and every one you checked and left alone) in the handoff — the
    gate re-runs this audit independently.

## CHANGELOG v0.15.0 (draft — implementer may refine wording)

> **WP-42 — reading is acking.** `teammate_inbox` now immediately moves every message it
> shows into your read inbox; the unread inbox only ever contains messages you have never
> seen, so restarts no longer resurface stale "unread" messages. The `teammate_ack` tool is
> removed (breaking). `show_all` is replaced by `show_read: N`, which reads back your N most
> recent read messages (post-compaction recovery). Group read receipts are now true "read up
> to here" positions. Legacy per-agent `seen.json` files are swept into the read inbox on
> first contact and deleted.

## Acceptance criteria (checked at the gate)

- **AC-1** Reading moves: a `teammate_inbox` call that shows messages leaves them present in
  `_read.json` (with `read_at`) and absent from `_unread.json`; a repeat call shows nothing
  and the watcher does not re-nudge them.
- **AC-2** NEVER-MISS: `count_only` and unshown/out-of-window messages never leave unread; a
  message arriving concurrently with a windowed read survives in unread (ported P4).
- **AC-3** Atomicity/order: whole move under one `unread_file` lock with a fresh read;
  `_read.json` written before `_unread.json` (verified in the diff, not just claimed).
- **AC-4** `teammate_ack` gone from `tools/list`, code, tests, and shipped docs (grep clean);
  `show_all` gone; `show_read` returns only read-inbox history and moves nothing.
- **AC-5** Legacy `seen.json` sweep: summary-line rendering (no body re-dump), whole backlog
  in one visit, gated off on `count_only`, file deleted after, records not `acked_unseen`.
- **AC-6** Watcher unseen-set is `unread_ids - muted_ids`; Identity seen-set plumbing gone;
  re-emit cap behavior unchanged.
- **AC-7** Version `0.15.0` in all three files + regenerated `uv.lock`; full pinned gate
  green.
- **AC-8** Tool-surface audit (step 11) done and evidenced in the handoff: no shipped tool
  description, schema field, bootstrap prompt, or error string references acking, `show_all`,
  suppression, or startup-drain semantics.

## Gate command (pinned — run the identical invocation)

```
uv sync
uv run --no-sync python tests/test_handshake.py
uv run --no-sync python tests/test_wp13_projects.py
uv run --no-sync python tests/test_wp14_avatars.py
uv run --no-sync python tests/test_compact.py
```

All four suites green. Silvie re-runs them at the pinned commit in an isolated worktree
before sign-off (do not trust the handed green).

## Handoff protocol

- Fix + proof in the same commit. Post the branch + commit SHA when ready for the gate.
- Include a **`For-the-record:`** field with any durable facts (surprises, decisions made
  during implementation) for Silvie to record to cognition.
- Do **not** commit `.cognition/journal.jsonl` on the WP branch (shared-checkout rule).
- Before any destructive git op (`reset`, `clean`, `stash`, `checkout -- <journal>`), ping
  Silvie to flush first.
