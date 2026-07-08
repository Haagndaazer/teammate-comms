# WP-41 — Make teammate-comms registration opt-in (silent removal of the auto-register nudge)

**Manager/author:** Silvie · **Implementer:** Svetlana · **Branch:** `wp/optin-registration` off `main`
**Ships as:** v0.14.0 · **Release:** merge to `main`, then hand to **Loki** for the marketplace pin.

## Goal & decision

Human directive (Colton): registration should be **opt-in**, not auto-nudged. Agents currently
auto-register (often picking random names) because the MCP server pushes a "call
`teammate_register` at session start" directive on every session and re-injects it after a
compact. Remove all in-session nudging.

**Decision — "full silent removal" (chosen by Colton, 2026-07-08):** the MCP `initialize`
handshake carries **no instructions field at all**, and the compaction re-inject hook is
**deleted**. The toolset stays fully functional; it is simply never advertised in-session.
The user opts in by telling an agent to `teammate_register`.

**Rejected alternatives (do not re-open):**
- *Soften + keep discoverable* (a one-line "teammate-comms available — call register to join"):
  rejected by Colton in favour of zero in-session mention.
- *Literal "remove both hooks.json entries"*: rejected — it would delete the load-bearing
  **venv pre-build** hook (server-launch risk on fresh sessions) **and** leave the real nudge
  (the handshake `INSTRUCTIONS`) intact, so it wouldn't achieve the goal.

## Known-intentional — do NOT touch (these are deliberate, not bugs to "fix")

- **The matcherless venv-build SessionStart hook** (`hooks/session-start.sh` + its hooks.json
  entry) — load-bearing: it pre-builds the zero-dep venv so the server launches without
  blocking the stdio handshake. **Keep it, including its `"source":"compact"` self-filter.**
- **The `teammate_register` tool itself** and all its behavior (profile echo, collision
  warnings, unregistered→isError guards, WP-12 re-register generation bump). We are removing
  the *nudge*, not the *capability*. Tests that exercise the tool stay.
- **Auto-register from `TEAMMATE_AGENT` env var** (power-user shortcut) — unchanged.

## One direction per fix (settled — implement exactly this)

For the instructions module, **delete it** rather than empty it:
- Delete `src/teammate_comms/instructions.py` entirely (its only purpose was single-sourcing
  the reinject text; reinject is gone).
- In `server.py`, remove `from .instructions import INSTRUCTIONS` and **omit the
  `"instructions"` key** from the `initialize` result object (do not send `"instructions": ""`).
  MCP `instructions` is optional; omitting is the clean expression of "no instructions."

## File-by-file spec

1. **`hooks/hooks.json`** — remove the `{"matcher": "compact", ...}` SessionStart entry
   (the reinject hook). Keep the matcherless venv-build entry. Update the top-level
   `description` to drop "…and re-inject standing instructions after a compact".
   **JSON-validity callout:** after deleting the compact object, remove the now-trailing
   comma after the first hook block's closing `}` — otherwise the file is invalid JSON and
   the plugin fails to load hooks entirely. Validate the file parses before commit.

2. **`hooks/reinject-instructions.sh`** — **delete** (dead once the hook is removed).

3. **`src/teammate_comms/instructions.py`** — **delete** the file.

4. **`src/teammate_comms/server.py`** —
   - Remove the `from .instructions import INSTRUCTIONS` import (line ~31).
   - Remove the `"instructions": INSTRUCTIONS` key from the `initialize` result (line ~499).
   - Update the comment block at ~59-60 (it explains INSTRUCTIONS single-sourcing for the
     reinject hook — now obsolete) and the H1 comment at ~661-664 that references INSTRUCTIONS.

5. **`src/teammate_comms/__init__.py`** — bump `__version__` to `"0.14.0"`.

6. **`pyproject.toml`** — bump version to `0.14.0`.

6b. **`.claude-plugin/plugin.json`** — bump `version` to `0.14.0`. **Required:** the handshake
   suite runs a zero-tolerance version-sync check (`test_handshake.py` ~5350) asserting
   `__init__.py` == `plugin.json` == `pyproject.toml`. Bumping only two of the three fails the
   gate with `version drift: …`.

7. **`uv.lock`** — regenerate so it reflects `teammate-comms==0.14.0` (`uv lock`). NB: the lock
   is currently stale (shows an older version) and already dirty in the working tree — this WP
   closes that out. Commit the regenerated lock.

8. **`tests/test_handshake.py`** — update assertions to the new behavior (must be
   tautology-proof — assert the *specific absence*, so the test fails against reverted code).
   **There are FIVE reinject/instructions-coupled sites — all must be handled or the gate
   fails on unlisted red assertions:**
   - The handshake block (~535-544) currently asserts `instructions` contains `teammate_register`
     + "status as you work" + the authority rule. **Invert:** assert the `initialize` result
     has **no `instructions` field** (or it is empty) AND does not contain `teammate_register`.
   - **Delete** the compact re-injection block (~1121-1149): the single-source identity check,
     the "INSTRUCTIONS missing…" assertions, and the reinject `additionalContext` checks.
   - **Delete** the WP-30 AC-2 block (~4997-5054, "reinject-instructions.sh self-filter +
     version stamp") — it subprocess-runs the now-deleted script via a fake `uv` shim and
     asserts on its stdout; with the script gone it fails.
   - **Delete** the WP-30 AC-4 block (~5056-5064) — it greps `DESIGN.md` for the literal
     headings `"H5 — the standing-instructions contract"` and `"H1 — version stamps"`; those
     sections are being removed/rewritten (step 9), so this guard is removed with them.
   - The script-existence/lint loop (~1933) iterates `("session-start.sh",
     "reinject-instructions.sh")` — drop `reinject-instructions.sh`.
   - **Keep** the version-sync check (~5350) — it now guards the three-way 0.14.0 bump (see 6b).
   - Leave all `teammate_register`-tool tests (register→identity, profile echo, unregistered
     guards, WP-12) untouched.

9. **Docs:**
   - `DESIGN.md` — the file-tree lines (~57, 65), the §6 reinject paragraph (~100-103), the
     initialize description (~204), and the **forward-looking** H5/H1 harness-contract notes
     (~229-257) describe the now-removed instructions/reinject design. Update to reflect:
     handshake sends no instructions; registration is opt-in; only the venv-build SessionStart
     hook remains. **Leave the historical changelog line ~880 (WP-16 fable-audit entry) alone**
     — it is a past-tense record of what that WP shipped; rewriting it would falsify history.
     (Removing the H5/H1 headings is coupled to deleting the WP-30 AC-4 test grep in step 8.)
   - `README.md` — the "standing instructions reach the agent via the handshake + reinject
     after compact" paragraph (~305-309) and the file-tree entries (~57, 65). Rewrite to:
     registration is opt-in (call `teammate_register` when you want to join); no auto-nudge.
   - `skills/teammate-comms/SKILL.md` — the "Startup protocol" paragraph (~55-59) currently
     says "as soon as the session begins, call `teammate_register`". Reword to opt-in: call
     `teammate_register` when you (or the user) want this instance to join comms. Do not leave
     the "as soon as the session begins" auto-directive — it now contradicts the shipped behavior.
   - `CHANGELOG.md` — add a `## v0.14.0` entry at the top (see below).

## CHANGELOG v0.14.0 (draft — implementer may refine wording)

> **WP-41 — registration is now opt-in; the auto-register nudge is removed.** The MCP
> `initialize` handshake no longer carries a standing-instructions block, and the
> post-compaction re-injection hook (`hooks/reinject-instructions.sh`) is deleted along with
> `teammate_comms.instructions`. Agents are no longer told to call `teammate_register` at
> session start, so they no longer auto-register under arbitrary names. The toolset is fully
> intact — register explicitly (`teammate_register`) or set `TEAMMATE_AGENT` to join. The
> load-bearing venv-build SessionStart hook is unchanged.

## Acceptance criteria (checked at the gate)

- **AC-1** Fresh handshake: `initialize` result has **no `instructions` field** and no mention
  of `teammate_register`. Server launches normally; all 19 tools still listed.
- **AC-2** `teammate_register` (and `TEAMMATE_AGENT` auto-register) still work end-to-end.
- **AC-3** `hooks/reinject-instructions.sh` and `src/teammate_comms/instructions.py` are gone;
  no remaining import/reference to either (grep clean across src, hooks, tests, docs) — and no
  surviving "call teammate_register at session start" directive anywhere shipped (SKILL.md too).
- **AC-4** venv-build SessionStart hook intact and still self-filters on `compact`.
- **AC-5** Version is `0.14.0` in **all three** of `__init__.py`, `pyproject.toml`, and
  `.claude-plugin/plugin.json`; `uv.lock` regenerated to match.
- **AC-6** Handshake test inverted and tautology-proof (fails against the reverted code); the
  four other reinject/instructions-coupled test sites (step 8) removed; version-sync check kept.
- **AC-7** Docs (DESIGN/README/SKILL/CHANGELOG) reflect opt-in registration; no stale reinject
  prose; historical DESIGN changelog line untouched.

## Gate command (pinned — run the identical invocation)

```
uv sync
uv run --no-sync python tests/test_handshake.py
uv run --no-sync python tests/test_wp13_projects.py
uv run --no-sync python tests/test_wp14_avatars.py
uv run --no-sync python tests/test_compact.py
```

All four suites green. Silvie re-runs them at the pinned commit in an isolated worktree
before sign-off (do not trust the handed green).

## GATE ROUND 1 — bounce delta (Silvie, 2026-07-08, @a19d88a)

Round-1 diff was correct on everything specced, all four suites green — but the gate
(adversarial reviewer + confirmed by Silvie) found the nudge-surface inventory was
incomplete. AC-3 ("no surviving 'register at session start' directive anywhere shipped")
is **not** met by removing only the handshake instructions. Two survivors to fix:

- **[BLOCKER] `src/teammate_comms/tools.py:244-245`** — the `teammate_register` tool
  description still reads *"Establish this instance's identity (call once at session start,
  like the old setup step)."* This ships via `tools/list` **every session** and is what the
  model reads to decide when to call the tool — a stronger nudge than the field we removed.
  Reword to opt-in: drop "call once at session start, like the old setup step"; state that
  registration establishes identity + arms the channel, called when you want to join comms.
  **Check the WP-31 schema-grep test block doesn't assert on the old phrase** — re-run the
  full gate.
- **[SHOULD-FIX] `DESIGN.md:781` (§11)** — still says *"Then at session start call
  `teammate_register`"*. Reword to opt-in, consistent with §6 (DESIGN ~64-67).

Not required (conscious call): the deleted WP-30 AC-4 grep also guarded two unrelated needles
("H7/H8 housekeeping", "tools/list_changed") that still exist in DESIGN — leaving that
coverage dropped is fine; don't re-add a trimmed grep.

Re-confirm AC-3 with a repo-wide sweep (`grep -ri "at session start" src docs skills`) before
handing back — only non-shipping/brief text should remain.

## Handoff protocol

- Fix + proof in the same commit. Post the branch + commit SHA when ready for the gate.
- Include a **`For-the-record:`** field with any durable facts (surprises, decisions made
  during implementation) for Silvie to record to cognition.
- Do **not** commit `.cognition/journal.jsonl` on the WP branch (shared-checkout rule).
- Before any destructive git op (`reset`, `clean`, `stash`, `checkout -- <journal>`), ping
  Silvie to flush first.
