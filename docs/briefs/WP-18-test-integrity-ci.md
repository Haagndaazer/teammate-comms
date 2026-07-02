# WP-18 — Test-suite integrity + CI coverage

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: Q1 (critical), Q2 (high), Q3 (high), P4 (high) —
> `docs/260701-fable-audit-systems.md`. Cognition tasks: `a2fa123fa4dc`, `3fe8923a6042`.

## Findings being fixed

- **Q1 (critical):** CI runs ONE of the three suites — `test_wp13_projects.py` (697 LOC) and
  `test_wp14_avatars.py` (486 LOC) run only when a human remembers, and both have rotted.
- **Q2 (high):** `test_wp13_projects.py:411` hardcodes `dir="C:/cctmp"` — `FileNotFoundError`
  on any machine but the author's. Direct proof of Q1.
- **Q3 (high):** both un-wired suites assert version == literal `"0.10.0"` while the repo
  ships 0.11.0 — the drift guard itself drifted.
- **P4 (high):** no macOS CI leg despite darwin-specific branches (`spawn.py`
  managed-settings paths) and documented macOS support.

## Direction

1. **Q2:** drop the `dir=` argument — plain `tempfile.TemporaryDirectory(prefix="tc-wp13-")`.
2. **Q3:** DELETE the hardcoded version literals entirely; replace with the cross-file
   3-way equality pattern from `test_handshake.py` (tail of main(): reads `__init__.py`,
   `plugin.json`, `pyproject.toml` and asserts pkg == plug == pyp). Do NOT just bump the
   literal to the current version — that re-plants the exact bug (peer-review finding #6).
   If the suite wants to prove "the version the server reports matches the packaged version",
   compare against the value READ from pyproject, never a string constant.
3. **Q1:** `.github/workflows/ci.yml` — run all three suites as separate named steps (so a
   failure names the suite):
   `uv run --no-sync python tests/test_handshake.py` / `...test_wp13_projects.py` /
   `...test_wp14_avatars.py`. Keep `--no-sync` + the existing sync step.
4. **P4:** add `macos-latest` to the matrix. Keep `fail-fast: false`. NOTE: the pinned action
   SHAs stay EXACTLY as they are (supply-chain pins — see the comment block in ci.yml; do not
   "helpfully" bump them).

## Acceptance criteria

- AC-1: all three suites pass locally on Windows from a clean checkout of the branch
  (no `C:/cctmp` dependency — verify by grep that the string is gone from tests/).
- AC-2: no version literal (`"0.1x.0"`-shaped string constant compared against a version)
  remains in either wp13/wp14 suite — grep-proof in the diff.
- AC-3: ci.yml lists 3 os × 3 suite steps; actions SHAs unchanged.
- AC-4: post the full local output (tail) of each suite with the diff.

## Known-intentional

- The handshake harness's fixed `time.sleep` for the muted-cache refresh (documented inline as
  deliberately NOT deadline-polled) — leave it.
- `test_wp14_avatars.py`'s Pillow-optional skips (avatars extra absent → skip paths) are by
  design; wiring the suite into CI must not force-install Pillow (CI syncs no extras — the
  suite must stay green WITHOUT Pillow; if it currently hard-requires Pillow, gate those
  blocks on importability and note it in the diff summary).

## Gate notes

I will run all three suites at your SHA on Windows myself. macOS leg is verified on the first
CI run after the branch is pushed (I hold the push; you don't push).
