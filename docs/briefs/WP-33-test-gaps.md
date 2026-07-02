# WP-33 — Test gaps: avatars error paths; one true multi-process contention scenario

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: Q4 (medium), Q5 (medium) — `docs/260701-fable-audit-systems.md`.
> Cognition task: `d7791d327351`. Sequence AFTER WP-28 (its D3 change adds an error path
> these tests must cover) and before WP-32. Test-only WP — no product code changes; if a test
> here EXPOSES a product bug, stop and post it to me (it becomes its own fix+proof commit).

## Findings being fixed

- **Q4:** `avatars.py` error/edge paths have zero coverage: oversize source (>50MB), invalid
  base64, corrupt image bytes, zero-dimension image, decompression-bomb guard — only happy
  paths + Pillow-absent are tested.
- **Q5:** ALL concurrency coverage is thread-based in one interpreter; the product's actual
  shape — separate PROCESSES contending on the mkdir lock — is inferred, never proven.

## Direction

1. **Q4 (in `test_wp14_avatars.py`, gated on `_HAS_PILLOW` where Pillow is needed):**
   - oversize: monkeypatch `_MAX_SRC_BYTES` small (don't allocate 50MB) → CommsError names
     the cap. Also the WP-28 pre-decode length check: oversize base64 STRING raises WITHOUT
     b64decode being called (Pillow-independent — the check precedes the import; run it in
     the no-Pillow path too).
   - invalid base64 → CommsError "Invalid base64".
   - corrupt image bytes (b"not-an-image") → CommsError "Could not decode".
   - decompression bomb: a small-bytes/huge-pixels PNG (e.g. 20000×20000 1-bit) → the
     MAX_IMAGE_PIXELS guard raises → CommsError "Could not decode". Keep the crafted file
     tiny on disk.
   - zero-dimension: if constructible (Pillow rejects 0×0 creation — then cover the guard by
     unit-calling the size check or skip with a comment naming why it's unreachable).
2. **Q5 (in `test_handshake.py`, hermetic block):** N=4 `subprocess.Popen([sys.executable,
   "-c", <worker>])` workers, each appending M=25 records to the SAME
   `probe_unread.json` via `file_lock` + read/append/write (import path via PYTHONPATH=SRC,
   the harness's own env pattern). Parent waits (generous timeout), then asserts: file parses
   as JSON, contains exactly N×M records, no duplicates/losses (each worker stamps
   worker-id+seq). This proves the mkdir lock excludes across PROCESSES — the core promise.
   Keep total runtime < ~20s (tune N×M down if slow CI would flake; deadline-poll, no fixed
   sleeps).

## Acceptance criteria

- AC-1: each Q4 error path asserts the specific CommsError MESSAGE (reason, not just raise).
- AC-2: the pre-decode length check test proves b64decode was never called (monkeypatch
  sentinel).
- AC-3: Q5 test green on Windows locally; N×M records intact, zero lost — and it FAILS if
  you neuter the lock (verify once locally by monkeypatching file_lock to a no-op —
  the tautology check I will re-run at the gate).
- AC-4: no-Pillow run of wp14 suite still exits 0 (new tests skip or run per their needs).
- AC-5: full harness green (three suites) on Windows.

## Known-intentional — do NOT "fix"

- 50MB cap value, MAX_IMAGE_PIXELS=50M, 256×256 canvas — test them, don't change them.
- The thread-based contention tests stay (they catch a real Windows bug class) — Q5 ADDS the
  process dimension, replaces nothing.
