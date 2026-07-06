# WP-37 — `teammate_request_compact(target)`: authz + atomic request-file drop

> Owner: Svetlana. Gate: Silvie. Branch: `feat/compact-broker` (after WP-36 — authz reads
> WP-36's `manager` field).
> Source: agent-farm compaction-broker plan (Lord-Wellington spec, 2026-07-05). The request
> file below is the v1 contract Wellington's broker consumes — key names and semantics are
> FROZEN; renaming anything here breaks his Phase 3.

## What this WP adds

One new tool, `teammate_request_compact`, whose whole job is: authorize, then atomically
drop a request file. The BROKER owns everything downstream — TTL expiry, re-validating
authz at execution time, pane-safety gates (permission-dialog state), the injection itself,
and the completion/expiry notification back to the requester. None of that lands in this
plugin.

1. **Dir helper** `get_compact_requests_dir(root)` → `<root>/TeammateComms/compact-requests`
   — deliberately NOT team-namespaced (unlike inboxes/groups): the broker watches ONE dir,
   and agent names are globally unique by constraint. `mkdir(parents=True, exist_ok=True)`
   on first use.
2. **Tool schema**: single required param `target` (agent name). NO `requester` param —
   requester is stamped server-side from `_require_registered(ctx)`; a stray
   caller-supplied `requester` arg is ignored (spoofing via free-text param was flagged in
   Wellington's peer review). Description stays lean; include the one-line doctrine: agents
   self-compact at a safe boundary (subordinate: on handing work back for gate check;
   manager: before blocking on subordinates), managers may also compact their own
   subordinates.
3. **Handler authz** (write-time; broker re-checks at exec-time):
   - `validate_agent_name(target)`; read the target's record FRESH at call time; absent →
     `CommsError` ("no registered teammate named X"), no file.
   - ALLOW iff `target == requester` (self-compact, any role, exact match) OR
     `target_record.get("manager") == requester` (manager compacting own subordinate).
   - DENY everything else: first drop an audit DM to the requester
     ("Compact request for 'X' denied: you are not X's manager (X's manager: Y / not set)"),
     then raise `CommsError` with the same reason. Audit send is best-effort — a send
     failure must not mask the denial error.
   - AUDIT-DM MECHANICS (peer-review finding): `send_dm` HARD-BLOCKS self-sends
     (`to == sender` raises, tools.py:652), so the naive
     `send_dm(sender=requester, to=requester)` always fails and best-effort would silently
     swallow it — AC-2's inbox assertion would be unsatisfiable. Instead send with the
     sentinel sender `"compact-broker"` (only `to` is registration-relevant in `send_dm`;
     the message renders as from `compact-broker` in the requester's inbox — a readable
     audit line). Do NOT weaken the self-send guard itself.
4. **Request file** (on allow):
   ```json
   {
     "v": 1,
     "id": "<uuid4>",
     "requester": "<server-stamped agent name>",
     "target": "<agent name>",
     "created_at": "<ISO8601 UTC, e.g. 2026-07-05T19:04:11.123456+00:00>",
     "ttl_seconds": 900
   }
   ```
   Exactly these six keys, no extras. `created_at` from `datetime.now(timezone.utc)` — UTC
   is the broker contract and deliberately diverges from the codebase's naive-local
   convention (note it in the docstring so nobody "harmonizes" it).
   - Format `created_at` with an explicit `strftime` carrying `%f` (fixed six digits),
     e.g. `%Y-%m-%dT%H:%M:%S.%f+00:00` — NOT `isoformat()`, which drops the fractional
     segment entirely when `microsecond == 0` and would break any fixed-width assumption
     (peer-review finding).
   - Filename: `<created_at compacted filesystem-safe>-<id[:8]>.json`, e.g.
     `20260705T190411123456Z-1a2b3c4d.json` (no colons — Windows). Derive it from the SAME
     strftime moment as `created_at` (one `datetime.now(timezone.utc)` call, two renderings)
     so file name and body always agree.
   - Write via `write_json_atomic` (comms.py:615) — temp sibling + `os.replace`, same dir.
5. **Return string**: request id + target + "broker validates and injects at a safe point;
   completion or expiry (TTL 900s) comes back as a teammate-comms message."

## Acceptance criteria

- AC-1 (self): registered agent A, `target=A` → file appears; JSON has exactly the six v1
  keys; `v==1`, `ttl_seconds==900`, `requester=="A"`, `id` is a valid uuid4; filename
  matches the pattern and embeds `id[:8]`.
- AC-2 (manager): B registered with `manager="A"` → A requesting `target=B` succeeds.
  Unrelated C requesting `target=B` → `CommsError`, NO file written, and an audit DM from
  `compact-broker` naming B and the reason lands in C's inbox.
- AC-3 (anti-spoof): calling with a stray `requester="A"` arg while registered as C still
  stamps `requester=="C"` in the file (self-compact case) — the arg is dead.
- AC-4: unregistered target → `CommsError`, no file. Unregistered CALLER → the standard
  not-registered error (`_require_registered`).
- AC-5 (atomicity): after a successful call, the request dir contains the final `.json` and
  zero `*.tmp` residue; the write path is `write_json_atomic` (grep-proof in diff).
- AC-6: all four suites green on Windows (`uv run --no-sync python tests/<suite>.py`).

## Known-intentional — do NOT "fix"

- The plugin does NOT implement TTL expiry, exec-time authz, pane-state gates, injection,
  or completion notifications — that's the broker's half of the contract. Resist the urge.
- The denial audit DM goes to the REQUESTER's own inbox — deliberate (durable audit trail
  outliving the ephemeral tool error), not a bug.
- No dedup or rate-limiting of repeated requests for the same target — broker's problem.
- `compact-requests` has no `[<team>/]` segment — deliberate divergence from the other
  stores (see §1).
- `created_at` UTC vs. naive-local everywhere else — deliberate (broker contract).
- Temp files are `<name>.json.tmp` siblings in the same dir — fine, because the broker
  globs `*.json` only (contract note sent to Wellington).
- DOCUMENTED RESIDUAL RISK (do not fix here): most-recent-register-wins means squatting a
  manager's name after it goes offline inherits its compact authority over subordinates
  whose records name it — this WP turns identity squatting into an actuation vector. The
  broker's exec-time re-check reads the same roster, so it does not close this. Accepted
  for the cooperative-agents threat model; flagged to Wellington.

## Gate

I run the four suites at your pinned SHA in an isolated worktree; I re-run AC-2's denial
leg and AC-3 by hand against the built server; tautology check: AC-2's no-file assertion
must fail if the authz branch is reverted to allow-all. Handoff includes `For-the-record:`.
