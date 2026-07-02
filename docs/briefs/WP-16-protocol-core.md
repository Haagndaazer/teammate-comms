# WP-16 — Protocol core: envelope guard + version negotiation + notification discipline

> Owner: Svetlana (implementer). Gate: Silvie (diff review at exact SHA).
> Branch: `fix/fable-audit-260701` (ALL WPs of this epic land here — user-directed single branch).
> Audit findings: S1 (critical), S3 (low), S5 (low) — `docs/260701-fable-audit-systems.md`.
> Cognition tasks: `8b193651d828` (S1), `66eb9c8afa55` (S3; S5 rides along).
> Fix + proof in the SAME commit. Full harness green on Windows before posting the diff.

## Findings being fixed

- **S1 (critical):** `server.py` main loop guards `json.loads` but not `handle(msg, ctx)`. A
  syntactically-valid non-object frame (bare scalar, `null`, or a spec-legal JSON-RPC batch
  array) raises uncaught `AttributeError` on `msg.get(...)` and kills the server — the process
  whose only job is to stay alive and wake the agent.
- **S3 (low):** `initialize` echoes `params.get("protocolVersion", "2025-06-18")` back to the
  client — a false compatibility claim. Answer with the version WE implement, always.
- **S5 (low):** the `initialize`/`ping`/`tools/list`/`tools/call` branches respond
  unconditionally — a notification-form request (no `id`) would get a spec-irregular
  `"id": null` frame. The unknown-method branch already does this correctly.

## Direction (one direction per fix — do it this way)

1. In `main()`'s stdin loop, after the `json.loads`: if `not isinstance(msg, dict)` →
   `respond_error(None, -32600, ...)` (JSON-RPC 2.0 prescribes a single -32600 with id null for
   an invalid request) and `continue`. Then wrap `handle(msg, ctx)` in try/except: on exception,
   log + `traceback.print_exc(file=sys.stderr)` (NEVER stdout), and if `msg.get("id")` is not
   None, best-effort `respond_error(id, -32603, ...)` — the loop must survive.
2. Add a module constant `PROTOCOL_VERSION = "2025-06-18"` near `SERVER_NAME`, with a comment
   saying it is OUR implemented revision, never an echo. `initialize` responds with it.
3. Each responding branch: skip the respond when `msg_id is None`. For `tools/call` as a
   notification, still EXECUTE the dispatch (a notification is processed, just not answered),
   only skip the respond.

## Acceptance criteria (pre-committed — the gate checks exactly these)

- AC-1: piping `null`, `"scalar"`, and `[{"jsonrpc":"2.0","id":63,"method":"ping"}]` lines to a
  live server leaves it able to answer a subsequent `ping`. At least one `-32600` error frame
  (id null) is emitted. Proven in the pipe section of `tests/test_handshake.py` (insert the
  sends right before `proc.stdin.close()`; request ids 63/64/65 are free — the harness docs
  say out-of-sequence ids are fine and `by_id` is order-independent).
- AC-2: a mid-session re-`initialize` with `protocolVersion: "1999-01-01"` (id 65) is answered
  with `"2025-06-18"` — proves non-echo (the FIRST initialize can't prove it: the harness sends
  the same version we'd answer).
- AC-3: a notification-form `ping` (no id) produces NO response frame. Assertion shape: no
  frame in `msgs` with `"id" in m and m["id"] is None and "result" in m` (the -32600 ERROR
  frames legitimately carry id null — don't over-assert).
- AC-4: a `handle()` crash is answered with `-32603` when the request had an id, and the loop
  survives. (If you can't induce a crash cheaply through the pipe, a hermetic unit check
  calling `server.handle()` with a method that raises via monkeypatched dispatch is fine —
  hermetic blocks live at the tail of `main()` before the version-sync check, own temp roots,
  `failures.append` style.)
- AC-5: stdout purity holds (the harness already asserts every stdout line parses as JSON-RPC;
  your traceback MUST go to stderr).
- AC-6: full harness green on Windows (`uv run --no-sync python tests/test_handshake.py`).

## Known-intentional — do NOT "fix" while in this file

- The `# Unknown notifications (no id): ignore.` branch is correct as-is.
- `iter(sys.stdin.buffer.readline, b"")` + `utf-8-sig` decode (BOM tolerance) is a recorded
  constraint — leave the read path alone.
- The watcher thread, `_maybe_auto_register`, and the `finally` offline-write are OUT of scope.

## Tautology check the gate will run

Your new tests will be run against the REVERTED fix — the S1 test must FAIL on current main
(server dies → later ping unanswered), and must assert the failure REASON (no reply to id 64 /
no -32600), not just "something threw".
