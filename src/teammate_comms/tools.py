"""MCP tool definitions and handlers for teammate-comms.

The server's own resolved identity supplies the implicit ``from`` / inbox owner;
tools never accept it as an argument. Every handler returns a plain string (the
``tools/call`` envelope requires ``content[].text`` to be a string) and raises
``CommsError`` for bad input — the dispatcher converts that into an ``isError``
result so a single bad call never tears down the long-lived server.

No handler may ``print`` to stdout: stdout is the JSON-RPC stream. Diagnostics go
to stderr (via the server's ``log``); user-facing failures go into the envelope.
"""

import json

from .comms import (
    CommsError,
    ensure_inbox,
    file_lock,
    get_agents_dir,
    get_inboxes_dir,
    is_channel_alive,
    now_timestamp,
    read_agent_record,
    read_json_safe,
    resolve_comms_root,
    validate_agent_name,
    write_json_atomic,
)

_PRIORITIES = ("normal", "urgent")

# ── Tool schemas (advertised via tools/list) ────────────────────────────────
# No-arg tools still declare an explicit empty-object inputSchema — some clients
# reject a missing schema.
TOOL_DEFINITIONS = [
    {
        "name": "teammate_send",
        "description": (
            "Send a message to another teammate's inbox. If the recipient is a "
            "live full instance, their channel nudges them automatically; "
            "otherwise the message is queued and seen on their next start."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient agent name."},
                "message": {"type": "string", "description": "Message body."},
                "priority": {
                    "type": "string",
                    "enum": list(_PRIORITIES),
                    "description": "Message priority (default 'normal').",
                },
            },
            "required": ["to", "message"],
        },
    },
    {
        "name": "teammate_inbox",
        "description": "Read your own unread messages (or just the unread count).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count_only": {
                    "type": "boolean",
                    "description": "If true, return only the unread count.",
                },
            },
        },
    },
    {
        "name": "teammate_ack",
        "description": (
            "Acknowledge a message (move it from unread to read). Pass a specific "
            "message id, or \"all\" to clear the whole inbox."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Message id to ack, or \"all\".",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "teammate_list",
        "description": "List registered teammates with their type and liveness.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "teammate_whoami",
        "description": "Report this instance's resolved identity, team, and comms dir.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

TOOL_NAMES = {t["name"] for t in TOOL_DEFINITIONS}


# ── Handlers ────────────────────────────────────────────────────────────────

def _handle_send(args, agent, team):
    to = args.get("to")
    validate_agent_name(to)  # raises CommsError on bad/missing
    if to == agent:
        raise CommsError(
            "Cannot send to yourself. teammate_send targets another teammate; "
            "use teammate_inbox to read your own messages."
        )

    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        raise CommsError("'message' is required and must be a non-empty string.")
    content = message.strip()

    priority = args.get("priority", "normal")
    if priority not in _PRIORITIES:
        raise CommsError(f"'priority' must be one of {list(_PRIORITIES)}.")

    inboxes_dir = get_inboxes_dir(team)
    ensure_inbox(inboxes_dir, to)
    unread_file = inboxes_dir / f"{to}_unread.json"

    record = {
        "id": now_timestamp(),
        "from": agent,
        "priority": priority,
        "message": content,
    }
    with file_lock(unread_file):
        messages = read_json_safe(unread_file)
        messages.append(record)
        write_json_atomic(unread_file, messages)

    lines = [f"Message sent to {to} (id: {record['id']})."]
    to_record = read_agent_record(team, to)
    if to_record and to_record.get("type") == "full":
        if is_channel_alive(to_record):
            lines.append(f"{to}'s channel is live — they will be nudged automatically.")
        else:
            lines.append(
                f"WARNING: {to}'s channel is not running. The message is queued "
                f"and will be seen when they next start their instance."
            )
    else:
        lines.append(
            f"{to} has no live channel. If they are a spawned subagent, their "
            f"lead must SendMessage-nudge them to check their inbox."
        )
    return "\n".join(lines)


def _handle_inbox(args, agent, team):
    inboxes_dir = get_inboxes_dir(team)
    unread_file = inboxes_dir / f"{agent}_unread.json"
    ensure_inbox(inboxes_dir, agent)
    messages = read_json_safe(unread_file)

    if args.get("count_only"):
        return str(len(messages))

    if not messages:
        return "No unread messages."

    out = [f"=== {len(messages)} unread message(s) for {agent} ==="]
    for msg in messages:
        tag = " [URGENT]" if msg.get("priority") == "urgent" else ""
        out.append(f"\n--- id: {msg.get('id')} | from: {msg.get('from')}{tag} ---")
        out.append(str(msg.get("message", "")))
    return "\n".join(out)


def _handle_ack(args, agent, team):
    msg_id = args.get("id")
    if not isinstance(msg_id, str) or not msg_id.strip():
        raise CommsError("'id' is required (a message id, or \"all\").")
    msg_id = msg_id.strip()

    inboxes_dir = get_inboxes_dir(team)
    ensure_inbox(inboxes_dir, agent)
    unread_file = inboxes_dir / f"{agent}_unread.json"
    read_file = inboxes_dir / f"{agent}_read.json"

    with file_lock(unread_file):
        unread = read_json_safe(unread_file)
        read = read_json_safe(read_file)

        if not unread:
            return "No unread messages to acknowledge."

        if msg_id == "all":
            count = len(unread)
            read.extend(unread)
            unread = []
            result = f"Acknowledged all {count} message(s)."
        else:
            to_ack = next((m for m in unread if m.get("id") == msg_id), None)
            if to_ack is None:
                available = ", ".join(m.get("id", "?") for m in unread) or "(none)"
                raise CommsError(
                    f"No unread message with id {msg_id!r}. Available ids: {available}"
                )
            read.append(to_ack)
            unread = [m for m in unread if m.get("id") != msg_id]
            result = f"Acknowledged message {msg_id} from {to_ack.get('from')}."

        write_json_atomic(unread_file, unread)
        write_json_atomic(read_file, read)
    return result


def _handle_list(args, agent, team):
    agents_dir = get_agents_dir(team)
    if not agents_dir.exists():
        return "No registered teammates yet."

    rows = []
    for path in sorted(agents_dir.glob("*.json")):
        record = read_agent_record(team, path.stem)
        if not isinstance(record, dict):
            continue
        # Heartbeat-freshness only (pid_check=False) so listing many agents does
        # not spawn one liveness subprocess per agent.
        live = is_channel_alive(record, pid_check=False)
        kind = record.get("type", "unknown")
        status = "live" if live else "offline"
        me = " (you)" if path.stem == agent else ""
        rows.append(f"  - {path.stem}{me}: type={kind}, channel={status}")
    if not rows:
        return "No registered teammates yet."
    return "Registered teammates:\n" + "\n".join(rows)


def _handle_whoami(args, agent, team):
    try:
        root, source = resolve_comms_root()
        root_str = str(root)
    except CommsError as e:
        root_str, source = f"<unresolved: {e}>", "none"
    info = {
        "agent": agent,
        "team": team,
        "comms_root": root_str,
        "comms_root_source": source,
        "inboxes_dir": str(get_inboxes_dir(team)) if source != "none" else None,
    }
    return json.dumps(info, indent=2, ensure_ascii=False)


_HANDLERS = {
    "teammate_send": _handle_send,
    "teammate_inbox": _handle_inbox,
    "teammate_ack": _handle_ack,
    "teammate_list": _handle_list,
    "teammate_whoami": _handle_whoami,
}


def dispatch(name, arguments, agent, team):
    """Run a tool. Returns ``(text: str, is_error: bool)`` — never raises.

    Unknown tool names and any handler ``CommsError`` (or unexpected exception)
    are converted to an ``isError`` result so the server stays alive.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return (f"Unknown tool {name!r}.", True)
    args = arguments if isinstance(arguments, dict) else {}
    try:
        return (handler(args, agent, team), False)
    except CommsError as e:
        return (f"{name} failed: {e}", True)
    except Exception as e:  # defensive: never let a tool kill the server
        return (f"{name} failed unexpectedly: {e}", True)
