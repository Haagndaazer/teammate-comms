# teammate-comms — Audit-fix backlog (managed by Silvie, implemented by Svetlana)

> Source of findings: `AUDIT-v0.7.0.md` (IDs referenced below). Owner of this file: **Silvie**
> (planner/manager — does not implement). Implementer: **Svetlana**. Work flows as
> work packages (WPs), in order, one branch per WP (`fix/audit-wpN`).
>
> **Quality bar (every WP):** this tool must withstand power-user scrutiny — bug-free is the
> standard, not "works for us." Concretely: (1) every behavior change ships with hermetic test
> blocks in `tests/test_handshake.py` covering the failure mode it fixes *and* the boundary
> cases around it; (2) full harness green on Windows; (3) pure stdlib, zero new runtime deps;
> (4) nothing in the AUDIT "Known-intentional" table gets "fixed"; (5) docs (README/DESIGN/
> SKILL) updated in the same WP as the behavior they describe; (6) Silvie code-reviews every
> diff before merge — no push to `main`, no version bump, no release without Silvie sign-off
> + Colton's go; sign-off names the exact commit SHA and the merge must be that SHA;
> (7) every process rule gets checked against the implementer's NON-CODE outputs (cognition
> nodes, logs, fixtures, generated docs) — a rule that keeps side-channel state out of version
> control is a data-loss bug wearing a tidiness costume (journal protocol: Silvie flushes to
> main via temp worktree at checkpoints; destructive git ops need a flush ping first);
> (8) any brief saying "mirror/match/copy existing pattern X" gets a pre-send cross-check of X
> against the audit's findings for that file, and must NAME the parts of X not to copy —
> and in review, inherited lines get MORE suspicion than novel ones ("matches existing code"
> is how flagged debt metastasizes; adopted from vince/Vorpid's WP-3, where a brief saying
> "mirror session-start.sh" faithfully duplicated that file's audit-flagged B-3 bug);
> (9) fix + proof land in the SAME commit, stated in acceptance criteria up front (ledger 20
> companion), and failure tests assert the REASON, not just the throw;
> (10) any WP that moves/splits/re-owns a surface re-greps the recorded constraints (cognition
> nodes, DESIGN claims, known-intentional list) against the new tree as an acceptance criterion —
> a recorded rule does not travel with the code it binds (ledger constraint-drift, from Levoit).

## Pipeline

| WP | Status | Scope (audit IDs) | Theme |
|----|--------|-------------------|-------|
| WP-1 | **APPROVED @ 1103be2** (de-flaked test-only delta verified; 5/5 her runs + 1 independent = 6 greens; merging --no-ff to local main, no push until the v0.7.1 checkpoint) | A-1, A-2, A-3 | Missed-event correctness: poll-cursor burst drop; reaction-wake tail rebind; blocking locks for reaction/deletion appends |
| WP-2 | **MICRO-CR** (72b92ec reviewed, approve-with-CR: "DM reaction"→"reaction (DM or group post)" in README+DESIGN; + extend gitattributes to `merge=union -text` per the CRLF byte-offset finding) | E-1, E-2, E-3, E-5, E-6, F-6(schema wording) | Doc-drift + dead code: TEAMMATE_HUMAN_NAME docs; DESIGN.md v0.7.0 reframe (12→13 tools, §2/§9/§12, `_audible`); delete `_validate_message`/`_validate_priority`/`DEFAULT_LAUNCH_ARGS`; TRANSCRIPT=0 wording; stale comments; ack-all/history-limit schema wording; + one-liner: `.gitattributes` `*.jsonl merge=union` for `.cognition/` (defense-in-depth — no journal commit should ever happen on a branch, but if one sneaks in, the merge is safe) |
| WP-9 | **MERGED @ 6d86b46** (disarm fix adversarially re-verified; main = WP-1+WP-2+WP-9, unpushed — release call with Colton) | NEW finding H-1 | Wake reliability: Claude Code drops channel notifications mid-turn (GH #38736) and sporadically at idle (GH #61797) — docs say queued, reality says dropped — while our watcher marks an id nudged-forever after ONE emit (`known_ids |= unread_ids`), so a dropped push = silent permanent miss until the next unrelated message. Fix: (a) re-nudge with backoff — if unseen unread persist ≥120s since last emit, re-emit the standard wake (count/senders/groups), capped (~3 tries, ×2 backoff), gated on UNSEEN ids only so a read-but-unacked message never re-nudges (preserves the v0.4.2 stress-proven no-noise contract); (b) stderr-log every emit (ids, kind, attempt) so server-emitted-vs-client-dropped is diagnosable; (c) DESIGN: replace "a dropped push loses nothing" with the honest contract + cite the CC issues |
| WP-3 | **MERGED @ e9cd571** (main = WP-1+2+9+3, unpushed; first push = CI's first run, watch the ubuntu leg) | G-1, G-4 | Test/ops floor: stdlib `http.client` endpoint coverage for all 5 dashboard APIs (token/Host guards, error codes); hooks hardening (`${CLAUDE_PLUGIN_ROOT:-}` guard + `{}` fallback, no stamp on failed `uv sync`, verify SessionStart matcher double-fire); minimal CI (ubuntu+windows: run the harness) |
| WP-4 | **MERGED @ 9703874** (main = WP-1+2+9+3+4; Colton's ~2-min manual-verify checklist rides with the release pass) | B-1, B-2, B-3 | Dashboard send parity: pass through `reply_to`/`post_type` in `_api_send` + compose affordances (reply, type, urgent); surface send/react errors like delete does |
| WP-5 | **MERGED @ 23f8a2c** (first zero-CR gate pass; main = WP-1+2+9+3+4+5) | D-1(doc), D-2, D-3, F-1, A-9, F-6(assert) | Hardening: write the trust model into DESIGN.md; redact query strings in dashboard `_log`; POST body + message-length caps; strip `TEAMMATE_REINCARNATE_ENABLED` in `build_child_env`; stderr-trace unexpected dispatch exceptions; real raise instead of `assert` in spawn.py |
| WP-6 | **MERGED @ 5839750** (main = WP-1+2+9+3+4+5+6; lock-design pattern node recorded as reusable) (A-7 pid-owner lock + atomic-rename steal claim, exactly-one-winner on Windows; A-5 predicate purge = window-SHRINK with documented residual; A-4 documented eventual-consistency; C-1 tail-first scan; N-1 documented + characterization test, byte-cursor fix SPLIT to co-design with WP-7 rotation) | A-4, A-5, A-7(promoted), C-1, N-1(new) | Race + scale, part 1: group-delete purge by `group==sigil` predicate (kills the escape race); membership re-read under meta lock for tombstone fan-out; bounded-tail-first scan in `react()`/`resolve_message`; N-1 = PRE-EXISTING out-of-order transcript tee can skip a dashboard record past the cursor (message ids are stamped at send, before the inbox lock — NOTE: the transcript id IS the message id used by ack/react, so the WP-1 stamp-under-lock fix does NOT transfer; needs its own analysis) |
| WP-7 | **APPROVED @ a9f3af7 — ALL FOUR PHASES, merging** (P4 inbox paging + union-prune last_seen; P1 seek-from-tail reader; P2 deletions compaction + lagged-tab rescue; P3 transcript byte-cursor closing N-1 — "shown late beats never shown") | C-1, N-1, C-2, C-3 | Scale, part 2: deletions compaction past the tail window; rotation/seek-tail strategy for NDJSON logs; inbox `limit`/`since` args + `_read.json` growth cap |
| WP-8 | **MERGED @ 227b2e1 — AUDIT BACKLOG COMPLETE** (F-2/F-3/F-4/F-5/G-2/G-3/G-5/G-6 shipped; B-4 DEFERRED by Silvie as documented-known-limit — 3.5s cosmetic gain not worth last-minute protocol surface; returns as its own WP if users feel it) | F-2, F-3, F-4, F-5, B-4(deferred), G-2, G-3, G-5, G-6 |

| WP-10 | **ASSIGNED** (Colton-requested 2026-06-11) | NEW | Authority-coordination standing rule in INSTRUCTIONS (session-start + compact paths): before starting a task, check teammate_list for authority over areas you'll touch; coordinate BEFORE modifying. Guidance only — NO enforcement code (D-1 boundary). Ships as v0.8.1; Loki holds the pin for the v0.8.1 SHA. |

Release checkpoints (Silvie decides, Colton gates): v0.7.1 after WP-1+WP-2; subsequent
versions batched as WPs land. E-4 (delete the stale in-repo `marketplace.json`) is held out —
it needs Colton's explicit OK since it changes an install path.

## Working agreement (autonomous mode)

- Svetlana works the queue **top-down without waiting for go-aheads between WPs**: post the
  plan (peer-reviewed per repo standing rules) as an FYI and proceed immediately; Silvie
  reviews async and can redirect.
- The **hard gate is the diff review**: post diff summary + test results to Silvie when a WP
  is code-complete. While a WP awaits review, **start the next WP** on its own branch
  (the queue is ordered to minimize file overlap between adjacent WPs).
- Anything ambiguous, risky, or touching a Known-intentional behavior: stop and ask Silvie
  rather than guess.
- **Branch-switch protocol (journal is a tracked file):** at every WP branch switch, ping
  Silvie first → Silvie verifies the live-vs-main journal diff (flushes if non-empty) → then
  switch. If git still refuses the checkout, SILVIE (not the implementer) runs the
  `git checkout main -- .cognition/journal.jsonl` alignment after re-verifying the diff is
  empty *seconds before* running it — a "no-op" claim about a live file goes stale between
  checking and acting. (Adopted from vince/Vorpid, who hit the same refusal on their WP-2.)
  **HOLD until WP-2's `-text` gitattribute merges:** with `* text=auto eol=lf`, a per-file
  journal checkout rewrites CRLF→LF bytes under the server's BYTE-based replay offset —
  content-no-op ≠ byte-no-op. After `-text`, byte-equal == content-equal and the step is safe.
- **Release pushes** (when Colton gives the go): branch + PR, merged by Silvie with
  `gh pr merge --match-head-commit <approved-sha>` — GitHub then mechanically rejects the
  merge if the head moved after sign-off (the SHA-pin rule, enforced by the platform).
