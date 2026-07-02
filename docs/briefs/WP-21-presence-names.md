# WP-21 — Human presence staleness; name case-folding; Windows reserved names

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: B1 (high, Stage-2), G2 (high, Stage-2), G5 (medium, Stage-2).
> Cognition tasks: `f35d5c1e4cb3`, `55e048ad55e9`, `0c05ca523891`.

## Findings being fixed

- **B1:** human presence is the flat unverified flag the liveness design explicitly rejected
  for agents: set "online" at dashboard start, flipped "away" only by graceful shutdown. Kill
  the terminal → "online" forever. Two dashboards sharing the default name cross-clobber
  ("away" from one marks the other, still-live operator offline).
- **G2:** agent/group names have no case-folding — Windows (case-insensitive FS) merges "Bob"
  and "bob" into one record; Linux splits them into two identities that never see each other.
  Same root, opposite outcomes, zero detection.
- **G5:** Windows reserved device names (`con`, `prn`, `aux`, `nul`, `com1-9`, `lpt1-9`) pass
  validation and then fail opaquely at file/lock creation.

## Direction

1. **B1 presence heartbeat:** `start_dashboard` stamps `presenceAt` (now_timestamp) and
   `dashboard_pid` (os.getpid()) alongside `presence="online"`. The dashboard refreshes
   `presenceAt` on `/api/poll`, throttled to ≥15s between writes (a module/state var holding
   the last-stamp monotonic time; the poll arrives ~1.5s so this is ~1 write per 15s).
   Consumers (`_handle_list` human row, `_format_profile`, `_api_conversations`) treat
   `presence == "online"` as online ONLY when `presenceAt` is fresh (≤60s).
   **Back-compat (peer-review blocker #4, MANDATORY): a record with NO `presenceAt` key is
   treated as today — trust the flag.** Pre-existing human records must not all flip to away
   on deploy.
   **Clobber guard:** `shutdown_dashboard` writes `presence="away"` ONLY when the record's
   `dashboard_pid` equals our pid (read-before-write under the same best-effort lock
   discipline write_agent_record already has). A second dashboard's shutdown then can't mark
   the first operator away; the first's stale flag also can't stick (staleness covers it).
2. **G2 case-fold policy — reject case-variant collisions at register:** in
   `register_identity` (and `register_human`), scan the agents dir for an existing record
   whose `name.lower() == agent.lower()` but name != agent → raise CommsError that NAMES THE
   EXISTING SPELLING ("a teammate is already registered as 'Bob' — register with that exact
   spelling, or pick a distinct name"). The retry self-corrects (peer-review #13). Exact-match
   re-register stays untouched. Group names: same check at `teammate_group create` only (open
   membership means member NAMES stay free-form — they're addresses, not identities; note this
   in the group-create docstring). CHANGELOG note required (behavior change: previously two
   shadow identities on Linux / silent merge on Windows).
3. **G5 reserved names:** a module-level frozenset + check in `validate_agent_name`,
   `validate_group_name`, and per-`/`-component in `validate_project_key`: reject when the
   FIRST DOT-SEGMENT, lower-cased, is one of `con prn aux nul com1..com9 lpt1..lpt9`
   ("con", "con.helper" rejected; "console", "con-bot" fine). Reject on ALL OSes (a shared
   comms root can be consumed from Windows even when the writer is on Linux — cross-OS is the
   whole point). Error text says why ("reserved device name on Windows").

## Addendum (from the WP-19 gate, 2026-07-02)

- WP-19's D1 dashboard warning fires on ANY host mismatch in the existing human record —
  including a long-dead dashboard's stale host. Once presenceAt exists (item 1), gate that
  warning on presence-freshness: warn only when the existing record shows FRESH presence
  from a different host/pid (a dead dashboard's record is a silent takeover, which is fine).

## Acceptance criteria

- AC-1 (B1): fresh `presenceAt` → human shows online in list/profile/conversations; aged
  `presenceAt` (write a stale timestamp) → shows away; record with NO presenceAt + presence
  online → shows online (back-compat).
- AC-2 (B1): shutdown with a FOREIGN dashboard_pid on the record leaves presence untouched;
  with our pid → away.
- AC-3 (G2): register "bob" over an existing "Bob" record raises; error contains "Bob".
  Registering the exact "Bob" again succeeds (idempotent re-register). Tautology: fails on
  current main (silently proceeds).
- AC-4 (G5): each of "con", "NUL", "com3", "con.helper" rejected by agent+group+project
  validators; "console", "con-bot", "lpt10" accepted. Existing valid names unaffected.
- AC-5: the pipe harness's HUMAN presence assertions (register→online, shutdown→away — see
  the "presence is read AFTER the process exits" block) stay green.
- AC-6: full harness green (three suites) on Windows.

## Known-intentional — do NOT "fix"

- The human record deliberately has NO channel/pid keys (never wakeable, never trips the
  agent collision guard) — presenceAt/dashboard_pid are ADDITIVE fields, not a channel.
- Names-are-identity (a re-registered name inherits the predecessor's transcript attribution)
  stays — G2 only rejects CASE-VARIANT new spellings.
- Open group membership stays; only group CREATION gets the case check.
