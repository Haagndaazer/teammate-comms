# WP-19 — Identity ownership: instance_id/epoch + collisions surfaced in-conversation

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: I1 (critical, Stage-2), S2 (high, Stage-1), C5 (medium), D1-dashboard-half
> (Stage-1 #D1). Cognition tasks: `7b31a55cfae3`, `986cfcf479ae`.
> This is the root-cause fix for the audit's #1 structural theme: "identity has no owner."

## Findings being fixed

- **I1 (critical):** cross-host live name collision never resolves — both sides' watchers
  rewrite `pid`/`host` every 5s (record flap) and both drain the same `{name}_unread.json`
  (shared mailbox). The collision guard is host-gated pid-check → heartbeat fallback, so each
  side sees the other "alive", logs to stderr, and proceeds anyway.
- **S2 (high):** register-time collision is a stderr log only; the tool returns unconditional
  success. The most likely first-run mistake (two instances picking "claude") = silent
  identity hijack with a success-looking response.
- **C5:** adopting an existing OFFLINE record (possibly another project's teammate) is silent.
- **D1 (dashboard half):** two dashboards defaulting to the same human name merge identities
  silently. (Presence staleness/clobber is WP-21; HERE we only surface the collision at
  `start_dashboard` time.)

## Direction

1. **Record fields:** `register_identity` stamps `instance_id` (`uuid4().hex`, minted once per
   server process — module-level or on the Identity object) and `epoch` (int: previous
   record's epoch + 1, read under the same write; absent → 1) onto the agent record. The
   watcher's heartbeat write carries the same `instance_id` (NOT epoch — epoch is
   register-owned, like `type`).
2. **Flap kill (policy: most-recent-register wins; ADDENDUM 2026-07-02 — ownership model
   made precise after a TOCTOU war-game):** ownership transfers ONLY at register while the
   record is fresh; heartbeats never steal a fresh record.
   - Heartbeat permit (each tick, ONE record read folded with the muted-cache read): WRITE
     iff `record.instance_id` is absent/ours, OR the record is STALE (lastHeartbeat older
     than the 30s constant — reuse `is_channel_alive`'s staleness). Foreign + fresh → SKIP
     (demoted; stderr-log once per episode, local flag). Foreign + stale → write (legitimate
     re-claim over a dead winner) — the check re-runs every tick, so a dead winner never
     silences us forever (peer-review H).
   - Epoch is REGISTER-OWNED: heartbeat writes never include `epoch`.
   - TOCTOU tie-break: a competitor's heartbeat can read-before-our-register and merge-write
     its `instance_id` OVER our fresh registration (their merge keeps OUR epoch). Detection:
     `record.epoch == the epoch I minted at register` AND `record.instance_id` foreign → I am
     the stomped rightful owner → re-claim (write, incl. my instance_id). The stomper then
     reads foreign+fresh and demotes. Converges to most-recent-REGISTER in ≤2 ticks.
     Store "my epoch" on watcher/Identity state at register.
   - Extract the decision as a pure function `compute_heartbeat_permit(record,
     my_instance_id, my_epoch, now)` (the compute_reemit pattern) — hermetic one-liner tests.
   - The demotion skip must NOT skip the muted-cache refresh or the reaction-wake tick.
3. **Register warning IN the return text (S2):** when the existing record is another LIVE
   claimant (existing instance_id != ours and heartbeat fresh, or the current
   `is_channel_alive` check trips), still proceed (register wins, epoch bumps) but PREPEND a
   loud warning to the returned string: name the other claimant's host/pid, state that the
   previous holder will stop heartbeating and that two agents sharing a name split messages
   unpredictably, and suggest re-registering under a distinct name if this was accidental.
4. **Offline-adoption note (C5):** if the existing record is offline and its `project` differs
   from the caller's auto-filled/passed project, append a NOTE to the return text: "adopting
   existing identity previously used in project X — inherit its inbox + transcript
   attribution."
5. **Human guard (peer-review blocker #2 — MANDATORY):** if the existing record has
   `type == "human"`, `register_identity` RAISES CommsError (the operator's identity is not
   claimable by an agent; mirrors `remove_teammate`'s carve-out). The env auto-register path
   already catches CommsError and logs — verify that path stays non-fatal.
6. **Dashboard collision (D1 half):** in `_handle_dashboard`/`start_dashboard`, if the human
   record already exists with a DIFFERENT `host` (or a fresh `presenceAt` once WP-21 lands —
   for now host-difference is the signal) append a warning line to the tool's return text
   ("another dashboard may already be registered as 'human' from host X — pass human_name to
   distinguish operators").

## Acceptance criteria

- AC-1: after register, the record carries `instance_id` (32 hex) + `epoch` (int ≥1);
  re-register same name bumps epoch and keeps profile fields (existing behavior).
- AC-2 (flap kill): hermetic watcher-level test — build a record whose instance_id differs and
  whose lastHeartbeat is NOW: one watcher tick performs NO write (record unchanged on disk).
  Then age the foreign heartbeat >30s: the next tick DOES write. (Use the pure-function style
  if you extract the decision — a `compute_heartbeat_permit(record, my_instance_id, now)` pure
  helper + hermetic tests is the pattern this codebase prefers; see compute_reemit.)
- AC-3 (S2): registering a name whose record shows a live foreign claimant returns text
  containing a WARNING naming the other host/pid — asserted in a hermetic block (craft the
  record file directly, call register_identity, match the substring).
- AC-4 (human guard): register over a `type=human` record raises CommsError; the error text
  names the human. Tautology: this test fails on current main (register currently succeeds).
- AC-5: two-instance sanity via the pipe harness stays green (the normal single-owner path
  must not print warnings).
- AC-6: full harness green on Windows (three suites).

## Known-intentional — do NOT "fix"

- Heartbeat-only liveness for teammate_list (`pid_check=False`) stays.
- The global flat namespace itself stays (constraint `0ff2595c61ef`) — we surface collisions,
  we don't add namespacing.
- `write_agent_record`'s field-level merge stays — instance_id/epoch ride it like `type` does.
- Registration still WINS on collision (most-recent-register-wins is the chosen policy; the
  rejected alternative — refuse to register — would brick the common crash-restart flow where
  the stale record still looks alive for ≤30s). If you find a hole in this policy, ask me
  BEFORE implementing an alternative.

## Gate notes

Composition risk I will specifically check: the skip-write demotion (item 2) must not starve
the MUTED-GROUPS cache refresh or the reaction-wake tick — those live on the heartbeat branch
today. Restructure so the read+cache-refresh still happens on schedule even when the WRITE is
skipped. Also: `register_human` writes no instance_id — the human record must never trip the
foreign-claimant skip for an agent of the same name (the human guard in item 5 makes that
unreachable, but verify the watcher path tolerates instance_id-absent records).
