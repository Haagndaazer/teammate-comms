# teammate-comms — Design

> A Claude Code **plugin** that bundles an **MCP server** providing agent-to-agent
> messaging plus **channel-based idle wake** for full Claude Code instances.
> Packaged like [`vibe-cognition`](https://github.com/Haagndaazer/vibe-cognition):
> a `uv`-managed Python package shipped as a marketplace plugin.

This document is the build blueprint. No plugin code exists yet; this spec drives
the implementation step that follows.

---

## 1. Purpose & lineage

Two **independent full Claude Code instances** (each started by a human in its own
terminal) cannot wake each other: the harness `SendMessage` nudge only works from a
parent agent to a subagent it spawned. teammate-comms closes that gap with a Claude
Code **channel** — an MCP server that pushes an event into a *running* session, even
while it sits idle waiting for its human.

This generalizes a prototype validated inside the `TestSVN` repo
(`.claude/skills/teammate-comms/scripts/channel_server.py` + `common.py`), which
proved the core mechanic end-to-end: a one-way channel server watches its own
agent's inbox file and emits `notifications/claude/channel` when new messages
arrive, so a peer's `send` *is* the nudge — no ports, no cross-instance addressing.

Once shipped, this plugin **supersedes** the in-repo TestSVN skill; that repo drops
its local copy and consumes the plugin instead (a later migration).

### Wake regimes (pick by process topology, not by "team")
- **Full instance** (its own `claude` process): woken by the **channel** here.
- **Spawned subagent** (a lead invoked it via the Agent/Task tool): woken by the
  parent's `SendMessage`. Channels do not apply — a spawned subagent has no
  independent session for a channel to inject into.

---

## 2. Repo layout (mirrors vibe-cognition)

```
teammate-comms/
├── .claude-plugin/
│   ├── plugin.json
│   └── marketplace.json
├── pyproject.toml            # hatchling; deps: mcp>=1.27,<2
├── uv.lock                   # COMMITTED — reproducible installs (see §5)
├── src/teammate_comms/
│   ├── __init__.py
│   ├── server.py             # low-level mcp Server: tools + channel (see §6)
│   ├── comms.py              # storage/registry/liveness (ported common.py; §8)
│   ├── channel.py            # background inbox watcher + push (§7)
│   ├── tools.py              # MCP tool handlers (§9)
│   └── cli.py                # optional CLI parity (teammate-send, etc.)
├── hooks/
│   ├── hooks.json
│   ├── session-start.ps1
│   └── session-start.sh
├── skills/teammate-comms/SKILL.md
├── README.md
└── .gitignore
```

---

## 3. Dependency install & MCP wiring

**Lesson from the vibe-cognition template:** do **not** let `uv` sync dependencies
during the MCP spawn — that would block the stdio handshake while a fresh,
lockfile-less resolve runs, and the `mcp` SDK pulls a non-trivial tree. Instead:

- **Ship a committed `uv.lock`** for reproducible, fast installs.
- A **SessionStart hook** (`hooks/session-start.ps1` + `.sh`, registered in
  `hooks/hooks.json` via `${CLAUDE_PLUGIN_ROOT}`) runs `uv sync --no-dev` against a
  stamp hash so dependencies exist *before* the server is spawned. Mirror the
  structure of vibe-cognition's `hooks/session-start.sh`.
- The MCP server is launched with **`uv run --no-sync`** (deps already present).
- **First-session UX:** on a fresh install the hook syncs, and the server connects
  after a **restart** — identical to vibe-cognition. Document this in the README.

`mcpServers` is declared **inline in `plugin.json`** with `${CLAUDE_PLUGIN_ROOT}`,
so the plugin is self-contained and does not write into the project's `.mcp.json`.

---

## 4. `.claude-plugin/plugin.json`

```json
{
  "name": "teammate-comms",
  "version": "0.1.0",
  "description": "Agent-to-agent messaging with channel-based idle wake for full Claude Code instances.",
  "author": { "name": "ColtonDyck" },
  "license": "MIT",
  "repository": "https://github.com/Haagndaazer/teammate-comms",
  "keywords": ["mcp", "channel", "agent", "messaging"],
  "mcpServers": {
    "teammate-comms": {
      "command": "uv",
      "args": ["run", "--no-sync", "--directory", "${CLAUDE_PLUGIN_ROOT}", "python", "-m", "teammate_comms.server"],
      "env": {
        "TEAMMATE_AGENT": "${TEAMMATE_AGENT:-}",
        "TEAMMATE_TEAM": "${TEAMMATE_TEAM:-}",
        "TEAMMATE_COMMS_DIR": "${TEAMMATE_COMMS_DIR:-}"
      }
    }
  },
  "channels": [{ "server": "teammate-comms" }]
}
```

`version` MUST stay in sync with `pyproject.toml`. The `channels` array marks the
`teammate-comms` MCP server as a channel so Claude Code registers the notification
listener.

---

## 4b. `marketplace.json` and the `coltondyck` name collision

⚠️ **Hazard:** a marketplace named `coltondyck` is **already registered on this
machine** (it resolves to the vibe-cognition repo). Marketplaces are keyed by name,
so adding a *second* repo that also declares `name: "coltondyck"` will collide on
`/plugin marketplace add` and can clobber the working vibe-cognition registration.

**Handling (do not defer):**
- Keep `name: "coltondyck"` in the manifest for brand consistency, but **do not add
  this repo as a second `coltondyck` marketplace.**
- **Local dev:** use `claude --plugin-dir C:\Users\colto\Documents\Projects\teammate-comms`
  (bypasses marketplace-name registration entirely), or temporarily add it under a
  distinct local name.
- **Publishing:** the durable fix is hosting both plugins from **one** marketplace
  repo. Until then, `coltondyck` cannot be added from two repos simultaneously.

```json
{
  "name": "coltondyck",
  "owner": { "name": "ColtonDyck" },
  "plugins": [
    {
      "name": "teammate-comms",
      "source": {
        "source": "url",
        "url": "https://github.com/Haagndaazer/teammate-comms.git",
        "sha": "<set-at-release>"
      },
      "description": "Agent-to-agent messaging + channel idle-wake."
    }
  ]
}
```

(Published path uses the `url`+`sha` source form like vibe-cognition; local dev
prefers `--plugin-dir`.)

---

## 5. `pyproject.toml`

- Build backend: `hatchling`.
- `name = "teammate-comms"`, `version = "0.1.0"` (synced with `plugin.json`).
- `requires-python = ">=3.11"`.
- `dependencies = ["mcp>=1.27,<2"]`.
- `[tool.hatch.build.targets.wheel] packages = ["src/teammate_comms"]`.
- `[project.scripts]` for the optional CLI (`teammate-send`, `teammate-inbox`,
  `teammate-ack`) reusing the same `comms` module.
- Commit `uv.lock`.

---

## 6. `server.py` — low-level mcp Server (tools + channel) — RISKIEST PIECE

One server is **both** a tool server and a channel:
`capabilities.experimental['claude/channel'] = {}` plus normal MCP tools.

**Verified against `mcp` v1.27.1:**
- The experimental capability is first-class:
  `server.create_initialization_options(experimental_capabilities={"claude/channel": {}})`.
- The custom `notifications/claude/channel` cannot go through the typed
  `session.send_notification(...)` (its `ServerNotification` union is a closed set of
  `Literal` methods). Send it raw instead — build a
  `JSONRPCNotification(method="notifications/claude/channel", params={"content":…, "meta":…})`,
  wrap `SessionMessage(JSONRPCMessage(...))`, and `await session._write_stream.send(...)`.
  This rides the SDK's own session stream — **not** raw stdout — so there is no
  stdio-ownership/interleaving hazard.

**The hard part — getting a session for an *unsolicited* push.** A channel must
push while the agent is idle, i.e. with **no in-flight request**. The low-level
`Server` lifespan does **not** receive a `ServerSession`, and
`request_context.session` is only populated *during* a request handler — empty
exactly when the channel needs it. Required approach:
- **Own/capture the `ServerSession`** as the server connection comes up (rather
  than the bare `await server.run(...)` convenience), stash the reference, and run
  the inbox watcher as an **anyio task in the same event loop** — *not* a raw
  thread, because `_write_stream.send` must be driven from the loop.
- **Gate** pushes on `notifications/initialized` (mirror the prototype's
  `_initialized` event); **seed the unread baseline** at that moment so pre-existing
  messages don't trigger a nudge (the agent drains those once on startup).

**Documented fallback:** if owning the SDK session proves too brittle against the
research-preview surface, retain the proven **pure-stdlib** channel/stdio core
(newline-delimited JSON-RPC, background thread, raw stdout writes) and expose tools
via hand-rolled `tools/list` / `tools/call`. The prototype already implements this
core; it is a viable exit if the SDK path fights us.

**Windows stdio:** the port inherits the prototype's constraints — verify the SDK
stdio transport emits **BOM-free UTF-8 terminated by `\n`** (no CRLF, no cp1252).
Read input tolerant of a leading BOM.

---

## 7. `channel.py` — wake mechanics (ported, proven logic)

An anyio task that, once the session is initialized:
- Polls `<self>_unread.json` every ~0.5s via a **non-destructive read** (never
  rewrite the file on a partial/corrupt read — that would destroy a message
  mid-delivery).
- Seeds `baseline` to the current unread count at init; emits only when the count
  rises above `baseline`; resets `baseline` downward on ack.
- Emits `notifications/claude/channel` with `meta = {count, agent}` and **content
  that references the MCP tools** (not script paths), e.g.:
  *"You have N new teammate message(s). Call the `teammate_inbox` tool to read them,
  then `teammate_ack`. You are a full instance — the channel wakes you; no polling
  loop needed."*
- Updates the agent's registry heartbeat each cycle.

Dropped pushes (session closed) never lose a message — the inbox JSON is the source
of truth and is drained on next startup.

---

## 8. `comms.py` — relocated `common.py`; storage resolution

Port the validated helpers, parameterized by the resolved comms root:
`get_inboxes_dir`, `get_agents_dir`, `validate_agent_name` (+ `AGENT_NAME_PATTERN`),
`read_json_readonly` (non-destructive), `write_json_atomic` (`os.replace`),
`write_agent_record` / `read_agent_record` (field-level merge under a per-record
non-fatal lock), `is_channel_alive` (same-host pid check, else heartbeat freshness),
and a single pinned timestamp format.

**Comms-root resolution — critical.** A plugin-spawned server's **cwd is the plugin
cache directory** (which is itself a git repo), so the prototype's
`git rev-parse --git-common-dir`-from-cwd would scatter inboxes into the plugin
cache. Resolve in this order and stop at the first hit:
1. `$TEAMMATE_COMMS_DIR` (explicit override — enables cross-project/global comms).
2. `$CLAUDE_PROJECT_DIR` (Claude Code sets this in a spawned server's environment;
   authoritative project root). Comms live at `<root>/TeammateComms/[<team>/]…`.
3. Otherwise: log to stderr and exit (do **not** fall back to cwd/git).

---

## 9. MCP tools (the "wrap everything up" surface)

Agents call tools instead of shelling out to scripts. `from` is implicit (the
server's own resolved identity). Validate `to` with `validate_agent_name`.

| Tool | Args | Behavior |
|------|------|----------|
| `teammate_send` | `to: str`, `message: str`, `priority?: "normal"\|"urgent"` | Append a message to `to`'s inbox (atomic write). Report whether `to`'s channel is live (auto-nudge) or offline (queued). |
| `teammate_inbox` | `count_only?: bool` | Read this agent's unread messages (or just the count). |
| `teammate_ack` | `id: str` | Move a message (`id` or `"all"`) from unread → read. |
| `teammate_list` | — | List registered agents with type + liveness. |
| `teammate_whoami` | — | Report resolved identity, team, and comms dir (diagnostics). |

Every tool's error text wraps the underlying cause with a one-line action sentence.

---

## 10. Identity delivery (`$TEAMMATE_AGENT`)

Per-instance identity reaches the server via `${TEAMMATE_AGENT}` expansion in the
plugin's mcp `env` — the same mechanism verified working in TestSVN's project
`.mcp.json` (set `$env:TEAMMATE_AGENT` in the shell **before** launching `claude`;
the spawned server inherits it).

**Failure mode to document:** if `TEAMMATE_AGENT` is unset/empty the server exits
with "no agent" and the channel silently never connects. The only signals are
`/mcp` showing the server not-connected and the stderr trace in
`~/.claude/debug/<session-id>.txt` (the server logs its resolved identity there at
startup, including a collision warning if another live server already owns the same
agent name on this host).

---

## 11. `skills/teammate-comms/SKILL.md`

Documents the tools (§9), the two wake regimes (§1), the reliability contract
(inbox is source of truth; `send` warns on an offline peer), and the launch line:

```powershell
$env:TEAMMATE_AGENT = 'Grant'
claude --dangerously-load-development-channels plugin:teammate-comms@coltondyck
# (or, for local dev:)
claude --plugin-dir C:\Users\colto\Documents\Projects\teammate-comms --dangerously-load-development-channels plugin:teammate-comms@coltondyck
```

Prerequisites: Claude Code **v2.1.80+**, `uv` installed, channels enabled
(individual Pro/Max: on by default). Custom channels stay on
`--dangerously-load-development-channels` (not on Anthropic's allowlist).

---

## 12. Open follow-ups (not in the scaffolding step)

1. Scaffold the package + manifests + hooks.
2. Implement & test the server; adapt the prototype's `channel_handshake_test.py`.
3. Resolve the §6 session-ownership approach (SDK-owned session vs stdlib fallback).
4. Write the README (install/dev flow, restart-after-first-sync note).
5. Migrate TestSVN to consume the plugin; remove its local skill copy.
6. First push to the remote, then set the `marketplace.json` release `sha`.
