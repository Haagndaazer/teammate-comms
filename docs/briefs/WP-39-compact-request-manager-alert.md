# WP-39 — `teammate_request_compact`: wake the requester's manager on a self-compact

> Owner: Svetlana. Gate: Silvie. Branch: `feat/compact-manager-alert` off current `origin/main`.
> Worktree: `C:\Users\colto\Documents\Projects\Worktrees\teammate-comms\feat-compact-manager-alert`.
> Builds on WP-37 (`_handle_request_compact`, tools.py:2092) + WP-38 broker. **Plugin-side only —
> no broker change, no request-file contract change (the six v1 keys stay frozen).**

## Problem

`teammate_request_compact` drops a request file and returns; the broker later acts at a safe
point. The request is **silent to the requester's manager**. A subordinate self-compacts and
its manager — sitting idle — is never woken, losing visibility into a state change on a
teammate it owns. `send_dm` writes the recipient's inbox, which wakes a live `full` teammate
via their channel watcher (tools.py:691-693) — that is the wake path we reuse.

## What this WP adds

In `_handle_request_compact`, on the **self-compact allow path only**, after the request file
is written, send a best-effort wake-DM to the requester's registered manager.

### Trigger conditions — fire the alert iff ALL hold

1. `target == requester` (self-compact leg — NOT the manager-compacting-subordinate leg).
2. `manager` (already read at tools.py:2106 as `target_record.get("manager")`) is truthy AND
   `manager != requester`.
3. **The manager resolves to a registered teammate** — `read_agent_record(root, team, manager)
   is not None`. Send only then.

> Note on condition 1 (peer-review finding): given the allow-path invariant at tools.py:2107
> (`if not (target == requester or manager == requester): DENY`), condition 2's
> `manager != requester` already implies we're on the self-compact leg among *allowed* states —
> so condition 1 is defensive/legible, not independently load-bearing. Keep it for clarity, but
> **the tests must prove the guard by asserting on the `send_dm` call, not by reverting one
> guard** (see AC section).

> Why condition 3 (peer-review findings #2 + #5): `manager` is a free-text, self-declared field
> that the WP-37 sentinel reservation does NOT police (that guard only blocks *registering* the
> name `compact-broker`, server.py:271 — not the `manager` field). Gating on "manager is a
> registered teammate" is the single check that closes three edges at once:
> - `manager == "compact-broker"` (exact) — unregistrable, so no record → no send (else it would
>   trip `send_dm`'s self-send guard and be silently swallowed).
> - `manager == "Compact-Broker"` (case variant) — no record → no send (else a phantom inbox
>   branded as the broker).
> - any mistyped/unregistered `manager` — no record → no send, no phantom-inbox litter.
> An **offline-but-registered** manager still has a record, so it still gets the durable inbox
> message and reads it on next wake — correct. This is one extra local `read_agent_record`; take
> the cost.

On the **manager-compacting-subordinate leg** (`manager == requester`, `target != requester`):
send NO alert — the manager is the requester; they already know, and a DM would be a self-echo.

### Mechanics (mirror the existing denial-audit pattern, tools.py:2109-2117)

- **Sender:** the `COMPACT_BROKER_SENDER` sentinel (`"compact-broker"`) — same label the denial
  audit and WP-38 completion/expiry notices use. System-generated notice, reserved name, can't
  be forged.
- **Ordering:** request file written FIRST (durable action), alert sent AFTER — same
  "observability never precedes delivery" ordering `send_dm` uses for its transcript tee. File
  write fails → no alert.
- **Best-effort + honest return (peer-review finding #3):** wrap the send in
  `try/except Exception`. Set a local `notified = False` before, `notified = True` only *after*
  a `send_dm` that returned without raising. An alert-send failure must NOT fail the request or
  mask the success return (the file already landed) — but it must ALSO NOT let the return string
  claim a notification that did not happen.
- **Message body — lean** (project token-efficiency value). Suggested:
  `f"{requester} requested a self-compact (id {req_id[:8]}, TTL 900s). The broker will inject "
  f"at a safe point when they're idle."`
  Do NOT promise the manager a completion/expiry notice — per WP-37 those go to the *requester*
  only; routing them to the manager is broker-side (WP-38) and out of scope here.
- **Return string:** append the transparency line `f" Your manager {manager!r} was notified."`
  to the existing success return **only when `notified` is True**. On a swallowed failure the
  base success string returns unchanged (no false claim).

### Doc touchpoints (grep `request_compact` in each; patch only where prose contradicts)

- Tool description (tools.py:588-596): add one lean clause — a self-compact notifies the
  requester's manager if one is registered.
- `DESIGN.md` (~:401), `README.md` (~:61), `SKILL.md` (~:36), `CHANGELOG.md` (~:15-18) — the four
  files the reviewer confirmed mention this tool. Update only the ones whose flow description now
  omits/contradicts the manager alert. CHANGELOG gets one line under a new unreleased entry.

## Acceptance criteria

Assert on the **`send_dm` invocation** (spy/monkeypatch `teammate_comms.tools.send_dm`,
capturing `to`/`sender`), not merely inbox contents — the guard conditions are only *proven*
load-bearing at the call boundary (peer-review finding #1).

- **AC-1 (subordinate self-compact alerts a registered manager):** register A; register B with
  `manager="A"`; B calls `target=B` → request file lands AND exactly one `send_dm` fires with
  `to == "A"`, `sender == COMPACT_BROKER_SENDER`, body naming B. The message is in A's inbox.
  Return string ends with the "manager … was notified" line.
- **AC-2 (no manager → no alert, no error):** register S with no manager; S calls `target=S` →
  request file lands, **zero** `send_dm` calls (spy asserts not-called), no error, return string
  has NO "notified" line. (Spy assertion is required — an inbox-only "S has no alert" check can't
  distinguish the guard from `send_dm`'s own `None`-validation swallow.)
- **AC-3 (manager-compacts-subordinate → NO self-echo):** A calls `target=B` (B.manager==A) →
  file lands; **zero** alert `send_dm` calls; A's inbox stays clean.
- **AC-4 (unregistered manager → no send, honest return):** register U with `manager="ghost"`
  (never registered); U calls `target=U` → file lands, zero `send_dm` calls, no phantom inbox
  created for "ghost", return string has NO "notified" line.
- **AC-5 (best-effort isolation + honest return under failure):** monkeypatch `send_dm` to raise
  on the alert call; the well-formed self-compact (B/manager=A) still returns success and the
  file still lands, AND the return string does NOT claim "notified" (i.e. `notified` stayed
  False). Proves the try/except isolates the request and the flag gates the claim.
- **AC-6 (anti-regression):** all existing WP-37 ACs in `tests/test_compact.py` still pass —
  denial audit DM, six-key file contract, sentinel reservation guard, anti-spoof.
- **AC-7:** all four suites green on Windows (`uv run --no-sync python tests/<suite>.py`).

## Known-intentional — do NOT "fix"

- Alert fires ONLY on the self-compact leg, never the manager-compact leg. Deliberate.
- Manager alerted at REQUEST time only. Broker completion/expiry notices still go to the
  requester, not the manager — routing those to the manager is broker-side (WP-38), out of scope.
- No opt-out/config flag, no dedup/rate-limit on the alert — cooperative-team model, keep simple.
- **DOCUMENTED RESIDUAL RISK (do not fix here) — peer-review finding #4:** `manager` is a
  self-declared, unverified field (since WP-36). WP-39 makes it, for the first time, a *delivery
  address*: an agent can direct broker-branded "X requested a self-compact" DMs at any registered
  third party by naming them `manager`, with no consent or rate-limit. Not privilege escalation
  (no authz gained), but a new directed-DM annoyance channel. **Accepted** for the
  cooperative-agents threat model, consistent with WP-37's accepted identity-squatting residual;
  flagged here so it is a conscious sign-off, not a silent gap. Revisit if the threat model hardens.

## Gate (Silvie)

I run the four suites at your pinned SHA in an isolated worktree (`git -C <abs>` on every
invocation; provenance-check the import path resolves to the worktree, not the main checkout).
I hand-verify AC-1 (B self-compacts → A's inbox shows the compact-broker alert) and AC-3 (A
compacts B → A's inbox stays clean) against the built server. Tautology proof is at the **call
boundary**, not a one-guard revert: with the alert `send_dm` spied, AC-1 must show exactly one
call to `to==A` and AC-2/AC-3/AC-4 must show zero — deleting the alert block fails AC-1; making
it fire unconditionally fails AC-2/AC-3/AC-4. I diff against `git merge-base main <branch>`.
Handoff includes `For-the-record:`.
