# Fable Audit — Stage 2: Interconnectivity audit — 2026-07-01

> Fact-finding only. Nothing here has been fixed. Tasks are filed under the `fable-audit epic (260701)` (node `291c795d20af`). Companion to `docs/260701-fable-audit-systems.md` (Stage 1).

## Intended purpose (confirmed with human)

teammate-comms is a Claude Code plugin (pure-stdlib MCP server) that lets independent, human-launched full Claude Code instances message each other and wake each other from idle — closing the gap that built-in `SendMessage` only covers parent→subagent. It provides identity/registration, DMs, groups, reactions, profiles, a local dashboard where the human is a first-class teammate, and opt-in "reincarnate" spawning. **Audit yardstick (human-confirmed): THIRD-PARTY ADOPTERS** — a brand-new Claude Code user/team installing from the marketplace with zero context. Definition of success: install the plugin, register via `teammate_register`, and reliably send/receive/wake across two or more full instances (cross-project, cross-OS) with no polling and no ports.

## Scope of this stage

Stage 2 audits the seams, not the boxes. Five Sonnet 5 subagents, split by boundary/data-flow so nothing falls between two agents' scopes: (1) the message lifecycle (send → store → watch → wake → read/suppress → ack); (2) identity & liveness as a cross-system contract; (3) dashboard ↔ store ↔ agents; (4) plugin ↔ Claude Code harness; (5) cross-project / cross-OS / cross-host. Stage 1 findings were given to each agent as "already captured" so this stage reports only what emerges from composition.

## Findings

### Identity & liveness seams

#### I1. Cross-host live name collision never resolves — perpetual record flap plus a de-facto shared mailbox  [severity: critical]  [type: bug]
- **What:** `register_identity`'s collision guard uses `is_channel_alive`, whose authoritative pid check is host-gated: across two hosts sharing a comms root (the documented cross-host mode) it falls back to heartbeat freshness, so both sides see the other as alive, log a stderr warning, and proceed. `write_agent_record` is a plain field-merge with no ownership check, and each side's 5s watcher heartbeat rewrites `pid`/`host` — the record's "owner" flips every ~5 seconds forever. Because registry and inbox are keyed purely by name, both physical instances drain the same `{name}_unread.json`: silent, non-deterministic message misdelivery.
- **Evidence:** `server.py:180-197` × `comms.py:860-883` × `channel.py:238-243`.
- **History (vibe-cognition):** Heartbeat fallback is deliberate (`c362e41c838f`); the guard's silence is Stage-1 S2; global uniqueness is constraint `0ff2595c61ef` with no enforcement. No node composes the three — the emergent behavior (flap + shared mailbox) was never analyzed. Oversight by composition.
- **Impact:** The confirmed success bar ("reliably send/receive/wake across 2+ instances, cross-OS") failing in its worst mode: no crash, no visible warning, just messages split unpredictably between two instances that both believe they are the addressee.
- **Root cause / Fable's read:** Identity has no owner. A name string is simultaneously the identity, the file path, and the collision key, and every writer merge-writes it. Most of this stage's identity findings (I2, I4, G2, B1) are the same root cause wearing different clothes.

#### I2. Reincarnate's collision guard structurally cannot see a live human  [severity: high]  [type: gap]
- **What:** `remove_teammate` special-cases `type == "human"` before its liveness check; `teammate_reincarnate`'s guard does not, and `register_human` deliberately never sets a `channel` key, so `is_channel_alive(human_record)` is always False. Reincarnating the operator's name spawns a real child whose auto-register merge-writes `type="full", channel=True` over the human's record — an agent can (even accidentally) hijack the operator's identity.
- **Evidence:** `tools.py:1509-1529` (no type check) vs `tools.py:1471-1472` (the carve-out it forgot); `comms.py:896-914`.
- **History:** Human-as-teammate deliberate (`a9594b942f2b`); no node discusses the reincarnate carve-out — oversight. Distinct from Stage-1 D1 (two dashboards).

#### I3. `teammate_delete` can report success while deleting nothing  [severity: high]  [type: bug]
- **What:** `remove_agent` hard-unlinks the registry and inbox files with no lock (every other mutator locks) and swallows `OSError` unconditionally — on Windows a sharing violation from a concurrent heartbeat write silently no-ops the deletion. The caller never checks and always returns "Removed teammate … (registry + inbox)."
- **Evidence:** `comms.py:767-778` (its own comment admits "already gone / locked"); `tools.py:1479-1488`.
- **History:** Undiscovered in the graph; adjacent to but distinct from `66785398c36d` (lost deletion event).

#### I4. Delete-then-heartbeat resurrects a permanent `type`-less ghost  [severity: medium-high]  [type: broken-assumption]
- **What:** `remove_teammate` uses heartbeat-only liveness (deliberate, `c362e41c838f`), so an actually-alive teammate whose last heartbeat write was swallowed (Stage-1 W7) reads offline and gets deleted; its watcher's next 5s tick recreates the record via the empty-dict merge path — without `type`, which only register ever sets. Every `type`-gated consumer then misreads it: `send_dm` tells the sender "no teammate named X is registered" for a live, registered agent; the doctor reports `alive: None`.
- **Evidence:** `tools.py:1473-1480` × `channel.py:238-243` × `comms.py:801-825`.
- **History:** Two separately-deliberate designs (heartbeat-only liveness, field-merge writes) composing into a gap neither anticipated — the signature Stage-2 failure shape.

### Cross-project / cross-OS / cross-host seams

#### G1. `teammate_list` filters by raw project string — the one entry point the WP-13 normalization fix skipped  [severity: critical]  [type: gap]
- **What:** `_handle_list` compares `record["project"]` strings verbatim, never through `validate_project_key` — while `_resolve_caller_project_key`/`_derive_project_roster` and the dashboard all normalize precisely to survive `\`→`/` and case differences. WP-13's own acceptance criterion #2 requires teammate_list grouping to survive a Windows-vs-Unix mismatch; the constraint node (`3a81e56eb341`) calls normalization "the load-bearing correctness fix … else cross-OS agents silently split the roster."
- **Evidence:** `tools.py:815-816, 830-832` (raw compare) vs `tools.py:1578-1608` and `dashboard.py:257-266` (normalized); `WP-13-project-profiles.md:170-173`.
- **History:** The raw filter predates WP-13 (`07214785e804`, WP-11b) and was never revisited when normalization landed six days later (`08051897f964` lists the dashboard + 4 project tools as touched; `_handle_list` isn't among them). Oversight that directly violates the project's own recorded acceptance criterion.
- **Impact:** Cross-OS teammates on the same normalized project silently vanish from each other's default `teammate_list` — the exact split WP-13 was built to prevent, reachable through the primary "who's on my team" surface, and the tools visibly disagree with each other (`list_projects` shows them together, `teammate_list` doesn't).

#### G2. Agent/group names have no case-folding — merge-vs-split depends on which host's filesystem serves the directory  [severity: high]  [type: broken-assumption]
- **What:** `validate_agent_name` accepts mixed case and every store keys off the literal string. On Windows (case-insensitive), "Bob" and "bob" silently collapse into one record; on a Linux host sharing the same root, they are two identities that never see each other. Same names, same root, opposite outcomes, zero detection. Project keys got exactly this fix in WP-13; names never did.
- **Evidence:** `comms.py:31, 74-84, 194-210, 813`; `tools.py:1524-1528` (exact-string collision lookup).
- **History:** No node addresses name case-folding; WP-13's silence on the other identity axes reads as scope-narrowing, not a considered rejection.

#### G3. The default project key is the parent directory's name — the same repo cloned at different paths splits the roster by default  [severity: high]  [type: broken-assumption]
- **What:** The auto-filled project label is literally `parent/name` from the cwd path. `~/Projects/teammate-comms` vs `~/dev/teammate-comms` on two machines → different keys, different rosters, invisible to each other. WP-8 introduced the two-component label to fix the opposite problem (two repos named `api` colliding) and never recorded the mirror-image tradeoff.
- **Evidence:** `server.py:131-134`; `tools.py:1611-1617`.
- **History:** `35f29d15eccb` (WP-8) records the collision fix; nothing records the split risk. WP-13's out-of-scope list names key *migration*, not default-key instability. Oversight.
- **Impact:** The yardstick's flagship scenario — same project, two machines — silently fails unless every teammate manually overrides `project` to agree, with no tooling nudge.

#### G4. No detection when peers resolve different comms roots  [severity: high]  [type: gap]
- **What:** Each process resolves its root independently (`$TEAMMATE_COMMS_DIR` / `$CLAUDE_CONFIG_DIR` / `~/.claude`); a sender happily `ensure_inbox`es the recipient into its *own* root and reports "queued, teammate offline" — textually identical to a genuinely offline same-root recipient. One agent with the env var set and a peer without believe they're teammates and can never exchange a message.
- **Evidence:** `comms.py:221-250`; `tools.py:580-617` (esp. 599, 612); `teammate_whoami(verbose=True)` inspects only the caller's root.
- **History:** No node on root-divergence detection; AUDIT F-2 covers the same UX for a different cause (typo). Oversight.

#### G5. Windows reserved device names (`con`, `aux`, `nul`, `com1-9`, `lpt1-9`) produce opaque failures  [severity: medium]  [type: bug]
- **What:** Name/key validation permits them; Windows refuses the file/lock paths. Depending on the path, the failure is a silently-swallowed lock break or a raw `OSError` surfaced as "failed unexpectedly."
- **Evidence:** `comms.py:31, 127, 657-683, 801-825`; `tools.py:1817-1823`.
- **History:** No graph history; AUDIT-v0.7.0 called validation "traversal-proof" with no reserved-name caveat — oversight.

#### G6. Cross-host storage transport semantics are asserted, never verified  [severity: medium]  [type: blindspot]
- **What:** The cross-host mode's atomicity foundations (`os.replace` same-volume atomicity, `mkdir` lock exclusivity) are unverified on the transports a team would actually use — SMB/NFS mounts or OneDrive/Dropbox-synced folders, the latter actively violating the model (out-of-band materialization, "conflicted copy" siblings no code path reads).
- **Evidence:** `comms.py:467-482, 612-647`; `DESIGN.md` §10.
- **History:** All cross-host graph work (`587f114aabab`, `51f70ca10a2a`) is about pid/host trust, never the transport. Compounds Stage-1 C1 (destructive read) and C4 (clock skew) — the three together make the documented cross-host mode unsafe as shipped.

### Message lifecycle seams

#### M1. (Corroboration) The unlocked destructive inbox read is the *only* non-conforming toucher of its file  [severity: high — already tasked]
- The lifecycle agent independently confirmed Stage-1 C1 and sharpened it: sender, group fan-out, ack, and tombstone all lock `{agent}_unread.json`; `_handle_inbox` alone uses the destructive reader with no lock — the outlier from the project's own named "concurrency-safety trio" pattern (`7bff080a52cc`). Covered by task `eb1c4c45d82c`.

#### M2. A "deferred" group-fan-out member gets zero wake signal — polling silently reintroduced  [severity: high]  [type: gap]
- **What:** `send_group` writes each member's inbox copy best-effort; on lock contention the member goes into `deferred` and *nothing else happens* — no retry, no persisted marker, and the watcher only ever polls `_unread.json`, never group transcripts. Only the **sender** is told ("will catch up via history"); the deferred member has no signal a post exists.
- **Evidence:** `tools.py:1085-1108` × `channel.py:192-325` (watcher never reads `groups/*/messages.json`).
- **History:** Group transcript-as-canonical is deliberate (`462773444a51`, `4152caab6808`) but the deferred-member wake gap is unaddressed — oversight.
- **Impact:** Directly breaks the "no polling" core promise in the group-chat feature, under load — exactly when a team demo has several agents talking at once.

#### M3. Watcher wake-counts aren't seeded from durable WP-15 suppression — fresh-session wakes over-report  [severity: medium]  [type: gap]
- **What:** `unseen_ids` subtracts in-memory `last_seen`, which is only populated by the session's first `teammate_inbox` call — never seeded from `{agent}_seen.json` at register. A new message arriving before that first read produces a wake counting stale, previously-shown messages ("3 new" → reads as "1 new, 2 already delivered").
- **Evidence:** `channel.py:285-296` vs `tools.py:668-701`; `server.py:67`.
- **History:** WP-15's None-sentinel separation is deliberate (`6dc06fd9c6b6`); its acceptance test T6 only covers behavior *after* the first read — the pre-read window is an uncovered edge of a deliberate design.

#### M4. `react()` resolves ids transcript-only; `delete` has a 3-tier fallback — same id space, different resilience  [severity: medium]  [type: broken-assumption]
- **What:** With `TEAMMATE_TRANSCRIPT=0` (a documented knob) or a rotated-away id, a reaction is recorded but the author is never woken (`target_from` unresolved), while delete on the same id still works via its fallback chain (transcript → group file → inbox).
- **Evidence:** `tools.py:1349-1372` vs `tools.py:1386-1422`.
- **History:** AUDIT E-5 tracked the doc side; the code-level asymmetry was never flagged — oversight.

#### M5. `ack("all")` cold-start drain pollutes group read-receipts  [severity: medium]  [type: broken-assumption]
- **What:** When `last_seen is None` (no inbox read this session), `ack("all")` drains the entire unread queue into `_read.json`; `group_read_positions` then infers read-receipts from `_read.json` membership and `teammate_group(action="reads")` presents "caught up" for messages never actually seen — the ack-hides-unseen pattern, via ack rather than suppression.
- **Evidence:** `tools.py:770-788` → `comms.py:917-933` → `tools.py:1265-1275`.
- **History:** Read-receipt semantics postdate the startup-drain design; no node reconciles them — oversight.

### Dashboard ↔ store ↔ agents seams

#### B1. Human presence has no staleness model — stuck "online" forever after any non-graceful exit  [severity: high]  [type: gap]
- **What:** Agent liveness deliberately distrusts flat flags (pid + heartbeat, `c362e41c838f`); human presence is exactly the flat flag that design rejected — set "online" at `start_dashboard`, flipped "away" only by the graceful shutdown path. Kill the terminal and every teammate sees the human online indefinitely. Under the default-name collision (Stage-1 D1) it's worse: two dashboards share one presence field and cross-clobber each other ("away" from one operator's clean exit marks the *other*, still-live operator offline).
- **Evidence:** `comms.py:896-914` × `dashboard.py:255-256, 496-514` × `tools.py:837-841, 1565`.
- **History:** No node on presence staleness — genuine oversight, and internally inconsistent with the project's own liveness philosophy.

#### B2. (Corroboration) Stale-token dead tab — folded into existing task `7cb9fd8b88e3` with the seam detail: the token's lifetime is the server process's, the tab's is independent, and `history.replaceState` strips the URL token so recovery without re-invoking `teammate_dashboard` is impossible.

#### B3. The dashboard's global per-id dedup assumes store-wide id uniqueness that nothing guarantees  [severity: medium]  [type: broken-assumption]
- **What:** `pushRecord` drops any record whose microsecond-timestamp id is already in one global `seen` Set across all conversations and writers. Two different messages stamping the same microsecond (two writers, two machines) → one silently never renders, though both are durably stored. The `_window` docstring's collision reasoning covers one writer's own window, not this cross-writer Set.
- **Evidence:** `index.html:189-198`; `comms.py:69-71, 966-981`.
- **History:** WP-7 P3 (`e13c14b38dde`) made id *order* irrelevant to the cursor; two-records-one-id was never addressed. Oversight; folds naturally into the id-scheme work (task `e3288a850c64`).

#### B4. `TEAMMATE_TRANSCRIPT=0` blanks the dashboard with no in-app signal  [severity: low]  [type: unclear-instruction]
- Deliberate, documented knob (`README.md:171-177`) — but in-app the result is indistinguishable from a bug ("No messages yet" everywhere). Covered as an addition to the dashboard-failure-surfacing task (`7cb9fd8b88e3`).

### Plugin ↔ Claude Code harness seams

#### H1. Mid-session plugin update splits the "single source of truth"  [severity: high]  [type: broken-assumption]
- **What:** The server's `INSTRUCTIONS`/`TOOL_DEFINITIONS` are frozen at process spawn; the compact hook re-execs `python -m teammate_comms.instructions` fresh from disk. A plugin update landing mid-session means compaction injects the *new* version's instructions while the agent still talks to the *old* running server — the exact provenance drift the single-source design exists to prevent, reintroduced through the file-vs-process split.
- **Evidence:** `server.py:25-26` × `hooks/reinject-instructions.sh:27-29`; DESIGN §3.
- **History:** The single-source design is deliberate but only reasons about same-version drift — oversight.

#### H2. Managed-settings allowlist detection is a schema guess verified against exactly one Claude Code build  [severity: high]  [type: broken-assumption]
- **What:** `channel_allowlisted()` reconstructs an undocumented schema, verified once on v2.1.161/Windows (`6e017ca575eb`), with no version guard and no post-spawn verification. A schema change silently yields either a false positive (child launched trusted but isn't — dead silent teammate under a success message) or a false negative (child gets the dangerous flag and an unanswerable headless trust prompt).
- **Evidence:** `spawn.py:56-82`; `tools.py:1547-1555`.
- **History:** Deliberate design on a necessarily time-bound observation, shipped with no expiry (`9a054b32abae`) — rots silently.

#### H3. First-install ordering (hook builds venv before MCP spawn) is not guaranteed — the documented fix is "restart," prose-only  [severity: high]  [type: gap]
- **What:** `plugin.json` spawns with `uv run --no-sync` (hard-fails without a venv); the SessionStart hook builds the venv; DESIGN concedes first install needs a restart. Nothing in-product tells the user that — the very first session simply has no `teammate_*` tools.
- **Evidence:** `hooks/hooks.json:4-13`; `plugin.json:9-14`; DESIGN §3.
- **History:** The hook-ordering contract was never verified (the team already treats matcher syntax as unverified, `session-start.sh:11-15`) — assumed, not hardened. Compounds Stage-1 P1/P3 into the full first-contact theme.

#### H4. `TEAMMATE_LAUNCH_ARGS` is a full allowlist bypass that inherits down the reincarnation chain forever  [severity: medium]  [type: broken-assumption]
- **What:** If set, it's used verbatim (skipping `channel_allowlisted()` entirely) and is deliberately NOT stripped from child env — so one stray `setx`/export silently overrides the trust detection for every descendant spawn, generations removed, with no visibility in `teammate_whoami`. The gate var got a strip after incident `c1fa517c047d`; the lesson was only half-applied.
- **Evidence:** `spawn.py:94-97, 107-138` (comment at 131-134).

#### H5. The entire standing-instructions contract rests on an unverified assumption that `initialize.instructions` reaches the model  [severity: medium]  [type: blindspot]
- **What:** The team's own framing treats only post-compaction survival as the open question; whether Claude Code surfaces MCP initialize instructions into model context *at all* was never verified — and everything (register-first protocol, inbox drain, status discipline) is built on it.
- **Evidence:** `server.py:227-237`; `instructions.py:4-6`; `reinject-instructions.sh:4-6`.
- **History:** No node — a foundational unstated assumption.

#### H6. (Deliberate, noted) The wake push has no acknowledgment protocol — the re-nudge is blind, time-based, and fixed. The seam-level residual (no indicator anywhere for "pending un-acked pushes after cap exhaustion") is folded into task `a8852f578a7d`.

#### H7/H8. Low: no `notifications/tools/list_changed` mechanism if the client ever caches tool schemas across a same-session server respawn; `$CLAUDE_PLUGIN_ROOT` is independently re-expanded by three consumers at different times with no consistency check — the shared root cause under H1/H2.

## Summary & Recommendations

Stage 1's themes explained defects inside boxes; Stage 2 shows the seams fail by **composition of individually-deliberate designs**. Five cross-cutting reads:

1. **Identity has no owner.** A mutable name string is the identity, the file path, and the collision key; every writer field-merges; liveness is advisory. I1 (cross-host flap + shared mailbox), I2 (human hijack), I4 (ghost resurrection), G2 (case merge/split), B1 (presence clobber) are one architectural gap: the agent record has no instance-ownership (e.g. an instance id + compare-and-swap or epoch) and no reconciliation protocol when two claimants exist. This is the highest-leverage structural fix in the audit; patching the five symptoms individually will leave the class alive.

2. **The cross-OS/cross-host promise fails at the seams even though every piece was deliberately built.** G1 (normalization skipped the primary surface — violating WP-13's own acceptance criterion), G3 (unstable default keys), G4 (undetected root divergence), G6+C4+C1 (unverified transport + clock assumptions). Recommendation: treat "two machines, one team" as a first-class tested scenario, or explicitly document it as unsupported; the current middle ground (documented but unverified) is the worst position for adopters.

3. **Liveness means something different to every consumer, and several consumers act destructively on a lying signal.** Heartbeat-freshness stands in for pid truth cross-host (I1), never-true for humans (I2, B1), and a single missed write cascades into deletion + ghost resurrection (I3/I4). Recommendation: one documented liveness contract (who may conclude "dead," on what evidence, for which action class), with destructive actions requiring the strongest evidence.

4. **Harness contracts are assumed from single observations and have no guards.** H2 (one build), H3 (ordering), H5 (does instructions injection work at all?), H1 (update timing), H7/H8. Recommendation: version-stamp and verify — record which Claude Code build each reverse-engineered contract was validated against, check at runtime where cheap, and fail loud on mismatch.

5. **Several paths silently reintroduce polling — the one thing the plugin promises to eliminate.** M2 (deferred group member never woken), M3 (miscounted wakes erode trust in the signal), H6/W3 (cap exhaustion with no indicator), M4 (lost reaction wakes). Recommendation: an invariant worth writing down and testing — *every durable message must eventually produce either a wake or a visible pending indicator*.

## Potential tasks (checklist)

- [ ] Identity ownership & reconciliation: instance-id/epoch in agent records, compare-and-swap or last-writer-wins-with-detection, collision resolution protocol (root fix for I1; unblocks I2/I4/B1) — priority: critical
- [ ] Normalize project comparison in `teammate_list` via `validate_project_key` (G1) — priority: critical
- [ ] Reincarnate guard: human/type carve-out like `remove_teammate`'s (I2) — priority: high
- [ ] Locked, verified deletion in `remove_agent`; prevent heartbeat ghost-resurrection (recreate path must not fabricate a `type`-less record) (I3, I4) — priority: high
- [ ] Human presence staleness: TTL/heartbeat for presence; stop cross-dashboard clobber (B1) — priority: high
- [ ] Group fan-out deferred members: retry and/or persisted behind-marker the watcher can wake on (M2) — priority: high
- [ ] Stabilize default project identity: derive from git remote when available, or detect/warn on cross-machine key divergence (G3) — priority: high
- [ ] Detect comms-root divergence between peers: root fingerprint in records + doctor/send diagnostics (G4) — priority: high
- [ ] Agent/group name case-folding policy (fold at compare, or reject case-variant collisions at register) (G2) — priority: high
- [ ] Harness contract guards: version-stamp managed-settings detection with runtime mismatch warning; verify `initialize.instructions` reaches the model; in-product first-install restart signal (H2, H5, H3) — priority: high
- [ ] `TEAMMATE_LAUNCH_ARGS`: strip from child env or surface its inheritance in whoami/reincarnate output; document the bypass (H4) — priority: normal
- [ ] Mid-session update drift: version-stamp reinjected instructions and detect server-vs-disk version mismatch (H1) — priority: normal
- [ ] Seed watcher `last_seen` from durable `{agent}_seen.json` at register (M3) — priority: normal
- [ ] `react()`: use the same 3-tier id-resolution fallback as `resolve_message` (M4) — priority: normal
- [ ] Distinguish ack-without-read from read in group read-receipts (M5) — priority: normal
- [ ] Message-id uniqueness across writers (per-writer disambiguator) — also fixes dashboard dedup drop (B3; extends task `e3288a850c64`) — priority: normal
- [ ] Reject Windows reserved device names in name/key validation (G5) — priority: normal
- [ ] Document supported/unsupported cross-host storage transports (SMB/NFS/sync-folder caveats) (G6) — priority: normal
- [ ] MCP housekeeping: `tools/list_changed` consideration; single-point `$CLAUDE_PLUGIN_ROOT` consistency check (H7, H8) — priority: low
