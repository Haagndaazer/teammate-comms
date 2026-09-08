# Harness parity — teammate-comms

One row per tool, hook, skill and CLI × harness. Values: `full` | `partial` | `waived` |
`n/a`; anything not `full` carries a note. `tests/test_codex.py` fails when a tool in
`TOOL_DEFINITIONS` or a script in `hooks/` has no row here.

| Surface | claude-code | codex | Note |
|---|---|---|---|
| teammate_register | full | full | Codex passes `harness_session` (its thread id) and `project_dir`; the register result reminds it when missing. |
| teammate_send | full | full | |
| teammate_inbox | full | full | |
| teammate_list | full | full | `harness:` line on every non-human record. |
| teammate_whoami | full | full | |
| teammate_update | full | full | |
| teammate_profile | full | full | |
| teammate_group | full | full | |
| teammate_react | full | full | Reaction wakes go through the harness waker. |
| teammate_reincarnate | full | partial | Codex child gets its identity from the launch prompt; `spawned_by` is not recorded; the gate must be set on the MCP entry env. |
| teammate_dashboard | full | full | |
| teammate_set_avatar | full | full | |
| teammate_delete | full | full | |
| project_register | full | full | `path` auto-fill needs `project_dir` at register on Codex. |
| list_projects | full | full | |
| project_profile | full | full | |
| project_delete | full | full | |
| teammate_request_compact | full | partial | The broker types `/compact` into a WezTerm pane; works on Codex only inside WezTerm. |
| idle wake | full | full | Claude: channel push (may be dropped upstream; re-nudge backoff). Codex: `codex queue` on own thread (durable; no re-nudge). |
| hooks/session-start.sh | full | full | Codex wraps it via `adapters/codex/hooks/session-start.sh`. |
| skills/teammate-comms | full | full | Invoked as `/teammate-comms` vs `$teammate-comms`. |
| python -m teammate_comms.deliver | full | full | |
| plugin install / update | full | full | Claude marketplace pin vs `.agents/plugins/marketplace.json`; Codex needs one restart after install/update. |
