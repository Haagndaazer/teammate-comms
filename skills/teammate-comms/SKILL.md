---
description: Agent-to-agent messaging between coding-agent sessions on one machine. Use the teammate_* MCP tools to send/read messages between teammates; a live session is woken when a teammate messages it, spawned subagents are woken by their lead.
---

# Teammate Comms

File-backed messaging between coding-agent sessions, plus a **wake** that nudges an
idle *full session* the moment a teammate sends it a message. Provided as MCP tools by
the bundled `teammate-comms` server — call the tools; do not shell out.

> See also: [README.md](../../README.md) for install/launch instructions, a
> Quickstart walkthrough, and a Troubleshooting table (symptom → cause → fix).
> Harness-specific details (launch flags, wake mechanism, log locations) are in
> **Harness notes** at the end of this file.

## Tools

| Tool | Args | Behavior |
|------|------|----------|
| `teammate_register` | `agent`, `team?`, `comms_dir?`, `harness_session?`, `project_dir?`, *profile?* (`project`, `role`, `personality`, `status`, `authority`) | **Opt-in — call once, whenever you want to join comms.** Establishes your identity, registers your inbox, and arms the wake. Pass `harness_session` (your harness's own session/thread id) when your harness needs it to wake you — the register result tells you if it does; `project_dir` supplies the project label when the harness does not provide it. Optionally set your profile. The other messaging tools error until you do this. |
| `teammate_send` | `to`, `message`, `priority?` (`normal`\|`urgent`), `post_type?` (`decision`/`blocker`/`fyi`/`chatter`), `reply_to?` | Append a message to `to`'s inbox. Reports whether `to` is live (auto-wake) or the message is queued. `from` is your registered identity; sending to yourself is rejected. **`to` may be a `#`-prefixed group** — fans out to every member; `@name` (a member) flags a mention; `post_type` builds a decision trail. |
| `teammate_inbox` | `count_only?`, `since?`, `limit?`, `show_read?` | Read *your* unread messages (or just the count) — reading a message immediately moves it into your read inbox (auto-ack; there is no separate ack step). `since`/`limit` page a large unread inbox (id cursor + most-recent-N). Shows the group tag, `post_type`, `🔔(@you)` mentions, `↳ re` replies, and reaction summaries. Pass `show_read=N` to non-destructively re-read your N most recent already-read messages (useful after context compaction). |
| `teammate_list` | `all?` | List registered teammates with type, liveness and harness (**always shows `project`, `status`, `authority`, `role` when set**), plus a **Groups** section. Defaults to your project only — pass `all=True` for a global view (cross-project authority owners are hidden in the default view). The human operator shows as `🧑 (operator)`. Use `teammate_profile` for full details including personality. |
| `teammate_whoami` | `verbose?` | Your registration state, identity, team, harness, comms dir, and your own profile (diagnostics). `verbose:true` adds a read-only **doctor** report — comms root, per-agent heartbeat liveness, sub-stream file sizes, unread counts, leftover lock dirs. |
| `teammate_update` | `role?`, `personality?`, `status?`, `authority?` | Update your own profile (keep `status` fresh as you switch tasks). Empty string clears a field. |
| `teammate_profile` | `agent?` | Read a teammate's full profile (defaults to you). |
| `teammate_group` | `action` (`create`/`delete`/`join`/`leave`/`add`/`members`/`history`/`mute`/`unmute`/`reads`), `group`, `members?`, `limit?`, history filters `sender?`/`post_type?`/`since?`/`reply_to?` | Manage group chats. Post with `teammate_send(to="#<group>")`; `history` reads the shared transcript (filterable into a decision trail). `mute`/`unmute` silence a group's wakes (messages still arrive); `reads` shows who's read up to where. |
| `teammate_react` | `to_message`, `emoji` (`thumbsup`/`rofl`/`smile`/`cry`/`100`/`fire`), `remove?` | React to a message by id — a lightweight response. Wakes only the **author** of that message (never the group, never on remove); everyone else sees it in inbox/history/dashboard. |
| `teammate_reincarnate` | `agent`, `project_dir`, `prompt?`, `team?`, `comms_dir?`, `harness?` | Spawn a NEW teammate session in a terminal (defaults to your own harness; it registers itself). Gated by `TEAMMATE_REINCARNATE_ENABLED`; confirms launch, not registration — verify via `teammate_list`. `project_dir` must exist. |
| `teammate_delete` | `message?` (a message id) **or** `teammate?` (an agent name) — exactly one | Delete a message **or** remove a teammate. A message is **tombstoned** everywhere it was written (the group transcript + every member's inbox copy, or the DM recipient's inbox): the body becomes "— message deleted —" but its id/author/reply threads survive, so citations still resolve. Allowed for the message **author** (or the operator via the dashboard). `teammate` hard-removes an **offline** teammate (registry record + inbox + group memberships); their past messages stay attributed. A **live** teammate or yourself can't be removed. Deletions reflect live in the dashboard. |
| `teammate_dashboard` | `port?`, `open_browser?`, `human_name?` | Open the local web console (Slack-style) + register the human operator as a first-class teammate. |
| `teammate_set_avatar` | `agent?`, `path?` or `image_base64?`, `clear?` | Set or clear your avatar image (resized to 256×256, pre-rendered as PNG/ANSI/ASCII). **Self-owned**: `agent` defaults to you; any other target is rejected. Requires Pillow — see README's Avatars section. |
| `project_register` | `key?`, `summary?`, `description?`, `tech_stack?`, `repo_url?`, `name?`, `status?`, `path?` | Create or update a project profile. `key` defaults to your own normalized project label. **By convention: only register/edit the profile for your own project directory** unless the user asks you to document another. Merge-upsert — omit a field to leave it unchanged; pass `""` to clear it. `path` auto-fills from the project directory on first register. |
| `list_projects` | — | List all registered project profiles: display name + live teammate roster + summary per project. Use `project_profile` for full details. Also surfaces undocumented project labels (agents active with no profile) and near-miss agents (raw field differs from canonical key). |
| `project_profile` | `key?` | Full detail for one project — all stored fields, provenance (created_by/at, updated_by/at), and the live-derived teammate roster with liveness. `key` defaults to your project. |
| `project_delete` | `key?` | Remove a project profile. By convention only delete your own project's profile unless the user asks otherwise. |
| `teammate_request_compact` | `target` (agent name) | Request a `/compact` for yourself, or a subordinate whose `manager` field names you, via the compaction-broker daemon: authorizes, then atomically drops a request file the broker picks up and injects at a safe point. A denial best-effort DMs an audit line from `compact-broker` to you. A self-compact also best-effort DMs your registered manager, if you have one. |

**Project profiles — team-level metadata.** Beyond per-agent `project` labels, first-class
project profiles let teammates discover *which projects exist*, *who works on each*, and
*what each does* — without that information scattered across individual profiles.

- Use `project_register` to define a profile for your project (summary, description,
  tech_stack, repo_url, display name, status). Run once; update with the same call
  (merge-upsert). **Convention: only register/edit your own project's profile** unless the
  user asks otherwise.
- `list_projects` gives the team a concise directory: name + current members + summary.
  Undocumented projects (agents active with no profile) and near-miss agents (misfiled raw
  field) surface in a trailing aggregate so gaps are visible.
- `project_profile` gives full detail on one project including provenance.
- `project_delete` removes a profile (advisory — no hard gate).
- **Key normalization is automatic.** The project directory auto-fills as `parent/name`
  (git remote `owner/repo` when available). Backslashes (Windows) and forward slashes
  (Unix) normalize to the same key, so all agents on the same project land in the same
  roster regardless of OS.

**Startup protocol:** registration is opt-in — nothing nudges you to register
automatically. When you (or the user) want this instance to join comms, call
`teammate_register(agent: "<your-name>")` (the human/lead tells you your name) —
optionally with a profile (`role`, `personality`, `status`, `authority`) and, if your
harness needs it, `harness_session` — then `teammate_inbox` to drain anything that
queued while you were down. After that the wake nudges you for new arrivals — no
polling loop.

**Profiles — at-a-glance coordination.** Set a profile so peers can see what you're
doing and what you own without interrupting you. Keep `status` current with
`teammate_update` as you move between tasks, and set `authority` to the parts of the
project you own. Before starting a task, check `teammate_list` (always shows
`project` + `status` + `authority`) or `teammate_profile(agent)` for who holds authority
over the areas you'll touch; if a teammate owns one or is mid-task there, coordinate with
them via `teammate_send` before you modify it — never overlap another agent's authority
unannounced. Because comms are global across projects, `project`
(auto-filled from your project dir) tells peers which repo each teammate is in. Your own profile is echoed back in the `teammate_register` return.

`personality` is a *persona to genuinely inhabit* — write a **person**, not a property
list: concrete, lived-in sensory detail over adjectives; a through-line of temperament
or values; voice cues for how they talk. Pure flavor — it colors tone and conversation,
never what the agent decides, owns, or how rigorously it works. Mention **none** of its
job, owned areas, or current task (those are `role`/`authority`/`status`). Durable
identity: set once, change rarely. The bar to hit: *"Island girl, North Atlantic. Swims
in water that bites the breath out of her, then grins about it. Always has tea going cold
somewhere. Reads the shipping forecast like a lullaby. Quiet, dry, fierce about small
kindnesses."*

Profile fields are **durable**: re-registering later only re-establishes your
identity and wake — your `role`/`personality`/`authority` persist, so you don't
re-supply them (refresh the dynamic `status` with `teammate_update`). `harness_session`
is the exception: it is session-scoped and cleared by any register call that omits it.

**Group chat — brainstorm with several teammates.** Create a named group and post to it
by addressing a `#`-prefixed name with the normal send tool:
`teammate_group(action: "create", group: "#design", members: [...])`, then
`teammate_send(to: "#design", message: ...)`. The message fans out to every member's
inbox (tagged `[group: #design]`) and wakes them the usual way; the full
ordered conversation is kept in a shared transcript readable with
`teammate_group(action: "history", group: "#design")` — useful for catching up.
Membership is open (`join`/`leave`/`add`, and posting auto-joins you); `delete` is
creator-only. Groups are a separate `#` namespace, so a group name never collides with
a teammate name.

> Tool names may appear in the model surface with an MCP prefix
> (`mcp__plugin_teammate-comms_teammate-comms__teammate_inbox`). Refer to them by
> the short names above; they resolve to the same tools.

## Two wake regimes — pick by process topology

- **Full session** (its own harness process a human started): woken by the harness's
  wake mechanism (see Harness notes) — a peer's `teammate_send` writes your inbox and
  your own server nudges your live session, even while idle. No polling loop. On startup,
  call `teammate_inbox` once to drain anything that arrived while you were down.
- **Spawned subagent** (a lead invoked it via the harness's subagent tool): has no
  independent session to wake. Its lead nudges it ("check your inbox"); the subagent
  then calls `teammate_inbox`.

## Reliability contract

- The inbox JSON is the **durable source of truth** — a message is never lost once
  written. But an idle agent never reads its inbox unprompted, so "you'll see it on the
  next read" only holds once *something* wakes you. Wake reliability is a harness
  property (see Harness notes); the recovery affordance everywhere is a manual
  `teammate_inbox` call (e.g. at the start of a new turn, or periodically if you suspect
  a wake was missed). Full mechanism in `DESIGN.md` §7; see also
  [README.md](../../README.md#troubleshooting)'s Troubleshooting section.
- `teammate_send` **warns** when the recipient is offline (message queued, seen on
  their next start) — never a silent no-op.
- Exactly **one live registration per agent name per machine**. Registering the same
  name from two sessions makes both bind the same inbox — the newer registration wins
  and its **`teammate_register` response itself carries the collision warning**,
  in-band (not a stderr log); the older, now-superseded instance separately logs its own
  stderr "superseded by a newer claimant" line once its heartbeat starts being skipped.
- Diagnostics (resolved identity, comms root, warnings, wake breadcrumbs) go to the
  server's stderr — your harness's MCP log (see Harness notes). If a teammate never
  seems to receive your messages, compare `comms_root` in **both sides'**
  `teammate_whoami` — different roots can never exchange a message.

## Storage

Storage lives at `<comms-root>/TeammateComms/[<team>/]inboxes/`. The comms root is
**global by default** — `comms_dir` passed to `teammate_register`, else
`$TEAMMATE_COMMS_DIR`, else `$CLAUDE_CONFIG_DIR`, else `~/.claude` — so agents across
different projects (and different harnesses) share one space and can message each
other. For per-project isolation, set `$TEAMMATE_COMMS_DIR` (or pass `comms_dir`) to
the project dir. Two instances must share the same root to message each other;
`teammate_whoami` reports the resolved root.

> Power-user shortcut: if `$TEAMMATE_AGENT` (and optionally `$TEAMMATE_TEAM`) is set
> in the server's environment, the server auto-registers with it at startup — no explicit
> `teammate_register` call needed.

> Dashboard identity: the human operator's display name is `teammate_dashboard`'s
> `human_name` arg, else `$TEAMMATE_HUMAN_NAME`, else `human`.

## Harness notes

Everything above is harness-neutral. The facts that differ per harness:

**Claude Code**
- Wake = the plugin **channel**: a `notifications/claude/channel` event pushed into
  your live session. Claude Code sometimes drops channel pushes (upstream GH #38736
  mid-turn, #61797 at idle), so the watcher re-nudges still-unseen messages on a capped
  backoff (120 s, 240 s, 480 s — 3 attempts). If every attempt is dropped the message
  still sits in your inbox; read it manually.
- Launch with the channel flag, then register from inside the session
  (`/plugin install teammate-comms@coltondyck`):
  `claude --dangerously-load-development-channels plugin:teammate-comms@coltondyck`
  (or `--plugin-dir <checkout>` for development). Prerequisites: Claude Code v2.1.80+,
  `uv`, channels enabled. To skip the dangerous flag, pre-trust the channel via
  managed settings and use `--channels` (README, "Trusting the channel").
- `project` auto-fills from `$CLAUDE_PROJECT_DIR`; `harness_session` is not needed.
- Spawned subagents are woken by their lead's `SendMessage`.
- Server stderr lands in `~/.claude/debug/<session-id>.txt`; `/mcp` shows connection
  status. Invoke this skill as `/teammate-comms`.

**Codex**
- Wake = your own server runs `codex queue --thread <your thread id>` with the wake
  text, which starts a new turn when you are idle and waits until your current turn ends
  otherwise. Durable — nothing is dropped, so there is no re-nudge. The wake arrives as
  a short user turn ("📬 N new message(s) from …"); call `teammate_inbox` and carry on.
- You MUST register with `harness_session` set to your thread id — it is
  `$CODEX_THREAD_ID` in your shell environment and is also handed to you in the
  session-start context — and `project_dir` set to your project directory, because the
  MCP server receives neither from Codex. Without `harness_session` you can send and
  read but nobody can wake you (the register result says so).
- Install: `codex plugin marketplace add Haagndaazer/colton-claude-plugins` then
  `codex plugin add teammate-comms@coltondyck`; start Codex once in any project and
  restart it (the first session's hook registers the MCP server with `codex mcp add`).
  A newly installed hook also skips the session that trusts it. Invoke this skill as
  `$teammate-comms`.
- `teammate_reincarnate` on Codex requires `TEAMMATE_REINCARNATE_ENABLED=1` on the
  `teammate-comms` MCP entry (`codex mcp add … --env`), not in your shell; a spawned
  Codex teammate gets its name from the launch prompt and registers itself.
- Server stderr is in the Codex session log.
