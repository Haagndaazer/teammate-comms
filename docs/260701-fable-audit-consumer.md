# Fable Audit — Stage 3: Consumer audit — 2026-07-01

> Fact-finding only. Nothing here has been fixed. Tasks are filed under the `fable-audit epic (260701)` (node `291c795d20af`). Companions: `docs/260701-fable-audit-systems.md` (Stage 1), `docs/260701-fable-audit-interconnectivity.md` (Stage 2).

## Intended purpose (confirmed with human)

teammate-comms is a Claude Code plugin (pure-stdlib MCP server) that lets independent, human-launched full Claude Code instances message each other and wake each other from idle — closing the gap that built-in `SendMessage` only covers parent→subagent. It provides identity/registration, DMs, groups, reactions, profiles, a local dashboard where the human is a first-class teammate, and opt-in "reincarnate" spawning. **Audit yardstick (human-confirmed): THIRD-PARTY ADOPTERS** — a brand-new Claude Code user/team installing from the marketplace with zero context. Definition of success: install the plugin, register via `teammate_register`, and reliably send/receive/wake across two or more full instances (cross-project, cross-OS) with no polling and no ports.

## Scope of this stage

Three Sonnet 5 subagents, each a distinct newcomer persona: (1) a **literal walkthrough** of README.md top-to-bottom, logging every stuck/guess/stale point against the code; (2) the **zero-context agent** consuming only the MCP tool surface (names, descriptions, schemas, error strings) with no docs; (3) the **advanced-flow consumer** (human operator daily use, team scaler, spawner, operator-in-trouble, upgrader/uninstaller). Findings that corroborate already-filed Stage 1/2 tasks are noted as such; only genuinely new gaps got new tasks.

## Findings

### The literal README walkthrough

#### N1. First contact: one symptom, three silent causes, no differentiating diagnostic  [severity: critical]  [type: broken-assumption]
- Missing `uv`, missing Git Bash, and normal first-sync timing all present identically as "the teammate_* tools aren't there," and README's remediation ("restart Claude Code once") gives no way to verify success — the README never even mentions `/mcp` as the check. **Corroborates Stage-1 P1/P3 by concrete walkthrough; covered by task `d0e5caeb18fd`.**

#### N2. The plugin's headline flow has no worked example  [severity: high]  [type: gap]
- **What:** Group chat and project profiles each get a runnable, copy-pasteable script in README; the core value proposition — two full instances registering and exchanging a first message — has none. The newcomer must assemble it from the reference table.
- **Evidence:** `README.md:100-108` (group script) vs `README.md:227-228` (a single unpaired `teammate_register` with no matching send/receive/wake sequence).
- **History:** No graph history — genuine, previously unfiled oversight.
- **Impact:** The success bar's exact scenario is the one flow the docs never demonstrate end to end.

#### N3. Reincarnate's enable-gate has no safe documented command — a regression, not an omission  [severity: high]  [type: broken-assumption]
- **What:** README says the tool is gated by `TEAMMATE_REINCARNATE_ENABLED` but shows no command to set it on any OS. Worse, the propagation model is undocumented and counterintuitive: the MCP server is forked at Claude Code startup, so setting the var mid-session (the newcomer's natural first move) silently does nothing, while the durable methods (`setx`, shell profile) permanently enable spawning machine-wide — the opposite of the "opt-in, default off" framing, and the exact shape of the prior incident.
- **Evidence:** `README.md:184-190`, `SKILL.md:25` (gate named, never shown); `tools.py:1504-1516`.
- **History:** Incident `c1fa517c047d` — the old `setx` demo leaked the flag machine-wide and was removed with **no safe replacement written back**. Task `2e9c79d023e2` covers the code-side session-scoping; the docs side was unfiled.

#### N4. There is no Troubleshooting section anywhere, and the real diagnostics live in a file README never links  [severity: high]  [type: blindspot]
- **What:** README's only diagnostic pointer is a table-cell aside on `teammate_whoami`. The actually useful guidance — check `/mcp`, the stderr debug log at `~/.claude/debug/<session-id>.txt`, the doctor report — exists only in SKILL.md, which README never references. The "my teammate isn't receiving messages" support walk (Stage-3 persona 4) requires assembling whoami + debug log + teammate_list from three unlinked places; a failed reincarnate's candid diagnosis exists only inside that one tool's return text. The dashboard section also omits its own precondition (`teammate_dashboard` errors if you haven't registered).
- **Evidence:** `README.md:26` vs `SKILL.md:106-116`; `tools.py:1547-1555` (in-call-only reincarnate guidance); `tools.py:1558-1566` vs `README.md:163-177`.
- **History:** No graph history — unfiled oversight.

#### N5. Corroborated stale-docs findings — the walkthrough confirms they bite in practice
- 17-vs-18 tool tables, `teammate_inbox`'s undocumented `since`/`limit`/`show_all`, `teammate_list` scoping contradicting "global by default": each concretely misleads the literal reader (tasks `2ec3a014b36f`, `58262dd8a212`). Avatars unreachable + impossible `pip` remediation (task `a908cfa546cc`). Hardcoded `coltondyck` marketplace — verified live via WebFetch that it currently matches, so the failure is invisible until someone forks (task `0fd06ee573ed`).
- Positive control: the Marketplace section, group chat, reactions, delete, and project-profile sections were verified letter-for-letter accurate.

### The zero-context agent (tool surface only)

#### N6. Group "membership" is cosmetic and nothing tells the agent  [severity: medium]  [type: broken-assumption]
- **What:** The `teammate_group` description invites a Slack-style mental model, but no action except `delete` checks membership — any registered agent can read any group's full `history` sight-unseen. Open membership for join/add is a recorded deliberate decision; the equally-open *reading* side and the gap between the "brainstorm with teammates" framing and the actual access model were never weighed anywhere.
- **Evidence:** `tools.py:1277-1279` (history: existence check only), `1238-1244`, `1164` ("F-2: open membership is intentional"); description at `tools.py:306-317`.
- **History:** `4152caab6808`/`70894fe7bfc3` cover join/add openness — the read side is an undocumented extension of it. Oversight in framing, deliberate in mechanism.
- **Impact:** An agent that assumes group privacy posts something sensitive to `#design`; nothing ever corrects the assumption.

#### N7. `@mention` syntax is invisible at the schema level  [severity: medium]  [type: gap]
- **What:** Group posts auto-detect `@name` and render `🔔(@you)` in recipients' inboxes, but no tool schema mentions the syntax — it lives only in SKILL.md. The WP-11b "move verbose guidance to SKILL.md" leanness pass (`2bf2ede47d91`, deliberate) swept a piece of core invocable behavior into the same bucket as trimmable prose.
- **Evidence:** `tools.py:209` ("Message body."), `565-577`, `1076-1078`; `SKILL.md:16`.

#### N8. Silent success on a nonexistent reaction target  [severity: medium]  [type: bug]
- Reacting to a mistyped message id returns "Reacted 👍 … to message X" while the reaction reaches nobody; ack and delete both raise actionable id-not-found errors for the same mistake. The tolerant fallback was designed for `TEAMMATE_TRANSCRIPT=0` and accidentally swallows typos too. **Folded into task `5d7b245c71f6` (react id-resolution unification).**

#### N9. Tool-surface polish gaps (bundled)  [severity: low-medium]  [type: unclear-instruction]
- Required-param failures are inconsistent: `'message' is required…` vs `Invalid agent name None. Use alphanumerics…` vs the bare `Invalid group name None.` — the latter two read as bad-value errors, not missing-parameter errors (`comms.py:80-84, 202-210` vs `tools.py:540-541`).
- The 64KB `MAX_MESSAGE_CHARS` cap is enforced but undisclosed in the schema, unlike profile fields which state their caps (`tools.py:80-84, 209` vs `128`).
- The shared `project` field description says "at registration" even inside `teammate_update`'s schema (`tools.py:104-107, 119-131`).
- `teammate_group`'s prose lists 7 of 10 actions; `mute`/`unmute`/`reads` hide in the enum text (`tools.py:306-324`).
- **History:** by-products of the deliberate WP-11b schema-leanness pass — the tradeoff was considered, these specific casualties were not.

#### N10. Output formats are ad-hoc per tool  [severity: medium]  [type: gap]
- **What:** `teammate_whoami` returns JSON; every other tool returns bespoke text with subtly different grammars — even the two `=== … === / --- id: … ---` block styles (inbox vs group history) differ in field order and tags. No shared renderer; a zero-context agent must relearn per tool and cannot parse across tools.
- **Evidence:** `tools.py:727-748` vs `1311-1321`; `840-853` vs `974-994` vs `1743-1767`; `943`.
- **History:** WP-11a/11b optimized token count per tool, never cross-tool shape — unexamined. Adjacent to (but distinct from) open leanness task `ee9f6d52b059`.

#### N11. Positive controls worth recording
- The startup sequence is genuinely discoverable from the tool surface alone (instructions text + register description spell out register → inbox → ack). `_require_registered`'s error is exemplary. The typo'd-recipient hedge in send's success text is a well-executed safety net (itself undocumented in any doc table). The ack `"all"` schema explanation is the standard the other schemas should match.

### The advanced-flow consumer

#### N12. The dashboard has no durable URL — daily human use requires re-asking an agent after every Claude Code restart  [severity: high]  [type: gap]
- **What:** Each `teammate_dashboard` call on a new server process mints a fresh token (and possibly port); the dashboard dies with its hosting instance. A bookmarked URL 403s forever after a restart, and the docs never describe this lifecycle — the "human as first-class teammate, run it daily" model implies a persistence that doesn't exist.
- **Evidence:** `dashboard.py:15-16, 454-493`; `tools.py:1558-1575`; README/SKILL silent on lifecycle.
- **History:** Security-first design (fresh token, loopback-only) whose consumer cost was never documented — oversight. The blank-page/no-diagnosis half is already task `7cb9fd8b88e3`.

#### N13. No uninstall or upgrade story at all  [severity: medium]  [type: gap]
- **What:** Nothing documents what `/plugin uninstall` leaves behind (`~/.claude/TeammateComms`: inboxes, transcript/reactions/deletions NDJSON, avatar sidecars) or how to clean it, and the only migration text in README is the one historical 0.3.0 note — no standing upgrade guidance as versions accumulate.
- **Evidence:** README greps for uninstall/cleanup: none; `README.md:296-301`.
- **History:** No graph history — unfiled oversight.

#### N14. Corroborated: the trust/authority model isn't learnable from docs (tasks `db4322e285a7`, `b03c67387760`); the dashboard operator's elevated delete powers (`dashboard.py:374-397`) have no documented parallel authorization story. The project-profile mental model, by contrast, is genuinely well documented — the one advanced journey learnable from docs alone.

## Summary & Recommendations

Measured against the confirmed yardstick — *can a zero-context third-party adopter actually reach "two instances reliably messaging and waking each other"?* — the answer today is: **only if nothing goes wrong, and several things go wrong by default.**

1. **The journey is documented everywhere except at its start and at its failure points.** Secondary features (groups, project profiles, reactions) have excellent, verified, runnable docs; the headline flow (N2) has no worked example, and no failure at any step has a troubleshooting path (N1, N4). The docs were written feature-by-feature as WPs shipped, never journey-by-journey — that's the root cause, and the fix is cheap: one quickstart script and one troubleshooting page would remove the two biggest consumer barriers.

2. **Critical operational knowledge is split across two docs that never reference each other.** SKILL.md holds the diagnostics, the mention syntax, and the (stale) reliability contract; README holds install and features. The agent reads one, the human reads the other, and neither is told the other exists (N4, N7, X4). Recommendation: cross-link deliberately, and treat "which surface does a fact live on" as a release-checklist question (extends task `2ec3a014b36f`).

3. **The tool surface guides the happy path well and the edges poorly.** Register-first, typo hedging, and ack semantics are genuinely strong (N11). The gaps are silent success (N8), invisible behavior (N7, N6), inconsistent error idioms and undisclosed caps (N9), and per-tool output grammars (N10). One consistency pass with a shared error/format idiom would fix the class.

4. **Operator lifecycles were never designed because the author never leaves their own habits.** Daily dashboard use across restarts (N12), enabling reincarnate safely (N3), uninstalling/upgrading (N13) — each works fine if you already know how the machine is set up, and each dead-ends a newcomer. This is the consumer-facing face of Stage 1's theme 1 (first-contact never walked on a clean machine).

5. **Stage 3 independently re-derived Stage 1/2's top findings by walking the docs** — first-contact silent failure, stale tool tables, avatar dead-end, marketplace hardcoding, dashboard blankness. That convergence from an entirely different method is strong evidence the task priorities already filed are the right ones.

## Potential tasks (checklist)

- [ ] README quickstart: runnable two-instance register → send → wake → inbox → ack walkthrough (N2) — priority: high
- [ ] Document safe, per-OS enablement of `TEAMMATE_REINCARNATE_ENABLED` incl. the propagation model (must be set before Claude Code launches; durable-set risks) (N3) — priority: high
- [ ] README Troubleshooting section: symptom→cause for first-contact failures, `/mcp` check, debug-log path, whoami doctor, failed-reincarnate walkthrough, dashboard-blank causes, dashboard register precondition; cross-link SKILL.md ↔ README (N4, N1) — priority: high
- [ ] Document the dashboard lifecycle (dies with hosting instance, fresh token per launch); consider a stable relaunch affordance for daily human use (N12) — priority: high
- [ ] Group visibility: state open-membership/open-read in the tool description, or gate history reads on membership (N6) — priority: normal
- [ ] Tool-surface polish: @mention schema hint, 64KB cap disclosure, required-param error wording, context-neutral shared field descriptions, complete action list in group prose (N7, N9) — priority: normal
- [ ] Unify tool output formats behind a shared renderer (consistent blocks/fields across the 18 tools) (N10) — priority: normal
- [ ] Uninstall & upgrade documentation: what persists in `~/.claude/TeammateComms`, how to clean it, standing upgrade guidance (N13) — priority: normal
- [ ] (Already filed, corroborated here): `d0e5caeb18fd` first-contact diagnostics; `2ec3a014b36f` + `58262dd8a212` docs sync; `a908cfa546cc` avatars installable; `0fd06ee573ed` marketplace hardcode; `2e9c79d023e2` gate session-scoping; `7cb9fd8b88e3` dashboard failure surfacing; `5d7b245c71f6` react id-resolution; `db4322e285a7`/`b03c67387760` trust model
