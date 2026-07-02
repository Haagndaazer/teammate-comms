# WP-32 — Docs mega-pass + v0.12.0 release prep

> Owner: Svetlana. Gate: Silvie (docs verified line-by-line against code at the gate).
> Branch: `fix/fable-audit-260701`. LAST WP — everything else must be gated first.
> Audit findings: X1–X6 (Stage-1), X4/X7 (Stage-1), N2/N3/N4/N13 (Stage-3), T3/D1-doc, G6,
> W3-doc, H6-doc. Cognition tasks: `58262dd8a212`, `2ec3a014b36f`, `d9af4a8af567`,
> `f194d7cfc1fa`, `14f6b52fe059`, `1d46603d1016`, `db4322e285a7`, `a96186336862`,
> `a8852f578a7d`, `4b56f68ceab3`, `a3fa50c8c6c8`.

## Scope (per doc)

### README.md
1. **Quickstart (N2, d9af4a8af567):** a runnable two-instance walkthrough — instance A
   `teammate_register(agent="alice")`, instance B `register(agent="bob")` → A
   `teammate_send(to="bob", ...)` → B's wake → B `teammate_inbox` → `teammate_ack("all")` →
   reply. Copy-pasteable, like the existing group-chat script.
2. **Troubleshooting section (N4/N1, f194d7cfc1fa):** symptom→cause table: teammate_* tools
   absent (uv missing / bash+Git-Bash missing / first-install needs one restart — check
   `/mcp`); teammate not receiving (compare `teammate_whoami` comms_root on both sides — G4;
   check teammate_list liveness; debug log at ~/.claude/debug/<session-id>.txt); dashboard
   blank (TEAMMATE_TRANSCRIPT=0 note; stale token after restart → re-run teammate_dashboard);
   reincarnate window never registers (trust prompt; whoami launch_args_override). Cross-link
   SKILL.md ↔ README both directions (the two docs never reference each other today).
3. **Tool table (X1/X2/X3, 2ec3a014b36f):** 18 tools incl. teammate_set_avatar; inbox row
   documents `since`/`limit`/`show_all` + body-suppression default; teammate_list row states
   the project-scoped default + `all=True` (reconcile with the "global by default" framing —
   comms are global, the LIST VIEW is project-scoped).
4. **Reincarnate enablement (N3, 14f6b52fe059):** per-OS safe enablement of
   TEAMMATE_REINCARNATE_ENABLED: set it for ONE session (PowerShell `$env:...=1` then launch;
   bash `TEAMMATE_REINCARNATE_ENABLED=1 claude`), why `setx`/profile-export is dangerous
   (propagation model: the var must exist BEFORE Claude Code launches; durable settings
   enable spawning machine-wide forever — cite that the durable-set warning exists in-tool
   after WP-24). Incident-informed (c1fa517c047d).
5. **Uninstall & upgrade (N13, 1d46603d1016):** what `/plugin uninstall` leaves behind
   (~/.claude/TeammateComms: inboxes, transcript/reactions/deletions NDJSON + state files,
   avatars, projects) and how to clean it; standing upgrade note (restart after update;
   marketplace pin flow).
6. **Cross-host transports (G6, a96186336862):** supported = one machine or NTP-synced hosts
   on a REAL shared filesystem honoring atomic rename (SMB/NFS with caveats); UNSUPPORTED =
   OneDrive/Dropbox-synced roots (conflicted-copy siblings are never read; documented
   data-loss mode). One honest paragraph.
7. **TEAMMATE_TRANSCRIPT=0 wording** already fixed in WP-2 — verify it survived; avatars
   section landed in WP-28; dashboard lifecycle below.

### SKILL.md
8. **Reliability contract rewrite (X4, 58262dd8a212):** replace the stale "a dropped push
   never loses a message — read on next inbox" claim with the honest WP-9 wording from DESIGN
   §7 (drops are real, GH #38736/#61797; capped re-nudge 120/240/480s ×3; residual: a
   permanently-dropped wake needs a manual teammate_inbox — the recovery affordance,
   a8852f578a7d). Cross-link README troubleshooting.
9. Keep @mention + diagnostics content; add the README cross-link header.

### DESIGN.md
10. **Version framing (X5):** header/embedded examples → v0.12.0; add the recurring-drift
    fix: a "Release doc-checklist" subsection (every tool/behavior change touches
    README+SKILL+DESIGN in the same PR; DESIGN version framing bumps with every release) —
    the enforcement hook the audit says was always missing.
11. **Trust model (T3/D1-doc, db4322e285a7):** the flat-trust paragraph (all local processes
    of this OS user are mutually trusting; `from` is advisory; author-delete is anti-footgun
    not authz; which tools are convention-gated: project_register/delete open-by-convention,
    teammate_delete open-for-offline, update/set_avatar self-only) — one authoritative
    section; tool descriptions already aligned by WP-31.
12. **Dashboard lifecycle (N12, 4b56f68ceab3):** dies with the hosting instance; fresh token
    per launch (bookmarks 403 after restart — expected); relaunch affordance = re-run
    teammate_dashboard (same URL while the instance lives — idempotent per process). README
    gets the 3-sentence user-facing version.
13. Sections for WP-14 avatars + WP-15 suppression (X6) incl. avatars.py in the §2 layout;
    the id-scheme paragraph landed in WP-26 and H5/H7/H8 in WP-30 — verify presence, don't
    duplicate.

### Release mechanics
14. **Version bump:** 0.12.0 across `__init__.py` / `pyproject.toml` / `plugin.json` (the
    three-way tests enforce agreement). CHANGELOG.md entry summarizing the epic by theme
    with WP references. Re-run ALL THREE suites AFTER the bump (peer-review #7).
15. **Tags (X7, a3fa50c8c6c8):** create LOCAL lightweight tags v0.7.0…v0.11.0 at each
    version's pin commit (`git log --grep="Pin marketplace sha"` names them) + v0.12.0 on
    the release commit once gated. DO NOT PUSH TAGS — pushing is Silvie's release step,
    Colton-gated.

## Acceptance criteria

- AC-1: every README claim added is verified against the CODE AS OF THIS BRANCH (the gate
  will spot-check: tool count vs TOOL_DEFINITIONS len, env-var tables vs os.environ.get
  greps, paths vs comms.py helpers).
- AC-2: SKILL.md contains no sentence contradicting DESIGN §7's honest contract (grep
  "never loses" etc.).
- AC-3: three suites green after the version bump; version tests prove the triplet.
- AC-4: `git tag --list` shows v0.7.0..v0.12.0 locally; none pushed.
- AC-5: docs contain no reference to per-WP branches (this epic is single-branch) and no
  stale "12 tools"/"17 tools" strings anywhere (grep).

## Known-intentional — do NOT "fix"

- The in-repo `.claude-plugin/marketplace.json` handling (E-4) stays UNTOUCHED — Colton-gated
  install-path change; not in this epic.
- No README promise of guaranteed wake delivery — the honest contract only.
- BACKLOG.md / AUDIT-v0.7.0.md / WP-1x docs are historical artifacts — do not update them.
