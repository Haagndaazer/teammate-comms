# WP-28 — Avatars: self-only target, lifecycle GC, installable extra

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: T1 (high), D2 (medium), D3 (medium), P2 (high).
> Cognition tasks: `b03c67387760`, `efbad7e5b2c1`, `a908cfa546cc`.

## Findings being fixed

- **T1:** `teammate_set_avatar` accepts any `agent` target — any teammate can overwrite or
  clear another's avatar (silent griefing; inconsistent with self-only teammate_update).
- **D2:** avatar sidecars survive teammate removal (a name-reuser inherits a stranger's
  image via direct URL); `/avatar` serves any shape-valid name with no registration check.
- **D3:** `image_base64` is fully decoded BEFORE the 50MB byte cap — the base64 STRING is
  never length-checked (WP-14's own brief said "byte cap before decode").
- **P2:** Pillow sits behind an extra no adopter can install: session-start syncs `--no-dev`
  with no extras, and the error text prescribes `pip install teammate-comms[images]` — not on
  PyPI and not the uv-managed venv. Avatars are dead-on-arrival for every third-party
  adopter.

## Direction

1. **T1:** in `_handle_set_avatar`, if a target `agent` arg is present and != caller → raise
   CommsError ("avatars are self-owned — only NAME can change NAME's avatar"). Update the
   schema description ("Defaults to yourself; only your own avatar can be set"). The
   dashboard has no set-avatar endpoint (verified) — no operator path to preserve.
2. **D2:** `remove_agent` also unlinks `avatars/<name>.{png,ansi,txt}` (inside its WP-20
   locked/verified rework — coordinate: if WP-20 already landed, extend it; report which
   sidecars failed like the other paths). `/avatar` (dashboard `_api_avatar`): after name
   validation, `read_agent_record` — no record → 404 (same body as no-file).
3. **D3:** before `b64decode`: if `len(image_base64) > _MAX_SRC_BYTES * 4 // 3 + 4` → raise
   the same 50MB-cap CommsError (mention it's the encoded length). Keep the post-decode
   check.
4. **P2:** three coordinated changes:
   - `hooks/session-start.sh`: when `TEAMMATE_AVATARS_ENABLED=1`, run the sync with
     `--extra images`; INCLUDE the flag's value in the stamp-hash input (toggling the env
     var must invalidate the stamp and trigger a re-sync — otherwise enabling does nothing
     until pyproject changes).
   - `avatars.py` ImportError text becomes actionable + truthful:
     "Pillow is not installed. Set TEAMMATE_AVATARS_ENABLED=1 before launching Claude Code
     (re-syncs the plugin venv with the images extra on next session start), or run:
     uv sync --project <plugin-root> --extra images".
   - README gets a short "Avatars (optional)" section documenting the env var + the manual
     command (this WP, not WP-32 — docs ride the behavior).

## Acceptance criteria

- AC-1 (T1): set_avatar with agent=<other> raises; agent omitted or ==caller proceeds
  (Pillow-absent path acceptable — the authz check must fire BEFORE the Pillow import so the
  test runs without Pillow). Tautology: current main happily targets another agent.
- AC-2 (D2): remove_teammate deletes existing sidecars; `/avatar` for an unregistered name →
  404 even when a stale PNG exists on disk (craft one).
- AC-3 (D3): an over-long base64 string raises the cap error WITHOUT decoding (monkeypatch
  base64.b64decode to fail the test if called — proves pre-decode placement).
- AC-4 (P2): session-start.sh stamp hash input includes the avatars flag (bash-level test in
  the harness's hooks block style: run the script twice with the flag flipped, assert the
  stamp differs / re-sync attempted). ImportError text contains "TEAMMATE_AVATARS_ENABLED"
  and no "pip install".
- AC-5: `test_wp14_avatars.py` suite green (it runs in CI after WP-18 — keep its
  Pillow-optional structure intact).
- AC-6: full harness green (three suites) on Windows.

## Known-intentional — do NOT "fix"

- Zero-dep hot path: Pillow stays lazy-imported ONLY in ingest_avatar (recorded decision) —
  the D3 length check must sit before the import-dependent code without adding any top-level
  import.
- Pre-rendered sidecars (PNG+ANSI+ASCII at ingest) stay — serve paths remain stdlib.
- The avatar statusline subcommand's degrade-to-blank behavior stays.
