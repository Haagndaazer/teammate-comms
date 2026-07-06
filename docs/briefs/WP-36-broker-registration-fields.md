# WP-36 — Registration auto-capture: WezTerm pane binding + manager field

> Owner: Svetlana. Gate: Silvie. Branch: `feat/compact-broker` (off origin/main).
> Source: agent-farm compaction-broker plan (Lord-Wellington spec, 2026-07-05, msg
> `2026-07-05T18:29:26.876226.14bf01`). Approved by Colton.
> Epic context: the broker daemon (Wellington's repo) injects `/compact` into a target's
> WezTerm pane via `wezterm cli send-text`. It needs each agent's (socket, pane) binding and
> a manager→subordinate relation in the roster. WP-37 builds the request tool on top.

## What this WP adds

Three registry-record fields captured at `register_identity` (server.py):

1. **`pane_id`** — `int(os.environ["WEZTERM_PANE"])` when present and parseable, else `None`.
   Malformed value (non-int) → `None`, never a registration failure.
2. **`wezterm_socket`** — basename of `WEZTERM_UNIX_SOCKET` (e.g. `gui-sock-87100`), else
   `None`. Use `Path(raw.strip()).name`; empty result → `None`. (Separate WezTerm windows =
   separate mux sockets; the broker keys targets by (socket, pane_id), so both are needed.)
3. **`manager`** — the agent this agent reports to. Precedence at register:
   `AGENT_MANAGER` env if set (Wellington's launcher sets it for subordinates), else a new
   optional `manager` param on `teammate_register`; neither → preserve existing (omit key
   from the write). Also a new optional `manager` param on `teammate_update` (set or, with
   empty string, clear). Validated with `validate_agent_name`: a malformed ENV value is
   dropped (parity with `spawned_by`); a malformed EXPLICIT param raises `CommsError`.
   CLEAR MECHANICS (peer-review finding): `validate_agent_name("")` always RAISES — an
   empty/whitespace `manager` param is a clear sentinel handled BEFORE validation is
   called; it writes `manager=None` (merge overwrites with None). Only a non-empty value
   goes through `validate_agent_name`.

Write mechanics — the load-bearing detail:

- `write_agent_record` is a field-level merge (comms.py:999); a key you omit survives.
  `pane_id` and `wezterm_socket` must therefore be passed on EVERY register — including as
  explicit `None` when the env vars are absent — so a re-register outside WezTerm clears a
  stale pane binding instead of inheriting one. A stale binding = the broker types `/compact`
  into whatever now owns that pane. `record.update(fields)` already overwrites with `None`;
  no change to `write_agent_record` itself.
- These are registry-record fields like `pid`/`host`/`spawned_by` — NOT `PROFILE_FIELDS`
  entries. `manager` is an authz key (WP-37 gates on it); it gets agent-name validation, not
  a free-text length cap.

Exposure (the broker's `agent list` joins on these):

- `teammate_profile` (`_format_profile`, tools.py:1110): always render all three lines —
  `manager:`, `wezterm_socket:`, `pane_id:` with `(not set)` when null, after the existing
  profile fields.
- `teammate_list` (`_handle_list`): one added line per agent, ONLY when set —
  `manager: <name>` and, when either pane field is set, `pane: <socket>#<pane_id>`. Keep it
  lean (open task ee9f6d52b059 is a token-efficiency audit; don't add noise for agents
  outside WezTerm).
- PRESENCE CHECKS USE `is not None`, NEVER TRUTHINESS (peer-review finding): WezTerm panes
  are 0-indexed — `pane_id=0` is the ORDINARY single-pane value, and the neighboring
  `_format_profile` idiom (`value if value else '(not set)'`) would silently render it as
  `(not set)`. Do not copy that idiom for `pane_id`.

## Acceptance criteria

- AC-1: register with `WEZTERM_PANE=42` + `WEZTERM_UNIX_SOCKET=<dir>/gui-sock-87100` →
  record carries `pane_id: 42` (int, not string) and `wezterm_socket: "gui-sock-87100"`.
  Repeat with `WEZTERM_PANE=0` → `pane_id: 0`, and profile/list render it as pane 0, NOT
  `(not set)`.
- AC-2 (stale-clear): seed a record with `pane_id`/`wezterm_socket` set, re-register with
  both env vars absent → both fields are `null` on disk. (Tautology check: with the
  explicit-None pass reverted, this test must fail.)
- AC-3: `WEZTERM_PANE="garbage"` → `pane_id` null, registration still succeeds.
- AC-4: `AGENT_MANAGER=Silvie` at register → `manager: "Silvie"`; env absent + explicit
  param → param wins; both absent → existing value preserved across re-register; invalid
  explicit param raises `CommsError`; `teammate_update(manager="")` clears the field.
- AC-5: `teammate_profile` shows all three; `teammate_list` shows the added lines only for
  agents that have them set.
- AC-6: new suite `tests/test_compact.py` (shared with WP-37) wired into ci.yml as a fourth
  named step per OS (pattern: WP-18); all four suites green on Windows via
  `uv run --no-sync python tests/<suite>.py`.

## Known-intentional — do NOT "fix"

- `write_agent_record` field-merge semantics stay exactly as they are — explicit-None pass
  is the chosen stale-clearing mechanism, not a merge redesign.
- `spawned_by` (launch provenance) and `manager` (command authority) are DIFFERENT facts —
  do not derive one from the other, even though Wellington's launcher will usually set both.
- Do NOT validate that `manager` names a currently-registered agent — the roster is
  eventually consistent (manager may register later, or live in another project).
- Pane fields are captured at register only — no heartbeat refresh. Agents re-register every
  session, so migration/refresh is automatic; the broker cross-checks pid/host and treats
  null pane fields as unreachable (its job, not ours).
- Most-recent-register-wins and the WP-19 epoch machinery are untouched.
- `teammate_reincarnate`/`build_child_env` (spawn.py) deliberately does NOT set
  `AGENT_MANAGER` — subordinate launching with manager authority is Wellington's launcher
  CLI's job; deriving manager from spawned_by would conflate provenance with authority.
  Scope boundary, not an omission.

## Gate

I run the four suites myself at your pinned SHA in an isolated worktree, plus the AC-2
tautology check against the reverted explicit-None pass. Handoff includes `For-the-record:`.
