# WP-38 — Broker delivery CLI: `python -m teammate_comms.deliver`

> Owner: Svetlana. Gate: Silvie. Branch: `feat/compact-broker` (after WP-37; same PR).
> Source: Wellington reply 2026-07-05 (msg `2026-07-05T18:48:44.313294.14bf02`, item 4).
> Gap: the broker daemon is NOT an MCP client, so its completion/expiry notices currently
> land in a side-file nothing reads. It needs to deliver real teammate-comms DMs (as
> `compact-broker`) into the requester's inbox — same actor name as WP-37's denial path,
> so the audit trail reads as one voice.

## Chosen mechanism (and the rejected one)

A tiny CLI entry point that reuses `send_dm` IN-PROCESS. `send_dm` (tools.py:643) is
already a pure file-protocol function — sender-explicit, takes (root, team, sender, to,
message, ...), does lock + `_cap_unread` + atomic write + transcript tee, and a live
recipient is woken by their OWN channel watcher observing the inbox change. A fresh
process calling it gets every guarantee for free.

REJECTED: documenting the inbox file format so the broker (PowerShell) writes it
directly. That births a second implementation of the storage protocol (locks, cap,
transcript, id minting) in a foreign repo — the exact coupling the request-file contract
exists to avoid, and a read-modify-write race against our own writers the moment the
lock protocol drifts.

## What this WP adds

New module `src/teammate_comms/deliver.py` + `main()`, invoked as
`python -m teammate_comms.deliver` (precedent: `instructions.py`, `server.py`).

1. **Args** (argparse):
   - `--to <agent>` (required)
   - `--message <text>` OR message body on stdin when `--message` is absent (PowerShell
     quoting of multi-line bodies is hostile; stdin is the escape hatch). Stdin mechanism
     is mandated (peer-review finding): `sys.stdin.buffer.read().decode("utf-8-sig")` —
     text-mode `sys.stdin.read()` decodes with the locale code page on Windows and turns
     a BOM'd UTF-8 body into mojibake. Precedent one file away: server.py:584. Bodies
     that may start with `-` must use `--message=<text>` form or stdin (argparse eats
     option-like tokens).
   - `--sender <name>` (default `compact-broker`)
   - `--priority normal|urgent` (default normal), `--post-type` (optional, the four
     existing labels), `--reply-to <id>` (optional)
   - `--comms-dir <path>` (optional; else the same `resolve_comms_root(None)` the server
     uses — explicit → `TEAMMATE_COMMS_DIR` → `CLAUDE_CONFIG_DIR` → `~/.claude`), `--team`
     (optional). CONTRACT NOTE (peer-review finding): a daemon launched outside the
     agents' environment (Task Scheduler, bare PowerShell) can silently fall through to
     `~/.claude` while agents resolve `CLAUDE_CONFIG_DIR` — a DM queued into a root nobody
     reads, exit 0, no error. The broker MUST pass `--comms-dir` explicitly, derived from
     the same root whose `TeammateComms/compact-requests` it already watches (the PARENT
     of `TeammateComms`). Sent to Wellington as a contract note.
2. **Behavior**: validate via the same helpers the tool path uses (`validate_agent_name`
   on to/sender — so `to == sender` still raises), then call `send_dm` and print the
   returned message id + `live`/queued state to stdout. NO new delivery logic — the module
   is arg-parsing + one call. An empty/whitespace message → error (mirror
   `_clean_message`'s contract) — do not deliver blanks.
3. **Exit codes**: 0 on delivery; 2 on ANY bad input — `CommsError` OR argparse usage
   error (stock argparse also exits 2 with multi-line usage text; do not promise
   one-line stderr, and do not override `ArgumentParser.error`); 1 on unexpected
   exception. The broker branches on exit code, parses nothing.
4. **Broker invocation contract** (peer-review finding — the broker repo cannot derive
   the plugin's versioned cache path, and bare `python -m teammate_comms.deliver` is a
   `ModuleNotFoundError` on any system Python): at server startup, `main()` drops a
   runtime pointer file `<root>/TeammateComms/plugin-runtime.json` via
   `write_json_atomic`: `{"v": 1, "python": sys.executable, "plugin_root": <package
   parent dir resolved from __file__>, "version": <package version>, "written_at":
   now_timestamp()}`. The broker reads it fresh per invocation and runs
   `<python> -m teammate_comms.deliver ...` — `sys.executable` is the venv interpreter
   the live server imports the package from, so `-m` resolves; the file self-heals
   across plugin version bumps because every server start rewrites it. Cheap: one write
   at startup, best-effort (a failure must not block server boot).
5. **Docstring note**: local-trust boundary — anyone who can run this CLI can send as any
   name; that is the same trust domain as a broker that can already inject keystrokes
   into panes. Not an authz surface, and deliberately NOT restricted to the
   compact-broker sender (the dashboard/human tooling may reuse it). The MCP-side forgery
   path (an agent REGISTERING as `compact-broker` and sending look-alike notices in-band)
   is closed by WP-37's name reservation, not here.

## Acceptance criteria

- AC-1: `python -m teammate_comms.deliver --to X --message hi` (registered X, isolated
  comms root via `--comms-dir`) → message readable in X's unread file with
  `from: "compact-broker"`, valid minted id, transcript tee present; exit 0; stdout
  carries the id.
- AC-2: stdin mode — a UTF-8 body with a leading BOM, an embedded newline, AND a
  non-ASCII character arrives with EXACT expected content (byte-for-byte assert after
  `_clean_message` semantics — an exact-content check is what catches a locale-codepage
  decode).
- AC-3: `--to compact-broker` with default sender (to==sender) → exit 2, no file touched.
  Invalid agent name → exit 2. Empty message → exit 2. Missing `--to` (argparse path) →
  exit 2.
- AC-4: unregistered `--to` still QUEUES (ensure_inbox creates it) — parity with
  `_handle_send`'s queue-for-later semantics; exit 0. (The broker may notify a requester
  whose session just died.)
- AC-5 (rewritten per peer review — the naive interleaving test is theater; subprocess
  spawn latency dwarfs the critical section, so it passes even with `file_lock` deleted):
  - (a) deterministic: the test process acquires `file_lock(unread_file)` ITSELF, launches
    the CLI, asserts the CLI has NOT completed while the lock is held, releases within
    `file_lock`'s 10s timeout, then asserts both messages present. Tautology clause: (a)
    must FAIL with the `file_lock` context removed from `send_dm` — I run that revert at
    the gate.
  - (b) probabilistic fan-out: ~8–16 concurrent CLI processes to one inbox; all ids
    present afterward.
- AC-6: `plugin-runtime.json` appears under an isolated root after a server start, with
  the five v1 keys; a pointer-write failure (e.g. unwritable dir) does not prevent boot.
- AC-7: all four suites green on Windows (`uv run --no-sync python tests/<suite>.py`);
  WP-38 tests live in `tests/test_compact.py` alongside WP-36/37's.

## Known-intentional — do NOT "fix"

- Sender-explicit `send_dm` (no MCP identity) is a deliberate dashboard-era design — the
  CLI leans on it; do not add identity checks to `send_dm`.
- The self-send guard stays intact (WP-37 brief already forbids weakening it).
- No broker-side polling/ingestion dir, no watcher changes — the broker CALLS the CLI;
  nothing in the plugin reads `broker-notify\`.
- stdout invariant: the MCP server's stdout discipline (BOM-free UTF-8, \n) applies to
  the SERVER; the CLI prints plain text for a human/broker — but never import-time
  side-effects that could break `python -m teammate_comms.server` (keep deliver.py
  import-clean).

## Gate

I run the four suites at your pinned SHA in an isolated worktree and drive AC-1/AC-3 by
hand from PowerShell (the broker's actual caller). Handoff includes `For-the-record:`.
