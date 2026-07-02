# WP-30 — Harness contracts: first-install signal, reinject self-filter, version stamps, housekeeping

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: H3 (high), P1/P3/N1 (critical theme — implementable slice), P5 (medium),
> H1 (medium), H2 (high), H5 (medium), H7/H8 (low).
> Cognition tasks: `e4c5fd263296`, `d0e5caeb18fd`, `54982be4879b`, `2f7eff973722`,
> `27fbaf6b8c96`.

## Findings being fixed

- **H3/P1 (first contact):** first install can't have the venv before the MCP spawn tries it;
  the documented fix ("restart once") is prose-only, and missing `uv`/`bash` present as the
  same silent "no teammate_* tools". The hook already warns for missing uv; nothing signals
  the restart case.
- **P5:** `reinject-instructions.sh` (the compact hook) has no defensive stdin self-filter
  (its sibling got one) and no test proves the matcher contract — AUDIT G-4 was recorded
  closed while half-open.
- **H1:** a mid-session plugin update splits instructions provenance: the running server's
  INSTRUCTIONS are spawn-frozen; the compact hook re-execs from disk — nothing stamps which
  version produced which text.
- **H2:** managed-settings allowlist detection is a schema guess verified against exactly one
  Claude Code build with no recorded expiry.
- **H5:** the entire standing-instructions contract assumes `initialize.instructions` reaches
  the model — never verified, nowhere stated.
- **H7/H8:** no tools/list_changed consideration recorded; `$CLAUDE_PLUGIN_ROOT` re-expanded
  by three consumers with no consistency note.

## Direction

1. **H3 first-install signal:** in `session-start.sh`, detect the FIRST build (stamp file
   absent before the sync) and, on a successful first sync, emit `additionalContext`:
   "teammate-comms just built its environment for the first time. If the teammate_* tools are
   not available in this session, restart Claude Code once (check with /mcp)." Subsequent
   sessions stay silent ('{}'). Missing-uv warning already exists — extend its text with the
   /mcp check hint. (Missing bash is undetectable from bash — record that residual ON task
   d0e5caeb18fd when closing it, per the queue's skip protocol; README troubleshooting is
   WP-32.)
2. **P5 reinject self-filter:** mirror session-start.sh's stdin pattern in
   reinject-instructions.sh: when stdin is not a tty, read it; if a `"source"` key is present
   AND its value is NOT "compact", emit '{}' and exit 0 (absent/unknown source → proceed, the
   matcher is the primary gate; the filter is defense-in-depth). Harness test in the WP-3
   hooks block style: feed `{"source":"startup"}` → expect '{}'; feed `{"source":"compact"}`
   → expect the instructions payload.
3. **H1 version stamps:** `instructions.py`'s `_REINJECT_HEADER` gains the package version
   (import `__version__` from the package — verify no import cycle; instructions.py currently
   imports nothing from the package, and `__init__.py` defines only `__version__`, so it's
   safe). The initialize handshake already carries serverInfo.version; additionally
   `server.py` logs its version at startup (one stderr line) so server-vs-disk drift is
   diagnosable from the debug log.
4. **H2 detection stamp:** constant `MANAGED_SETTINGS_VERIFIED = "Claude Code 2.1.161 / Windows"`
   in spawn.py next to `channel_allowlisted`, cited in its docstring; when the allowlisted
   path is CHOSEN, stderr-log one line naming the verification vintage and the
   TEAMMATE_LAUNCH_ARGS escape hatch. No runtime CC-version probe exists — the stamp makes the
   assumption visible where it acts.
5. **H5 statement:** DESIGN.md §instructions gains the explicit assumption + how to verify
   ("/mcp shows the server; the standing rules text appears in the session's MCP instructions
   block") — land the DESIGN paragraph HERE (it documents THIS subsystem's contract; WP-32
   only sweeps what's left).
6. **H7/H8 housekeeping (doc-level):** DESIGN note: tool list is static per process (no
   tools/list_changed needed — new tools arrive only via server respawn); enumerate the three
   `$CLAUDE_PLUGIN_ROOT` consumers (plugin.json spawn, session-start.sh, reinject hook) and
   the invariant that all three expand in the same session.

## Acceptance criteria

- AC-1: session-start.sh first-run (no stamp) emits the restart additionalContext; second run
  (stamp present, hash match) emits '{}'. Bash-level harness test (the hooks block pattern).
- AC-2: reinject self-filter: startup-source input → '{}'; compact-source input → payload
  containing the standing-rules text AND the version stamp (proves H1's header change too).
- AC-3: spawn.py's allowlisted-path stderr line appears when a crafted settings file trusts
  the channel (extend the existing channel_allowlisted unit checks).
- AC-4: DESIGN.md diff includes the H5 assumption + H7/H8 notes (gate = my read).
- AC-5: full harness green (three suites) on Windows.

## Known-intentional — do NOT "fix"

- Hooks stay fail-closed-but-visible ('{}' emission pattern) — extend, don't restructure.
- The venv stamp design (hash of pyproject+uv.lock[+avatars flag after WP-28]) stays.
- `--no-sync` in both the plugin spawn and the reinject hook stays (handshake speed).
- Do NOT attempt an in-band CC-version probe or a tools/list_changed emitter.
