# WP-29 — Dashboard failure surfacing; comms-root divergence diagnostics; stable project identity

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: D4 (medium) + B2/B4 (folded), G4 (high), G3 (high).
> Cognition tasks: `7cb9fd8b88e3`, `c125b9d5d1b2`, `b111b11fd93a`.

## Findings being fixed

- **D4/B2/B4:** dashboard `poll()`/roster use bare `catch(e){}` — a stale token after a
  server restart (or any 401/500/network death) yields a silently frozen console. WP-4 added
  error surfacing for writes only. Also TEAMMATE_TRANSCRIPT=0 renders as an inexplicable
  blank ("No messages yet") indistinguishable from a bug.
- **G4:** peers that resolved DIFFERENT comms roots believe they're teammates and can never
  exchange a message; sender output is textually identical to a genuinely-offline recipient.
  True divergence is invisible by definition (different roots don't share files) — the
  implementable slice is fingerprints + diagnostics.
- **G3:** the default project key is `parent/name` of the cwd path — the same repo cloned at
  different paths on two machines silently splits the roster (the mirror-image of the F-4
  fix that introduced it).

## Direction

1. **D4 — status banner in static/index.html:** add a fixed connection-status element.
   `poll()`/roster fetch failures set it: distinguish (a) HTTP 401 → "session token expired —
   the hosting instance restarted; re-run teammate_dashboard for a fresh URL" (permanent, stop
   polling); (b) network/other errors → "connection lost — retrying…" with the existing poll
   cadence acting as the retry (clear the banner on the next success). Keep ALL rendering
   `textContent`-only under the existing CSP (no innerHTML — the audit verified XSS-clean;
   keep it that way). B4: when a fresh load succeeds but the records stream is empty AND
   `TEAMMATE_TRANSCRIPT` is off, we can't see the env from the browser — instead have
   `/api/poll`'s fresh response include `"transcript_enabled": <bool>` (server knows) and
   render a one-line note "message history disabled (TEAMMATE_TRANSCRIPT=0) — live tombstones
   only" instead of the misleading empty state.
2. **G4 — root fingerprint + diagnostics:** `register_identity` stamps `comms_root:
   str(root)` onto the agent record (it already logs it; now persist it). `_doctor_report`
   gains a `root_mismatches` list: agents whose recorded `comms_root` differs from the
   caller's root string (case-folded compare on Windows). send_dm's unregistered-recipient
   NOTE gains one sentence: "If NAME believes they are registered, compare `comms_root` in
   both sides' teammate_whoami — different roots cannot exchange messages." README
   troubleshooting (WP-32) will cross-reference; the tool text lands HERE.
3. **G3 — stable default project label (chosen policy: git-remote-derived, path fallback):**
   in `server.py`, extend the auto-fill: try `git -C <CLAUDE_PROJECT_DIR> remote get-url
   origin` (subprocess, 3s timeout, capture_output, any failure → fallback). Parse the URL
   with a PURE helper `_project_label_from_remote(url)` → `owner/repo` (handle
   https://host/owner/repo(.git), ssh git@host:owner/repo(.git); anything else → None) —
   unit-test the helper exhaustively, keep the subprocess path best-effort. Fallback remains
   `_project_label(path)` (parent/name). Explicit `project` args always win (unchanged).
   Consequence to state in the diff summary + CHANGELOG note: agents re-registering in a
   git repo with an origin get a NEW default label (one-time roster shift; list_projects'
   near-miss section already surfaces strays; profiles keyed on old labels can be updated via
   project_register key=...). This is the recorded trade: stability-across-machines beats
   label continuity, per the third-party-adopter yardstick. NOTE the label cap (100) and
   validate_profile_field still apply — reuse the existing truncation guard.

## Acceptance criteria

- AC-1 (D4): with the dashboard running, a forced 401 (poll with a bad token — unit-test the
  JS? No JS harness exists: assert at the HTTP layer that /api/poll returns proper codes, and
  assert index.html contains the banner element + handler strings via source grep, the
  established pattern for frontend checks in this harness) — plus transcript_enabled in the
  fresh poll response (HTTP-level assert, both env states).
- AC-2 (G4): after register, the record carries comms_root; doctor flags a crafted record
  with a different root string; send's note contains the whoami hint for unregistered
  recipients.
- AC-3 (G3): `_project_label_from_remote` unit matrix: https/.git, https no .git, ssh colon
  form, bare host path, garbage, empty → expected owner/repo or None. Auto-fill integration:
  in a temp git repo with a fake origin, register picks owner/repo; in a non-repo dir, falls
  back to parent/name (existing harness assertion for PROJECT stays green — note the harness
  sets CLAUDE_PROJECT_DIR to a fake path that is NOT a git repo, so the fallback path keeps
  it passing unchanged; verify that assumption).
- AC-4: full harness green (three suites) on Windows.

## Known-intentional — do NOT "fix"

- Loopback bind, per-launch token, token-in-query bootstrap (D-2 accepted+redacted), CSP,
  textContent-only rendering — all stay.
- The dashboard dying with its hosting instance is the documented lifecycle (WP-32 docs) —
  the 401 banner NAMES it, we don't add persistence.
- Auto-fill remaining overridable + `project` field free-text stays (decision 1d271c4a786f).
