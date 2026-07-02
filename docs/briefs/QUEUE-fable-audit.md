# Fable-audit (260701) WP queue — teammate-comms

> Manager: Silvie (plans/briefs/gates — no code). Implementer: Svetlana (all code + commits).
> Branch: `fix/fable-audit-260701` — ALL WPs land here (user-directed single branch).
> Epic: cognition task `291c795d20af` (55 open child tasks). Source docs:
> `docs/260701-fable-audit-{systems,interconnectivity,consumer}.md` + `AUDIT-v0.7.0.md`
> ("Known-intentional" table — binding for every WP).

## Working agreement (autonomous overnight mode)

- Svetlana works the queue **top-down without waiting for go-aheads between WPs**: post your
  WP plan to Silvie as an FYI and proceed immediately; Silvie reviews async and redirects.
- The **hard gate is the diff**: when a WP is code-complete, commit (fix + proof same commit),
  post the SHA + test output to Silvie, and START THE NEXT WP immediately. Fix-forward commits
  if the gate finds issues (single shared branch — no per-WP branches this epic).
- Claim each WP's cognition tasks when you start (`cognition_update_task`: owner=Svetlana,
  status=in_progress). Silvie flips them to done only after the gate passes.
- Anything ambiguous or touching a Known-intentional behavior: ask Silvie, don't guess. If a
  question needs COLTON (asleep — unavailable all night), Silvie stores the question on the
  task and the task is SKIPPED, never guessed.
- Journal protocol (shared checkout): NEVER commit `.cognition/journal.jsonl` on the branch;
  Silvie flushes it to main at checkpoints. No destructive git ops without a flush ping.
- Version bump to 0.12.0 + CHANGELOG happens ONCE at the end (WP-32), not per-WP.

## Queue (briefs land in docs/briefs/ as they're written; order = execution order)

| WP | Tasks (cognition ids) | Theme | Brief |
|----|----------------------|-------|-------|
| WP-16 | 8b193651d828, 66eb9c8afa55 | S1 envelope guard; S3 protocolVersion; S5 notification discipline | WP-16-protocol-core.md |
| WP-17 | 281def4ace27, eb1c4c45d82c, 8585f71607e0 | G1 list normalization; C1 inbox read safety; C2 bounded reactions | WP-17-hotpath-storage.md |
| WP-18 | a2fa123fa4dc, 3fe8923a6042 | Q1/Q2/Q3 test integrity; P4 macOS CI | WP-18-test-integrity-ci.md |
| WP-19 | 7b31a55cfae3, 986cfcf479ae | Identity ownership (instance_id/epoch, flap kill) + collisions surfaced in-conversation | (next) |
| WP-20 | 374bd3a6d4ee, 1022f45a83f4 | Verified deletion + ghost prevention; reincarnate human carve-out | |
| WP-21 | f35d5c1e4cb3, 55e048ad55e9, 0c05ca523891 | Human presence staleness; name case-folding; Windows reserved names | |
| WP-22 | 4ca460444b4d, c2ab8b30771d, 0929bb4149d8 | Watcher: atomic snapshot, heartbeat-failure handling, seen-seed, deferred fan-out recovery | |
| WP-23 | 5d8ffa03db4c, 0fd06ee573ed, 9091253cb1c5 | spawn.py: posix split, child reaping, marketplace un-hardcode, LAUNCH_ARGS surfacing | |
| WP-24 | 2e9c79d023e2, e8be977680da | Reincarnate gate durable-set detection; auto-register failure surfacing | |
| WP-25 | 60f989d05f0a, 945b1ad3b274, cfe9b9d3e54d | unread cap (move-to-read, tagged); ack-without-read receipts; group NDJSON | |
| WP-26 | 399bc7d3ce75, e3288a850c64, 5d7b245c71f6 | Message-id uniqueness; cross-host id docs; react 3-tier resolution + miss error | |
| WP-27 | 21ce53019f93 | Reactions compaction (state baseline) + transcript rotation | |
| WP-28 | b03c67387760, efbad7e5b2c1, a908cfa546cc | Avatars: self-only, lifecycle GC, installable extra | |
| WP-29 | 7cb9fd8b88e3, c125b9d5d1b2, b111b11fd93a | Dashboard failure surfacing; root-divergence diagnostics; project identity from git remote | |
| WP-30 | e4c5fd263296, d0e5caeb18fd, 54982be4879b, 2f7eff973722, 27fbaf6b8c96 | Harness contracts: first-install signal, reinject self-filter, version stamps, housekeeping | |
| WP-31 | e8fa4a45a4fa, 21477b1b6530, 0461f5dfab9d | Tool-surface polish; shared renderer; group open-read description | WP-31-tool-surface.md |
| WP-33 | d7791d327351 | Test gaps: avatars error paths (Q4); true multi-process lock contention (Q5) — runs AFTER WP-28 | WP-33-test-gaps.md |
| WP-32 | 58262dd8a212, 2ec3a014b36f, d9af4a8af567, f194d7cfc1fa, 14f6b52fe059, 1d46603d1016, db4322e285a7, a96186336862, a8852f578a7d, 4b56f68ceab3, a3fa50c8c6c8 | Docs mega-pass + v0.12.0 bump + CHANGELOG + local tags — LAST | WP-32-docs-release.md |

## Standing Known-intentional list (binding, from AUDIT-v0.7.0.md — do not "fix")

- Transcript tee best-effort/droppable ("observability, not delivery"); inbox is authoritative.
- Global comms root; cross-project by default. Pure-stdlib zero-dep; single-threaded loop.
- Heartbeat-only liveness in teammate_list (`pid_check=False`).
- Identity at runtime via teammate_register; TEAMMATE_AGENT is a convenience.
- Open group membership, auto-join-on-send; open read (documenting it IS WP-31's job).
- ack("all") startup-drain; WP-15 last_seen None-sentinel (decision `6dc06f...`).
- Reacting to a deleted message still wakes its author.
- Dashboard mutation-reflection requires TEAMMATE_TRANSCRIPT=1.
