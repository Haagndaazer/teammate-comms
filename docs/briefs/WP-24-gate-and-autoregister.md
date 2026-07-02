# WP-24 — Reincarnate-gate durable-set detection; $TEAMMATE_AGENT auto-register failure surfacing

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: W2 (high), S4 (medium). Cognition tasks: `2e9c79d023e2`, `e8be977680da`.
> Incident context: `c1fa517c047d` — TEAMMATE_REINCARNATE_ENABLED leaked machine-wide via a
> `setx` demo (2026-06-10); OS-level cleanup only, no code mitigation ever landed.

## Findings being fixed

- **W2:** the reincarnate gate is checked per-process with no session scoping — the natural
  way users persist env vars (`setx`, shell profile) silently enables process-spawning
  machine-wide forever, the opposite of "opt-in, default off". This exact leak already
  happened (incident above).
- **S4:** a CommsError during `$TEAMMATE_AGENT` auto-register (the reincarnate-child path) is
  a stderr log only — the child looks alive to its parent but never appears in the roster,
  and nothing in-conversation ever says why.

## Direction

1. **W2 — detect durable enablement and warn loudly (chosen policy: warn, not refuse):**
   a true session-scope is impossible for an inherited env var; the implementable mitigation
   is detection. Add a helper (spawn.py or tools.py) `_gate_durably_set()`:
   - Windows: `winreg` read of `HKCU\Environment` and
     `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment` for
     `TEAMMATE_REINCARNATE_ENABLED` (guard every call; missing → False).
   - POSIX: return None (undetectable — grepping shell profiles is out of scope).
   When the gate is ON and `_gate_durably_set()` is truthy, PREPEND to the reincarnate
   success text: "⚠ TEAMMATE_REINCARNATE_ENABLED is set DURABLY (registry/user env) — this
   enables process-spawning for every future session machine-wide. Recommended: remove the
   durable setting and set it per-session instead (see README)." Also stderr-log it once at
   first gate use. Rejected alternative (enforce refusal when durably set): would break a
   user who consciously chose durable enablement — warn-don't-block; docs (WP-32) carry the
   per-OS safe-enablement guidance.
2. **S4 — surface auto-register failure in-conversation:** `_maybe_auto_register` stores the
   failure string in a module global (e.g. `_auto_register_error = f"..."`), cleared on any
   later successful register. Surface it in BOTH places the agent will look:
   - `teammate_whoami` (unregistered form): add `"auto_register_error": <msg>` when set.
   - `_require_registered`'s CommsError text: append "Note: auto-register from
     $TEAMMATE_AGENT failed earlier: <msg>" when set.
   Keep the existing stderr log. State lives in server.py; tools.py reads it via a small
   accessor passed on ctx (follow the existing ctx["identity"]/ctx["register"] pattern —
   e.g. ctx["auto_register_error"] = callable returning the current value).

## Acceptance criteria

- AC-1 (W2): `_gate_durably_set` unit-tested with an injected fake winreg (or a
  settings_paths-style injection seam) — truthy when either hive names the var, False when
  absent, never raises when winreg APIs fail. Reincarnate text carries the warning iff gate
  on + durably set (hermetic: monkeypatch the helper).
- AC-2 (W2): gate-off behavior byte-identical (the harness's gated-off reincarnate probe at
  id 52 must stay green).
- AC-3 (S4): hermetic — force register_identity to raise inside _maybe_auto_register (bad
  TEAMMATE_AGENT like "../evil"): whoami reports auto_register_error; a messaging tool's
  not-registered error mentions it; after a successful register both are clean.
- AC-4 (S4 tautology): on current main the whoami output has no trace of the failure — the
  test must fail there.
- AC-5: full harness green (three suites) on Windows.

## Known-intentional — do NOT "fix"

- The gate stays opt-in-default-off; the CHILD-side strip in build_child_env stays.
- $TEAMMATE_AGENT auto-register itself stays best-effort/non-fatal (a bad env var must not
  kill the server) — we surface, we don't harden into a crash.
