# WP-15 — Durable cross-session inbox body-suppression

> **Manager:** Silvie (planner — does not implement). **Implementer:** Svetlana.
> **Branch:** `feat/wp15-cross-session-suppression`. **Target version:** v0.11.0
> (default behavior change → minor bump; Silvie sets the checkpoint, Colton gates the release).
> **Source task:** vibe-cognition `156f27bb476a` (Colton, 2026-06-30). **Research:** discovery `9fb4d08007ed`.
> **Gate:** Silvie diff-review + Colton go, sign-off names the exact commit SHA, merge is that SHA.

---

## 1. Problem (one paragraph)

`teammate_inbox` body-suppression (WP-11b) keeps a read-but-unacked message from re-dumping
its full body **within a session** — but the seen-set lives only in `Identity._last_seen`
(in-memory, `server.py:67`), which is lost on every new session / server restart / re-register.
So on the **first `teammate_inbox` of a new session**, the standing startup drain re-dumps the
**full body of every lingering read-but-unacked message** (`_prev_seen` is empty). Agents
routinely leave messages unacked across long agentic runs and multiple compactions (confirmed by
Colton), so this re-injection is the common path, not an edge case — it burns context tokens every
time a long-lived agent restarts.

## 2. The fix — a separate, persisted shown-set (Approach A)

Add a **per-agent persisted shown-set** — `{agent}_seen.json` in the inboxes dir, a JSON list of
message ids whose bodies have already been shown to this agent — and consult it **only** for body
suppression in `_handle_inbox`. **Do NOT persist `Identity._last_seen` itself** (see §4 — it is a
load-bearing sentinel).

Concretely, in `_handle_inbox` (`tools.py:655`):

0. **`seen_file` location:** `get_inboxes_dir(root, team) / f"{agent}_seen.json"` — same dir as
   `_unread.json` / `_read.json`. No `ensure_inbox` change is needed: `read_json_safe` returns `[]` on
   FileNotFoundError, so a missing file reads as the empty set (state this rather than assume it).
1. **Load + prune at LOAD time, AFTER the `count_only` early return** (D2): a `count_only` read returns
   at `tools.py:661` before any of this — do NOT load `seen_file` for a `count_only` call (no benefit,
   one wasted read). For a real read: `persisted = set(read_json_safe(seen_file)) & _unread_ids`. Prune
   to current-unread immediately on load — not lazily on first use — so acked/removed ids can never
   resurrect and there is no stale-id window between session start and the first read.
2. **Suppression set** = `_prev_seen = (in-memory last_seen ∪ persisted)`. Use this **only** for the
   `new_msgs` / `seen_msgs` split (`tools.py:698-699`). A brand-new id is in neither set → it always
   renders full (NEVER-MISS preserved).
3. **`set_last_seen(...)` line is unchanged but its SEMANTICS shift — intentional** (D5)
   (`tools.py:687`): because `_prev_seen` is now `in-mem ∪ persisted`, the in-memory `last_seen` will
   absorb prior-session ids after the first inbox read. That is correct and desirable — `ack("all")`
   after the first read now clears cross-session-seen messages, and the watcher quiets faster. The line
   stays identical; do not "fix" it because the inputs changed.
4. **Persist on EVERY read path, immediately after `set_last_seen` at `tools.py:687`, BEFORE any early
   returns** (D3): `write_json_atomic(seen_file, sorted((persisted ∪ _shown) & _unread_ids))`. Placing
   it after line 687 (not at the final render path) guarantees the all-suppressed early return
   (`tools.py:701-707`) still flushes the pruned set. Bounded by inbox size; no tail-cap (it is pruned
   by intersection with unread, unlike `_read.json` which is append-forever and tail-capped).
5. **Reword the count-line copy** (D3): `tools.py:704`, `:706`, AND `:731` all say *"already read this
   session"* — all three become false for prior-session suppression (704 is the windowed-read path —
   do not miss it). Reword to something true across sessions. **Recommended:** include the sender names
   (the `seen_msgs` list is already computed at `tools.py:699`, zero extra IO), e.g.
   *"3 message(s) already delivered (from: Alice×2, Bob) — pass show_all=True to re-read."* This is the
   post-compaction triage affordance (§4a) — a bare count leaves a context-wiped agent blind to who
   messaged it. Exact wording is the implementer's call; the hard requirements: (i) must NOT claim
   "this session" for an earlier-session message, (ii) name the senders.

`teammate_ack` may optionally drop acked ids from `seen_file` for tidiness, but it is **not required**
— the load-time `& _unread_ids` prune already removes them. If you do touch `seen_file` in ack, do it
under the existing unread-file lock; do not add a second lock.

**Concurrency (D4):** `write_json_atomic` prevents torn writes (no corruption), but the
read→compute→write of `seen_file` is NOT lock-held, so two *simultaneous* inbox calls for the same
agent could lose one update. MCP tool calls for one agent are serialized in practice, so this is
accepted: worst case a body re-shows at most once more on the next session. **Do NOT add a lock** for
this — it is not worth the contention. (This corrects an earlier draft that claimed atomic-write alone
made concurrent writes fully safe — it does not; the lost-update is simply tolerated.)

## 3. Acceptance criteria (fix + proof land in the SAME commit)

Hermetic test blocks in `tests/test_handshake.py`, each asserting the **reason**, not just a throw:

- **T1 — cross-session suppression works.** Read messages (populates `seen_file`), then simulate a
  new session by resetting the in-memory identity's `last_seen` to `None` and re-reading. **Assert:**
  the message bodies are NOT in the output, the count line IS present, and the count line does NOT
  contain the substring "this session".
- **T2 — NEVER-MISS holds.** After the simulated new session, a brand-new arrival renders its full
  body. **Assert** the new body text is present.
- **T3 — `ack("all")` startup-drain still works (real regression guard, NOT a tautology).** The
  inbox MUST contain **both** `msg-old` (id in the simulated prior-session `seen_file`) **and**
  `msg-new` (a fresh arrival, id NOT in the prior seen set). At fresh session start (in-memory
  `last_seen is None`), `ack("all")` with no prior read drains the **WHOLE** inbox. **Assert the drain
  count equals the full inbox size (== 2), including `msg-new`.** This is what distinguishes the
  sentinel-is-`None` path from a naive persisted-`last_seen` impl: with persisted `last_seen` holding
  only `{msg-old}`, ack would drain only `msg-old` (count == 1) and orphan `msg-new`. A T3 built with
  *only* prior-session messages passes vacuously against the buggy code — it MUST include the fresh
  arrival or it proves nothing.
- **T4 — load-time prune.** Pre-seed `seen_file` with an id that is no longer unread (acked/removed);
  read; **assert** it neither resurrects into output **and** that it is absent from `seen_file` AFTER
  the read (assert the on-disk file content, not just the rendered output).
- **T5 — `show_all=True`** still re-dumps suppressed bodies across the simulated session boundary.
- **T6 — watcher no-noise unchanged.** After a cross-session read, the in-memory `last_seen` is set by
  the read, so the watcher's `unseen_ids` excludes the read message → no re-nudge. A focused
  `compute_reemit` / unseen-filter test is sufficient; assert no emit for an already-read id.
- **T7 — `count_only` is inert.** A `count_only=True` read neither reads nor writes `seen_file` (it
  returns at `tools.py:661`). Assert no `seen_file` is created by a count-only call on a fresh inbox.
- **T8 — windowed cross-session.** Read window A in "session 1" (e.g. `limit`/`since` selecting the
  older page), then in "session 2" read a DIFFERENT window B. **Assert** window-A ids are suppressed
  (bodies absent) and window-B's not-yet-shown ids render full. This is the case a high-water cursor
  would get wrong — it proves the **set** is the right structure.

Plus the standing quality bar (BACKLOG.md): full harness green on Windows; pure stdlib, zero new deps;
docs updated in the SAME WP (§5); nothing in the Known-intentional list (§4) "fixed".

## 4. Known-intentional — do NOT "fix" these

- **`Identity._last_seen is None` is a SENTINEL, not an oversight.** It means "never read this
  session" and is consumed by `ack("all")` startup-drain (`tools.py:755-758`) and the watcher's
  unseen filter (`channel.py:285-286`). **Do not persist it, do not pre-seed it non-None.** The whole
  point of a separate `seen_file` is to leave this sentinel alone.
- **`set_last_seen(...)` in `_handle_inbox` stays** — it is what keeps the watcher quiet after a read.
- **Read-receipts are ack-based.** `group_read_positions` reads `_read.json` (`comms.py:929`). Do NOT
  wire `seen_file` into receipts — "shown" is not "acked".
- **`_read.json`'s `_READ_CAP` tail-trim** (`tools.py:786`) is correct for an append-forever log;
  do NOT copy that pattern to `seen_file` (it is bounded by intersection with unread instead).

## 4a. Post-compaction triage (deliberate tradeoff)

Suppression is durable, so after a **compaction** (context wiped, but `seen_file` persists) the agent
sees only the count-line for messages whose bodies it no longer holds. That is the intended
token-saving behavior — Colton's call — with `show_all=True` as the recovery hatch. The mitigation is
**naming the senders in the count-line** (§2 step 5) so a context-wiped agent can triage *who* to
re-read rather than blindly dumping everything. This is why the sender-names requirement is hard, not
optional: a bare integer count would convert a token win into a "who messaged me?" footgun.

## 5. Docs to update (same WP as the behavior)

- `README.md`, `skills/teammate-comms/SKILL.md`, `CHANGELOG.md` — the body-suppression description
  becomes "durable across sessions" (these three already mention suppression).
- `DESIGN.md` — if it describes the seen/last_seen model, add the `seen_file` as the cross-session
  suppression layer and state explicitly that `last_seen` stays in-memory and is still the
  ack-all/watcher sentinel.
- Schema description for `teammate_inbox` (`tools.py:230,237`) if the "this session" wording leaks
  into it.

## 6. Mirror-pattern cross-check

Where you mirror `write_json_atomic` / `read_json_safe` usage from the `_read.json` path: copy the
atomic-write and safe-read, **do NOT** copy `_read.json`'s tail-cap (`_READ_CAP`) trimming — the
seen-set is pruned by `& _unread_ids`, never by recency, or you would drop the suppression for the
oldest still-unread messages and re-introduce the re-dump for exactly the long-lived inboxes this WP
targets.

## 7. Rejected alternative (recorded so it is not re-litigated)

**Full auto-ack-on-read as the default** — move every shown message to `_read.json` on read. Gives
honest unread counts, but changes read-receipt timing (receipts would fire on read, not explicit ack),
removes the deliberate "leave unacked as a TODO" affordance, and is a larger protocol-surface change.
Deferred: if honest unread-counts / inbox-never-drains becomes a felt problem, auto-ack returns as its
own WP. This WP solves the stated problem (token re-injection) with zero ack-semantic change.
