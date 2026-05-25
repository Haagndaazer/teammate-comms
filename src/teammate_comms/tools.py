"""MCP tool definitions and handlers for teammate-comms.

Identity is established at runtime by ``teammate_register`` (the setup.py
equivalent) rather than baked into the MCP launch config. Until an instance
registers, the messaging tools return an ``isError`` asking the agent to register
first; ``teammate_whoami`` always works (it reports the unregistered state).

Every handler returns a plain string (the ``tools/call`` envelope requires
``content[].text`` to be a string) and raises ``CommsError`` for bad input — the
dispatcher converts that into an ``isError`` result so a single bad call never
tears down the long-lived server. No handler may ``print`` to stdout: that is the
JSON-RPC stream. Diagnostics go to stderr; failures go into the envelope.
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
    validate_agent_name,
    write_json_atomic,
)

_PRIORITIES = ("normal", "urgent")

TOOL_DEFINITIONS = [
    {
        "name": "teammate_register",
        "description": (
            "Establish this instance's identity (call once at session start, like "
            "the old setup step). Registers your inbox and starts the channel that "
            "wakes you when teammates message you. Run teammate_inbox afterward to "
            "drain anything that arrived while you were down."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Your agent name (how teammates address you)."},
                "team": {"type": "string", "description": "Optional team name for namespaced inboxes."},
                "comms_dir": {
                    "type": "string",
                    "description": "Optional comms root override (else $TEAMMATE_COMMS_DIR or the project dir).",
                },
            },
            "required": ["agent"],
        },
    },
    {
        "name": "teammate_send",
        "description": (
            "Send a message to another teammate's inbox. If the recipient is a live "
            "full instance, their channel nudges them automatically; otherwise the "
            "message is queued and seen on their next start."
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
                "count_only": {"type": "boolean", "description": "If true, return only the unread count."},
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
                "id": {"type": "string", "description": "Message id to ack, or \"all\"."},
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
        "description": "Report this instance's registration state, identity, and comms dir.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _require_registered(ctx):
    """Return (agent, team, root) or raise CommsError if not yet registered."""
    agent, team, root, _ = ctx["identity"].snapshot()
    if agent is None or root is None:
        raise CommsError(
            "Not registered yet. Call teammate_register(agent=\"<your-name>\") "
            "first to establish your identity and start your channel."
        )
    return agent, team, root


# ── Handlers ────────────────────────────────────────────────────────────────

def _handle_register(args, ctx):
    agent = args.get("agent")
    validate_agent_name(agent)  # raises CommsError on bad/missing
    team = args.get("team")
    if team is not None and (not isinstance(team, str) or not team.strip()):
        team = None
    comms_dir = args.get("comms_dir")
    # ctx["register"] does the side effects (resolve root, inbox, registry,
    # start watching) and returns a human-readable status string.
    return ctx["register"](agent, team.strip() if team else None, comms_dir)


def _handle_send(args, ctx):
    agent, team, root = _require_registered(ctx)
    to = args.get("to")
    validate_agent_name(to)
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

    inboxes_dir = get_inboxes_dir(root, team)
    ensure_inbox(inboxes_dir, to)
    unread_file = inboxes_dir / f"{to}_unread.json"

    record = {"id": now_timestamp(), "from": agent, "priority": priority, "message": content}
    with file_lock(unread_file):
        messages = read_json_safe(unread_file)
        messages.append(record)
        write_json_atomic(unread_file, messages)

    lines = [f"Message sent to {to} (id: {record['id']})."]
    to_record = read_agent_record(root, team, to)
    if to_record and to_record.get("type") == "full":
        if is_channel_alive(to_record):
            lines.append(f"{to}'s channel is live — they will be nudged automatically.")
        else:
            lines.append(
                f"WARNING: {to}'s channel is not running. The message is queued and "
                f"will be seen when they next start their instance."
            )
    else:
        lines.append(
            f"{to} has no live channel. If they are a spawned subagent, their lead "
            f"must SendMessage-nudge them to check their inbox."
        )
    return "\n".join(lines)


def _handle_inbox(args, ctx):
    agent, team, root = _require_registered(ctx)
    inboxes_dir = get_inboxes_dir(root, team)
    ensure_inbox(inboxes_dir, agent)
    messages = read_json_safe(inboxes_dir / f"{agent}_unread.json")

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


def _handle_ack(args, ctx):
    agent, team, root = _require_registered(ctx)
    msg_id = args.get("id")
    if not isinstance(msg_id, str) or not msg_id.strip():
        raise CommsError("'id' is required (a message id, or \"all\").")
    msg_id = msg_id.strip()

    inboxes_dir = get_inboxes_dir(root, team)
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
                raise CommsError(f"No unread message with id {msg_id!r}. Available ids: {available}")
            read.append(to_ack)
            unread = [m for m in unread if m.get("id") != msg_id]
            result = f"Acknowledged message {msg_id} from {to_ack.get('from')}."

        write_json_atomic(unread_file, unread)
        write_json_atomic(read_file, read)
    return result


def _handle_list(args, ctx):
    _agent, team, root = _require_registered(ctx)
    agents_dir = get_agents_dir(root, team)
    if not agents_dir.exists():
        return "No registered teammates yet."

    rows = []
    for path in sorted(agents_dir.glob("*.json")):
        record = read_agent_record(root, team, path.stem)
        if not isinstance(record, dict):
            continue
        # Heartbeat-freshness only (no per-agent liveness subprocess).
        live = is_channel_alive(record, pid_check=False)
        kind = record.get("type", "unknown")
        me = " (you)" if path.stem == _agent else ""
        rows.append(f"  - {path.stem}{me}: type={kind}, channel={'live' if live else 'offline'}")
    return "Registered teammates:\n" + "\n".join(rows) if rows else "No registered teammates yet."


def _handle_whoami(args, ctx):
    agent, team, root, _ = ctx["identity"].snapshot()
    if agent is None:
        return json.dumps({
            "registered": False,
            "hint": "Call teammate_register(agent=\"<your-name>\") to establish identity.",
        }, indent=2)
    info = {
        "registered": True,
        "agent": agent,
        "team": team,
        "comms_root": str(root),
        "inboxes_dir": str(get_inboxes_dir(root, team)),
    }
    return json.dumps(info, indent=2, ensure_ascii=False)


_HANDLERS = {
    "teammate_register": _handle_register,
    "teammate_send": _handle_send,
    "teammate_inbox": _handle_inbox,
    "teammate_ack": _handle_ack,
    "teammate_list": _handle_list,
    "teammate_whoami": _handle_whoami,
}


def dispatch(name, arguments, ctx):
    """Run a tool. Returns ``(text: str, is_error: bool)`` — never raises."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return (f"Unknown tool {name!r}.", True)
    args = arguments if isinstance(arguments, dict) else {}
    try:
        return (handler(args, ctx), False)
    except CommsError as e:
        return (f"{name} failed: {e}", True)
    except Exception as e:  # defensive: never let a tool kill the server
        return (f"{name} failed unexpectedly: {e}", True)
