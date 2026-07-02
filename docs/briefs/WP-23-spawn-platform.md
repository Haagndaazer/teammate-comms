# WP-23 — spawn.py: Windows shlex, child reaping, marketplace un-hardcode, LAUNCH_ARGS surfacing

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: W6 (medium), W5 (medium), W1 (high), H4 (medium).
> Cognition tasks: `5d8ffa03db4c`, `0fd06ee573ed`, `9091253cb1c5`.

## Findings being fixed

- **W6:** `shlex.split` defaults to POSIX rules on Windows — an unquoted Windows path in
  `$TEAMMATE_LAUNCH_ARGS` (`C:\Users\name`) is silently corrupted to `C:Usersname`, in a
  Windows-first module, on exactly the path a forked-marketplace adopter needs.
- **W5:** every `subprocess.Popen` handle is dropped — zombies accumulate on POSIX for the
  life of the server.
- **W1:** `MARKETPLACE = "coltondyck"` is hardcoded into PLUGIN_SPEC/allowlist matching — any
  fork/rehost makes every spawn load a nonexistent plugin ref, and the DEVNULL child makes the
  failure indistinguishable from success.
- **H4:** `TEAMMATE_LAUNCH_ARGS` silently bypasses the allowlist detection and inherits down
  the reincarnation chain forever, invisible in whoami.

## Direction

1. **W6:** `shlex.split(base, posix=(os.name != "nt"))`. Comment why (backslash-as-escape
   corrupts Windows paths). Note in the diff summary: on Windows, quoted args in
   LAUNCH_ARGS keep their quotes under posix=False — acceptable (document the contract:
   Windows values are whitespace-split, quotes preserved).
2. **W5:** module-level `_children = []`; every Popen handle appended; a tiny
   `_reap_children()` (iterate, `p.poll()`, drop finished) called at the top of
   `spawn_in_terminal`. On POSIX additionally try `signal.signal(signal.SIGCHLD,
   signal.SIG_IGN)` ONCE (module flag, wrapped in try/except — only valid in the main thread;
   spawn is called from the main server thread via dispatch, but guard anyway) so zombies
   never accumulate between spawns.
3. **W1:** marketplace resolution order: `$TEAMMATE_PLUGIN_MARKETPLACE` env override →
   best-effort derivation from `$CLAUDE_PLUGIN_ROOT` (the plugin cache path contains the
   marketplace directory name — parse defensively, e.g. a path segment following
   `marketplaces` or matching the known cache layout; return None on any doubt) → fallback
   `"coltondyck"`. Make `PLUGIN_SPEC`/`DANGEROUS_LAUNCH_ARGS`/`ALLOWLISTED_LAUNCH_ARGS`
   functions (or computed at call time) so the resolution is testable; `channel_allowlisted`
   matches against the RESOLVED marketplace. Pre-spawn: `shutil.which("claude")` → actionable
   CommsError ("claude CLI not on PATH — the spawned teammate cannot launch") BEFORE any
   Popen. The reincarnate success text names the exact plugin spec used and states that a
   fork must set TEAMMATE_PLUGIN_MARKETPLACE or TEAMMATE_LAUNCH_ARGS.
4. **H4:** when `TEAMMATE_LAUNCH_ARGS` is set: (a) `teammate_whoami` output gains
   `"launch_args_override": <value>`; (b) the reincarnate return text notes "launch override
   active (TEAMMATE_LAUNCH_ARGS) — allowlist detection bypassed". Inheritance itself STAYS
   (recorded as deliberate in spawn.py:131-134) — surfacing, not stripping.

## Acceptance criteria

- AC-1 (W6): on Windows, `build_claude_command` with TEAMMATE_LAUNCH_ARGS containing
  `C:\Users\test` yields an argv element containing the backslashes intact. Tautology: fails
  under posix=True.
- AC-2 (W5): `_children` collects handles; `_reap_children` drops a finished one (spawn a
  trivial `python -c pass` in the unit test — do NOT spawn terminals). SIGCHLD path guarded
  (test only that importing/calling on Windows doesn't raise).
- AC-3 (W1): with TEAMMATE_PLUGIN_MARKETPLACE=forkco, the built argv references
  `plugin:teammate-comms@forkco` and `channel_allowlisted` matches a settings file naming
  marketplace "forkco" (parametrize the existing allowlist unit tests). Unset → current
  behavior byte-identical (existing spawn tests must stay green unmodified).
- AC-4 (W1): reincarnate with `claude` absent from PATH raises the actionable error (mock
  shutil.which or PATH-scrub in a hermetic block) — no Popen attempted.
- AC-5 (H4): whoami includes launch_args_override iff env set.
- AC-6: full harness green (three suites) on Windows. The existing hostile-prompt injection
  test and list-form-argv invariants must remain untouched.

## Known-intentional — do NOT "fix"

- List-form argv end-to-end (never a shell string) — the injection defense. Keep it.
- Child stdio = DEVNULL (stdout purity) — keep; W1's "surface spawn failure" is the
  PRE-SPAWN which-check + honest text, not child output capture.
- `--permission-mode bypassPermissions` and the dangerous-flag fallback direction (fail
  toward the safe flag) stay as recorded decisions.
- TEAMMATE_LAUNCH_ARGS child inheritance stays (deliberate; spawn.py:131-134).
