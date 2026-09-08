# Gate TC-C1 — human field verification: teammate-comms v0.16.0 on Codex CLI

Run by: Colton, on the Windows machine with codex-cli ≥ 0.149 (verified design on 0.153.4)
and Claude Code with the current teammate-comms plugin. Mirrors vibe-cognition's
`docs/codex-test-brief.md` phases 0–4 and adds Phase 5 (wake). Stop at the first FAIL and
report; do not work around.

Commands marked (PS) run in PowerShell outside any Codex session; inside a Codex session
call `codex.cmd` (the `.ps1` shim is blocked by execution policy) and expect process
enumeration to be denied by the sandbox.

## Phase 0 — pre-install cleanliness

- (PS) `codex mcp list` shows no `teammate-comms` entry.
- (PS) `codex plugin list` shows no `teammate-comms`; `codex plugin marketplace list` shows no
  `teammate-comms-dev`.
- No `~/.codex/plugins/data/teammate-comms-*` directory; no `teammate-comms` lines under
  `[hooks.state]` in `~/.codex/config.toml`.
- Claude side: `teammate_list(all=true)` from a Claude Code session shows no stale `Codex*`
  records (delete offline leftovers with `teammate_delete`).

## Phase 1 — install (dev marketplace, current branch)

```
codex plugin marketplace add Haagndaazer/teammate-comms
codex plugin add teammate-comms@teammate-comms-dev
```
Expected: both succeed; `~/.codex/plugins/cache/teammate-comms-dev/teammate-comms/<ver>/`
exists with `.codex-plugin/plugin.json` version 0.16.0.

## Phase 2 — first session (registration + hook trust)

Start `codex` inside `E:\E Drive Projects\teammate-comms`. Trust the hook when prompted.
First turn: `What teammate-comms context did you receive at session start?`
Expected on THIS session: possibly nothing (a newly trusted hook skips the session that
trusts it) — that is a PASS. Exit Codex, start it again in the same directory, ask the same
question. Expected: the agent quotes `teammate-comms: harness=codex thread_id=<uuid>
project_dir=E:\E Drive Projects\teammate-comms` plus the "registered its MCP server …
restart Codex once" note. (PS) `codex mcp list` now shows `teammate-comms` with
`TEAMMATE_HARNESS=codex` and `UV_PROJECT_ENVIRONMENT=…plugins/data/…/.venv`; the venv dir
exists.

## Phase 3 — second session (the real test)

Restart Codex in the same directory.
1. `/mcp` shows `teammate-comms` connected.
2. `Register with teammate-comms as "Codex1" using the harness_session and project_dir from
   your session-start context.` Expected: register result WITHOUT the "Re-register with
   harness_session" sentence; `teammate_whoami` shows `harness: codex` and the thread id.
3. From a Claude Code session (registered, e.g. as Silvie): `teammate_list(all=true)` shows
   `Codex1: type=full, channel=live` with `harness: codex` and `project: Haagndaazer/teammate-comms`
   (or `teammate-comms/…` path form — record which).
4. `$teammate-comms` in Codex loads the skill; it shows the Harness notes section.

## Phase 4 — wake (the reason for this release)

With Codex1 idle (prompt showing, no turn running):
1. Claude side: `teammate_send(to: "Codex1", message: "wake test 1")`. Note the time.
   Expected: within ~15 s the Codex window starts a turn on its own showing
   `📬 1 new message(s) from <sender>.` and the agent calls `teammate_inbox`. Record the
   delay. No manual `codex queue` anywhere.
2. Two DMs 1 s apart (`wake test 2a`, `wake test 2b`). Expected: one or two queued turns;
   both messages read; server stderr (see below) shows a `dropped-busy` line if the second
   landed while the first `codex queue` was running.
3. Busy deferral: tell Codex `Count to 30 slowly, one number per line`, and while it counts
   send `wake test 3`. Expected: the wake turn starts after the count finishes.
4. Group: from Claude create `#gate` with members Silvie + Codex1, post `group test`.
   Expected: Codex wakes with `Reply to to:'#gate' not the sender.`; Codex replies to the
   group; Claude side sees the reply in `teammate_group(action: "history", group: "#gate")`.
5. Reaction: Claude reacts 👍 to a Codex1 message. Expected: Codex wakes with
   `💬 <name> 👍 reacted to your message(s).`
6. Codex → Claude: `Send Silvie a message saying "hello from Codex"`. Expected: the Claude
   session wakes via its channel and reads it.
7. Server stderr: locate the teammate-comms server log Codex keeps (record the path) and
   confirm lines `wake-emit kind=fresh harness=codex` and `harness=codex rc=0`.

## Phase 5 — reincarnate + concurrency

1. From Claude Code with `TEAMMATE_REINCARNATE_ENABLED=1` in the server env:
   `teammate_reincarnate(agent: "Codex2", project_dir: "E:\E Drive Projects\vibe-cognition", harness: "codex")`.
   Expected: a new terminal opens running `codex`, the agent registers as Codex2 with a
   thread id (its prompt tells it to), `teammate_list(all=true)` shows `Codex2 … harness: codex`
   within ~30 s. If Codex blocks on an approval prompt, record which flag was missing.
2. Two Codex sessions (Codex1, Codex2) in two projects: (PS) `Get-CimInstance Win32_Process |
   Where-Object CommandLine -like '*teammate_comms.server*'` shows two server processes.
   Send each a DM; each wakes independently.

## Phase 5b — comms-root migration (v0.16.0 moves `~/.claude/TeammateComms` → `~/.teammate-comms`)

Precondition: this machine still has `~/.claude/TeammateComms` with live Claude Code
agents on the OLD plugin version (the roster above).
1. Start a v0.16.0 server (Codex session from Phase 3, or a Claude Code session loading
   the branch) while at least one old-version agent is live. Expected: `teammate_whoami`
   reports `comms_root: …\.claude` with source "legacy root (move … deferred)"; the server
   log shows `legacy comms root migration: deferred (<names>)`; old and new agents still
   see each other in `teammate_list`.
2. Stop every teammate (all Claude Code and Codex sessions), wait 60 s, start one
   v0.16.0 session. Expected: log shows `migrated (…\.claude\TeammateComms -> …\.teammate-comms\TeammateComms)`;
   `~/.claude/TeammateComms/` now contains only `MIGRATED.json`; `teammate_whoami` reports
   `comms_root: …\.teammate-comms`; the roster, groups, and read history are intact.
3. Start a second session (other harness). Expected: same root; DM round trip works.
4. `teammate_whoami(verbose=true)` doctor shows `legacy_root: migrated`.

## Phase 6 — uninstall

```
codex mcp remove teammate-comms
codex plugin remove teammate-comms@teammate-comms-dev
codex plugin marketplace remove teammate-comms-dev
rm -r ~/.codex/plugins/data/teammate-comms-teammate-comms-dev
```
Expected: Phase 0 checks pass again. `teammate_delete(teammate: "Codex1")` and `Codex2`
from Claude once they are offline.

## Report

For each numbered step: PASS / FAIL + the observed text. Attach: wake delays (4.1–4.3),
the server log path (4.7), the exact register result text (3.2), and any prompt Codex
showed that needed a human click. FAILs go into the graph as `fail` nodes with the step id.
