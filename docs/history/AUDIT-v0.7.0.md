# teammate-comms v0.7.0 — Full Toolset Audit

> **Advisory document — uncommitted, nothing implemented.** Identification only, per request.
> Produced 2026-06-10 against `main` @ `53827f8` (v0.7.0, 13 tools).
>
> **Method:** five parallel audit lenses (correctness/concurrency+protocol, dead-code/drift,
> security/trust-model, UX/API gaps, tests/operability), each finding spot-verified against the
> code, then traced through the vibe-cognition history so every recommendation knows **how the
> current state came to be** — deliberate decision vs. accidental drift. Findings that trace to
> recorded decisions are either moved to "Known-intentional" or framed as "revisit the decision,"
> never as bugs.
>
> **Severity rubric:** **critical** = data loss / protocol corruption / silent message loss ·
> **high** = crash, missed wake, silent wrong behavior, or a break in the tool's own permission
> model · **medium** = drift, friction, perf-at-scale, coverage gap on an important path ·
> **low** = polish/hardening/cosmetic.

---

## Executive summary

The core is in good shape: the concurrency trio is applied with real discipline (every mutation
site checked — locks, lock-then-read ordering, and destructive-vs-readonly reads are all
correct), stdout protocol purity holds everywhere, the dashboard has no XSS/CSRF/traversal
surface, spawn is injection-proof (list-form argv), and name validation is traversal-proof and
enforced server-side. **No critical findings.**

The real themes, in priority order:

1. **The newest subsystems didn't get the hardening the oldest ones did.** The message-wake
   path was race-hardened across v0.4.1–v0.4.4 (read-position gating, live stress tests); the
   v0.6.1 *reaction*-wake path and the v0.5.0 dashboard poll cursors never got the same
   missed-event analysis (A-1, A-2, A-3).
2. **Nothing is ever bounded.** transcript/reactions/deletions/inbox files grow forever; several
   hot paths re-read whole files (C-1, C-2).
3. **The trust model is real but unwritten.** Identity (`from`) is forgeable by design-adjacent
   accident; permission checks that look like authorization aren't (D-1).
4. **Docs lag the code by ~4 versions** in DESIGN.md, plus one undocumented env var (E-group).
5. **The dashboard HTTP layer and hooks have zero test coverage**, and there is no CI (G-group).

### Top 10 priorities

| # | Finding | Severity | One-liner |
|---|---------|----------|-----------|
| 1 | A-1 | high | Dashboard poll cursor skips past records on a >200 burst — permanently invisible |
| 2 | A-2 | high | Reaction-wake 500-tail re-bind can silently miss wakes; never got the message-path's analysis |
| 3 | A-3 | high | `append_reaction` uses a droppable lock — a reaction (a *feature*) can be lost under contention |
| 4 | D-1 | high | Forgeable `from` underpins author-delete + wake routing; trust model undocumented |
| 5 | B-1 | high | `/api/send` silently drops `reply_to`/`post_type` — operator can't thread from the dashboard |
| 6 | C-1 | medium | Every `react()`/`delete` scans the entire unrotated transcript (`limit=None`) |
| 7 | G-1 | high | All five dashboard HTTP endpoints have zero test coverage; no CI anywhere |
| 8 | A-5 | medium | Whole-group delete races a concurrent post — escaped inbox copy (same class as the v0.7.0 bug) |
| 9 | E-1..E-5 | high/med | Doc-drift batch: DESIGN.md "as-built v0.3.1", "12 tools", `TEAMMATE_HUMAN_NAME` undocumented |
| 10 | F-1 | medium | Reincarnated child inherits `TEAMMATE_REINCARNATE_ENABLED` — re-spawn loop possible |

---

## A. Correctness & concurrency

### A-1 | high | dashboard.py:226-238 + comms.py:649-650 — poll cursor advances past burst-dropped records
`read_transcript(since=cursor, limit=200)` collects everything `>= cursor` then keeps only the
**newest** 200 (`records[-limit:]`); `_api_poll` then sets `new_cursor = records[-1]["id"]`. If
more than 200 records land between two ~1.5s polls (group fan-out makes this realistic), the
oldest are sliced off **and the cursor jumps past them** — permanently invisible in the
dashboard. Same pattern: reactions (500), deletions (1000).
**Provenance:** no recorded decision on burst semantics — the limits were sized as "comfortable
tails" when the sub-streams shipped (v0.5.0/v0.6.1/v0.7.0); the truncate-then-advance interaction
was never analyzed. Accidental gap.
**Direction:** when the limit is hit, keep the **oldest** N instead and advance the cursor only
to the last *returned* record (or return `truncated: true` and let the client immediately
re-poll). One-line semantic change in `read_transcript`'s slice + cursor choice.

### A-2 | high | channel.py:158-170 — reaction-wake re-binds `known_reaction_ids` to a 500-tail
The 5s heartbeat reads `read_reactions(limit=500)`, computes fresh wakes, then unconditionally
re-binds `known_reaction_ids = set(rids)`. If >500 reaction events are appended within one
heartbeat window, a reaction targeting this agent can scroll past the tail without ever being in
a snapshot → silent missed wake. Needs extreme volume, hence high not critical — but it's the
same *class* of bug the message path was explicitly hardened against.
**Provenance:** message-wake went through v0.4.1→v0.4.4 missed-nudge analysis with live stress
tests (`5b3a46a17274`, `b801059f24a7`); the v0.6.1 reaction poll (`5ee68bfab557`) has **no
recorded decision** behind the tail-rebind — implementation shortcut. Accidental gap.
**Direction:** track a high-water cursor (max id seen) like the dashboard does, instead of a
bounded id-set; or `known_reaction_ids |= rids` with pruning by cursor.

### A-3 | high | comms.py:689-710 (`append_reaction`) — reactions can be silently dropped
`append_reaction` mirrors `append_transcript`: `file_lock_optional(timeout=2)` + silent return
when not acquired. For the transcript that's the documented "observability, not delivery" stance
— but the code's own comment says reactions are "**a feature, not observability**." A reaction
lost under lock contention is feature-data loss: no wake, no chip, ever.
**Provenance:** copy of the transcript-tee pattern (intentional for transcript, per pattern
`7bff080a52cc`); applying the same droppability to reactions was inherited, not decided.
**Direction:** use the blocking `file_lock` for reactions (and `append_deletion`, same reasoning
— a dropped deletion event means the dashboard never learns about a tombstone).

### A-4 | medium | tools.py:~1080 (`delete_message` group path) — membership read outside lock
Group tombstone reads `meta` members unlocked, then tombstones inboxes one by one. A member who
joins mid-loop (having already received fan-out) can keep a live copy; a crash mid-loop leaves a
partial tombstone. Bounded — tombstone, not data loss.
**Provenance:** v0.7.0 plan consciously accepted per-store locking (no cross-store atomicity);
the membership-read race specifically wasn't called out. Mostly-intentional, edge accidental.
**Direction:** re-read membership under the group meta lock, or document eventual consistency.

### A-5 | medium | tools.py:~850 (`_handle_group` delete) — concurrent post escapes inbox purge
Whole-group delete snapshots message ids + members with no lock, then purges/rmtrees. A racing
`send_group` can fan out a new message after the snapshot; its inbox copies survive the purge —
a "deleted group" message lingering in an inbox, the same *symptom class* as the v0.7.0 bug this
flow fixed.
**Provenance:** v0.7.0 plan fixed the *ordering* (B4: ids → purge → rmtree → event) but didn't
add cross-operation exclusion. Accidental residue of the fix.
**Direction:** hold the group meta lock across snapshot+purge, or purge member inboxes by
`group == sigil` predicate instead of by id-set (the predicate is immune to the race).

### A-6 | medium | server.py:245-253 — single-threaded request loop
One contended `file_lock` (up to 10s) or `tasklist` call (5s) stalls every other tool call.
Heartbeat survives (own thread); interactivity doesn't.
**Provenance:** direct consequence of the recorded pure-stdlib decision (`b8f4fab73a85`).
Intentional-by-architecture. **Direction:** document as a scaling limit; revisit only if real.

### A-7 | low | comms.py:300-311 — lock steal on timeout has no owner check
A slow-but-alive holder can be stolen from → two writers → lost append. Needs >10s contention.
**Direction:** write a pid into the lock dir and steal only verified-dead locks, or don't steal.

### A-8 | low | server.py:249-252 — malformed JSON frame with an id gets no `-32700` reply
Client would hang/timeout. **Direction:** best-effort parse-error response, or document.

### A-9 | low | tools.py:1233-1238 — dispatch catch-all hides programming bugs
Necessary to keep the loop alive, but a non-`CommsError` exception should also trace to stderr
so refactor bugs surface in logs. **Direction:** add `_log` on the unexpected branch.

### A-10 | low | channel.py:144-150 — heartbeat write uses droppable lock, return value unchecked
A skipped heartbeat narrows the 30s liveness window by 5s. Tolerated by design margin
(`c362e41c838f`), worth a comment at most.

---

## B. Dashboard behavior gaps

### B-1 | high | dashboard.py:278-294 (`_api_send`) — `reply_to` and `post_type` silently dropped
The operator cannot thread a reply or mark a decision/blocker from the dashboard; `_api_send`
never reads those payload keys (zero references in dashboard.py) and the compose UI has no
controls. Group threads the human participates in silently fracture.
**Provenance:** `_api_send` predates typed posts/threading polish (v0.5.0 dashboard vs. v0.5+
group polish); never revisited. Accidental drift.
**Direction:** pass-through in `_api_send` + minimal compose affordances (reply chip, type
selector, urgent toggle — see B-2).

### B-2 | medium | static/index.html — no priority selector in compose
`_api_send` accepts `priority` but the UI can't set it; the human can never send urgent.
**Direction:** one checkbox.

### B-3 | medium | static/index.html:417-426 — `sendMsg` swallows errors silently
Delete surfaces failures via alert (deliberate v0.7.0 fix, N6); send still restores the compose
text with no message. Inconsistent with the just-established convention.
**Provenance:** N6 was scoped to the new delete affordance only. Accidental inconsistency.
**Direction:** apply the same error surfacing to send (and react, which also swallows).

### B-4 | medium | dashboard.py:218 + index.html — read receipts up to 5s stale
`groupReads` refreshes only on the 5s roster tick, not the 1.5s poll. Cosmetic lag on a feature
whose point is at-a-glance ack state. **Direction:** include read-positions in the poll response.
Note `_api_conversations` already does O(groups × members) full `_read.json` reads every 5s —
fold into the C-group scale work rather than naively polling it faster.

---

## C. Scale & unbounded growth

### C-1 | medium | tools.py react()/resolve_message() — full-transcript scan, `limit=None`
Every reaction and every delete reads and JSON-parses the **entire** `transcript.jsonl`. On the
global shared root this grows for the life of the install.
**Provenance:** `react()` adopted the simplest correct resolve at v0.6.1; `resolve_message`
copied it at v0.7.0. No decision weighed scan cost. Accidental.
**Direction:** scan a bounded recent tail first (targets are almost always recent), fall back to
the full scan on miss.

### C-2 | medium | comms.py — no rotation/compaction anywhere; tail-reads still stream whole files
`read_transcript`/`read_reactions`/`read_deletions` read every line then slice the tail — the
dashboard poll does this ~every 1.5s. Specific sharp edge: `read_deletions(limit=1000)` means
after 1,000 lifetime deletions a fresh dashboard load replays an incomplete deleted-set → an
old deleted message renders **undeleted**.
**Provenance:** append-only NDJSON was deliberate (observability model); the absence of rotation
was never decided, just never needed yet. Accidental-by-youth.
**Direction:** rotation or seek-from-tail reads; for deletions, compact to a set file once the
tail window is exceeded (deletions are idempotent and keyed by target — ideal for compaction).

### C-3 | medium | tools.py:514-541 — inbox and `_read.json` grow without bound; no pagination
`teammate_inbox` has no `limit`/`since` (unlike `group history`); acked messages append to
`_read.json` forever, and the unread-file is what the watcher polls twice a second.
**Direction:** `limit`/`since` args on inbox; cap or rotate `_read.json`.

---

## D. Security & trust model

*(Verified-clean: loopback-only bind, Host-header rebinding guard, constant-time 256-bit token,
no URL→filesystem mapping at all, every untrusted string → `textContent` under a
`default-src 'none'` CSP, list-form argv in spawn, traversal-proof name validation enforced at
the write boundary. No critical findings.)*

### D-1 | high (trust-model) | tools.py send/react/delete + channel.py wake routing — `from` is forgeable and downstream checks treat it as identity
Any local process (or any teammate) can author records as anyone: `from` is caller-asserted,
never authenticated. Downstream, **the author-or-operator delete check and reaction/mention wake
routing consume this forgeable field** — so "author-only delete" is a courtesy convention, not a
boundary, and a forged `target_from`/`mentions` wakes arbitrary victims. Any process can also
read any inbox file.
**Provenance:** emergent, not decided. Two deliberate decisions — sender-explicit send for
human-as-teammate (v0.5.0, `a9594b942f2b`) and the global shared root (`ef4af8135c03`) — compose
into this property, but **no node ever records "we accept forgeable identity."**
**Direction:** do **not** re-architect (correct for a single-user cooperative localhost tool).
Write the trust model into DESIGN.md explicitly — "all local processes of this OS user are
mutually trusting; `from` is advisory; the delete author-check is anti-footgun, not authz" — so
no future feature (e.g. cross-host comms) builds on the author-check as if it were real.

### D-2 | medium | dashboard.py:141,95 — token accepted in GET `/` query string; logged
The bootstrap URL carries the token as a query param; `log_message` writes request lines (incl.
query) to stderr, and the token sits in browser history until `replaceState` scrubs it. A local
log-reader recovers full send/delete power. Local-only.
**Direction:** keep query bootstrap (it's how a URL works) but redact query strings in `_log`,
and note the residual exposure in DESIGN. Optionally add an Origin allowlist on POST as
defense-in-depth (custom-header auth already blocks browser CSRF).

### D-3 | medium | dashboard.py:171-172 + tools.py `_clean_message` — no size caps
POST bodies are read to `Content-Length` with no ceiling; message bodies have **no max length**
(profile fields are capped, messages aren't). A multi-MB body is stored and re-served to every
poller forever.
**Provenance:** profile caps were a deliberate v0.2.0 design; message-length cap simply never
came up. Accidental. **Direction:** cap POST bodies (e.g. 1 MB) and message length (e.g. 64 KB).

### D-4 | low | comms root files carry default OS-user permissions
Single trust domain across all projects of this user — consistent with D-1; document together.

---

## E. Dead code & doc drift

### E-1 | high | `TEAMMATE_HUMAN_NAME` (tools.py:1197) — read + advertised in the tool schema, absent from README/DESIGN/SKILL
A human deployer reading any doc's env-var list cannot discover it. **Direction:** add to all
three env tables.

### E-2 | high | DESIGN.md:8,103,283 — framed "as-built (v0.3.1)", embedded plugin.json says 0.3.1, "**12 tools:**" above a 13-row table
Four major versions of drift in the document's own framing; a reader is invited to distrust the
correct later sections. §12 status list also stops at v0.3.1; §2 layout omits
`hooks/reinject-instructions.sh`; §9 references an `_audible` filter that doesn't exist in
channel.py (the real mechanism is inline set-difference on `muted_ids`).
**Direction:** one doc pass: reframe header to v0.7.0, fix the count, refresh §2/§9/§12.

### E-3 | medium | tools.py:428-435 — `_validate_message` / `_validate_priority` are dead
Grep-confirmed zero call sites across py/sh/tests/dispatch table. Leftover from the deliberate
v0.5.0 sender-explicit refactor (`a9594b942f2b`); the cross-reference comment on
`_clean_message` (tools.py:391) dangles with them. spawn.py:34 `DEFAULT_LAUNCH_ARGS` is likewise
a self-described back-compat alias with zero external references.
**Direction:** delete all three.

### E-4 | medium | `.claude-plugin/marketplace.json` — pinned at `e7272d9` (v0.4.4), 3 versions stale
**Provenance changes the recommendation:** the marketplace consolidation decisions
(`468a128ef82d`, `e90ee298dfc6`) made `colton-claude-plugins` the single source of truth and
deleted vibe-cognition's in-repo manifest; this one survived only because its name
(`colton-comms`) doesn't collide. Nobody has re-pinned it since — evidence it has no consumers.
**Direction:** delete it (finish the consolidation) rather than resume re-pinning; update the
DESIGN §4b sentence that still promises per-release re-pins.

### E-5 | medium | README — `TEAMMATE_TRANSCRIPT=0` described as a full opt-out
`reactions.jsonl` and `deletions.jsonl` are **always** written (deliberate: "a feature, not
observability"), and reaction wakes silently degrade when the transcript is off
(`target_from` unresolvable). The README's opt-out sentence implies full quiet.
**Direction:** one parenthetical in README + DESIGN.

### E-6 | low | Stale comments batch
session-start.sh:4 header still says "remind the user to set TEAMMATE_AGENT" (contradicted by
its own lines 47-51 — predates the runtime-identity decision `d6f6652ac59e`);
test_handshake.py:8 "register + 12" phrasing. **Direction:** trivial comment fixes.

---

## F. UX/API gaps & feature debt

### F-1 | medium | spawn.py:114 + tools.py:1174-1175 — child inherits `TEAMMATE_REINCARNATE_ENABLED`
`build_child_env` copies the full parent env and never strips the gate, so a child can re-spawn.
Two real brakes exist (gate must be on in the parent; live-name collision guard caps each name
at one live instance) so it's churn, not an exponential bomb.
**Provenance:** the reincarnate plan (`59cf255d30ed`) deliberately made the gate opt-in
default-off; child inheritance was never discussed. Accidental gap.
**Direction:** strip/zero the gate in `build_child_env` (opt back in per-child explicitly).

### F-2 | medium | tools.py:487-507 — DM to a never-registered name silently creates a dangling inbox
`ensure_inbox` runs before any existence check; a typo'd recipient yields success + a phantom
inbox nobody will ever read; the only hint is the generic "no live channel" warning.
**Provenance:** open membership / auto-join was a deliberate group decision (`4152caab6808`);
extending the same laissez-faire to DM typos was never decided. **Direction:** warn (don't
error) when no agent record exists; same warning for unregistered names in `group create`.

### F-3 | medium | comms.py `_reaction_summary` vs group history — inbox shows reaction counts, not names
Group history shows `👍 alice, bob`; the inbox shows `👍 2`. The wake says someone reacted; the
inbox can't say who. **Direction:** unify on the names form.

### F-4 | medium | server.py:127-129 — `project` auto-fill is basename-only
Two repos named `api` are indistinguishable in `teammate_list`.
**Provenance:** auto-fill was deliberate (`1d271c4a786f` — "chosen over manual so agents don't
forget"); single-component value was incidental. **Direction:** `parent/name` two-component
default, still overridable.

### F-5 | medium | tools.py:1145-1187 — reincarnate parent gets no registration feedback
"Launch ≠ registration" is honestly stated, but there's no timeout guidance and the headless
trust-prompt case (allowlist absent, nobody clicks) looks identical to success.
**Direction:** document an expected-registration window + suggest a `teammate_list` recheck
pattern; consider a `spawned_by` breadcrumb in the child's register record.

### F-6 | low | Ergonomics batch
`history` limit applies post-filter (correct for decision trails, surprising — say so in the
schema); ack-all's never-read-this-session branch clears everything ("startup-drain", deliberate
v0.4.x behavior `d771ab975ca3` — but the schema description omits it); react success string
could name the resolved author; delete-already-deleted is effectively idempotent but appends a
duplicate deletion event; removed-then-re-registered names inherit the predecessor's transcript
attribution (names are identity by design — worth one DESIGN sentence); `assert` in
spawn.py:121 is a no-op under `python -O` — make it a real raise.

---

## G. Tests & operability

### G-1 | high | dashboard.py HTTP layer — zero endpoint coverage; no CI
All five endpoints (`/api/conversations`, `/api/poll`, `/api/send`, `/api/react`,
`/api/delete`), the token/Host guards, and error codes are exercised only by a human in a
browser. A serialization or cursor regression ships silently. There is also no `.github/`
workflow — the 1000-line harness runs only when someone remembers.
**Provenance:** the harness grew integration-first around the stdio surface (consequence of the
zero-dep decision `b8f4fab73a85` — no pytest); the dashboard arrived at v0.5.0 and its HTTP
layer never got a client. No decision *against* coverage — accumulation.
**Direction:** stdlib `http.client` smoke block in the existing harness (start dashboard, hit
each endpoint, assert codes/shapes — zero new deps); minimal CI matrix
(ubuntu + windows: `uv run --no-dev python tests/test_handshake.py`).

### G-2 | medium | Untested branches that have bitten this codebase's exact failure classes
`file_lock` timeout/steal; `read_json_safe` corrupt-reset; `_maybe_auto_register`
(`TEAMMATE_AGENT` env path — tests explicitly pop it); group `join`/`leave`/`members` actions;
`spawn_in_terminal` (gate-off in tests); `resolve_comms_root` fallback branches.
**Direction:** hermetic unit blocks in the existing harness style.

### G-3 | medium | Harness brittleness
A stdout-purity failure cascades into dozens of false downstream failures (no short-circuit on
`bad_stdout`); hard `time.sleep` choreography against the real 5s heartbeat will flake on slow
CI (use deadline-polling); out-of-sequence request ids (47-52 amid 34-46) are undocumented.
**Direction:** short-circuit on corrupted stdout; replace fixed sleeps with poll-until-deadline.

### G-4 | medium | hooks — fail-closed but silent, and one contract ambiguity
Unset `CLAUDE_PLUGIN_ROOT` → `set -u` kills either script before any JSON is emitted (fail-closed
but invisible); `uv sync … || true` in session-start.sh can stamp a half-built venv as done, so
the *next* session skips the sync and the server fails with no diagnostic; the matcherless
SessionStart entry plausibly also fires on compact alongside the `compact`-matched one
(double-fire is cheap thanks to the stamp, but it's unverified behavior).
**Provenance:** the venv-stamp design is unrecorded; the reinject hook deliberately mirrors
session-start.sh's venv resolution (vince's gotcha, applied at v0.7.0). Mostly accidental.
**Direction:** `${CLAUDE_PLUGIN_ROOT:-}` guard + `echo '{}'` fallback at the top of both
scripts; don't write the stamp when `uv sync` fails; verify the matcher behavior once and add
an explicit `"matcher": "startup|resume|clear"` if double-fire is real.

### G-5 | low | Diagnosability
Everything is best-effort stderr prints; there is no doctor affordance. A corrupt comms root,
dead watcher, or stale dashboard are all silent. **Direction:** a `teammate_whoami` verbose mode
or small `doctor` action reporting root path, watcher liveness, file counts/sizes, lock leftovers.

### G-6 | low | dashboard `_load_index` triple-fallback can serve a placeholder page on a packaging mistake
Test never asserts `static/index.html` is present in the installed wheel. **Direction:** one
packaging assert in the harness.

---

## Known-intentional — do not "fix" (recorded decisions)

| Behavior | Decision provenance |
|---|---|
| Transcript tee is best-effort/droppable; inbox is authoritative ("observability, not delivery") | pattern `7bff080a52cc`; **but see A-3** — reactions/deletions inherited droppability without a decision |
| Dashboard mutation-reflection requires `TEAMMATE_TRANSCRIPT=1`; `=0` degrades to durable tombstones only | v0.7.0 plan B2, documented |
| Reactions persist on tombstoned messages in MCP reads, hidden in dashboard | v0.7.0 plan S3 |
| Reacting to a deleted message still wakes its author | v0.7.0 plan S4 — *accepted as benign*, though the UX lens makes a fair case to revisit (woken for a message you can no longer read); if revisited, it's a decision change, not a bug fix |
| Offline-teammate removal open to any registered teammate, any project; live/self/human never removable | v0.7.0 plan resolved decision ("blast radius: open") |
| `transcript.jsonl` never rewritten in place; deletions ride their own sub-stream | v0.7.0 plan, documented |
| ack-all with no prior read this session clears everything (startup-drain) | v0.4.1/v0.4.2 selective-ack decisions (`d771ab975ca3`), live-proven — schema wording gap only (F-6) |
| Global comms root; cross-project by default | `ef4af8135c03` |
| Pure-stdlib zero-dep server; single-threaded loop; stdlib http.server dashboard | `b8f4fab73a85` — constrains all recommendations above to stdlib-only |
| `teammate_list` liveness via heartbeat-only (`pid_check=False`) | `c362e41c838f` |
| Identity at runtime via `teammate_register`; `TEAMMATE_AGENT` is a convenience | `d6f6652ac59e` |
| Open group membership, auto-join-on-send | `4152caab6808` |

---

## Verified solid (audited, no action)

- Lock discipline at **every** mutation site: tombstones and acks lock the unread file and
  rewrite both inbox files under it; lock-then-read ordering everywhere; `read_json_safe`
  (destructive) only ever on owned files under a held lock; shared reads use
  `read_json_readonly`.
- stdout protocol purity: all diagnostics → stderr across all six modules; single
  `send_message` under `_stdout_lock`; children get `DEVNULL`; test asserts purity.
- Message-wake path: per-id set membership (not count deltas) — ack+new in one poll window
  cannot mask a wake; mute absorption can't retro-nudge; `known_ids` stays bounded.
- Dashboard security posture: loopback bind, Host check, constant-time token, no
  URL→filesystem route, textContent-only rendering under strict CSP, no inline handlers.
- spawn.py: list-form argv end-to-end, hostile-prompt test asserted, channel flags are
  compile-time constants; managed-settings parse fails toward the safe (dangerous-flag) path.
- Validation: agent/group names traversal-proof; profile caps enforced server-side at both
  register and update.
- Version triplet agrees at 0.7.0; `_HANDLERS` ↔ `TOOL_DEFINITIONS` ↔ docs tool tables all
  match at 13; instructions single-sourced (server handshake + compact hook share one object).
- Clean shutdown: watcher daemon + stop event; offline record written in `finally`; dashboard
  shutdown can't block it.
