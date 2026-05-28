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
    PROFILE_FIELDS,
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
    validate_profile_field,
    write_agent_record,
    write_json_atomic,
)

_PRIORITIES = ("normal", "urgent")

# Per-field descriptions reused by teammate_register and teammate_update schemas.
_PROFILE_DESCRIPTIONS = {
    "role": "Your job/role on the team (e.g. 'backend / API').",
    "personality": "A short personality blurb (mostly for fun).",
    "status": "What you're doing right now — keep this fresh so teammates can see it at a glance.",
    "authority": "Areas of the project you own (e.g. 'src/auth/**, billing'), so teammates know before modifying them.",
}


def _profile_schema_properties():
    """inputSchema 'properties' for the four optional profile fields.

    The per-field char cap is appended from PROFILE_FIELDS so the advertised limit
    always matches what validate_profile_field actually enforces.
    """
    return {
        name: {
            "type": "string",
            "description": f"{_PROFILE_DESCRIPTIONS[name]} Max {PROFILE_FIELDS[name]} chars, single line.",
        }
        for name in PROFILE_FIELDS
    }


def _collect_profile_args(args):
    """Return {field: raw value} for any profile field present in args (unvalidated)."""
    return {name: args[name] for name in PROFILE_FIELDS if name in args}

TOOL_DEFINITIONS = [
    {
        "name": "teammate_register",
        "description": (
            "Establish this instance's identity (call once at session start, like "
            "the old setup step). Registers your inbox and starts the channel that "
            "wakes you when teammates message you. Run teammate_inbox afterward to "
            "drain anything that arrived while you were down. Optionally set your "
            "profile (role, personality, status, authority) — update it later with "
            "teammate_update."
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
                **_profile_schema_properties(),
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
        "description": "Report this instance's registration state, identity, comms dir, and your own profile.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "teammate_update",
        "description": (
            "Update your own profile fields (role, personality, status, authority). "
            "Use this to keep your status fresh as you move between tasks so "
            "teammates can see what you're doing without interrupting you. Updates "
            "only your own record; pass an empty string to clear a field."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _profile_schema_properties(),
        },
    },
    {
        "name": "teammate_profile",
        "description": (
            "Read a teammate's full profile (role, personality, status, authority) "
            "plus their type and liveness. Omit 'agent' to read your own."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Teammate to read (defaults to you)."},
            },
        },
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
    profile = _collect_profile_args(args)  # validated inside register_identity
    # ctx["register"] does the side effects (resolve root, inbox, registry,
    # start watching) and returns a human-readable status string.
    return ctx["register"](agent, team.strip() if team else None, comms_dir, profile)


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
        # status + authority always surface — they are the at-a-glance fields.
        rows.append(f"      status:    {record.get('status') or '(not set)'}")
        rows.append(f"      authority: {record.get('authority') or '(not set)'}")
        role = record.get("role")
        if role:
            rows.append(f"      role:      {role}")
        personality = record.get("personality")
        if personality:
            rows.append(f"      personality: {personality}")
    return "Registered teammates:\n" + "\n".join(rows) if rows else "No registered teammates yet."


def _handle_whoami(args, ctx):
    agent, team, root, _ = ctx["identity"].snapshot()
    if agent is None:
        return json.dumps({
            "registered": False,
            "hint": "Call teammate_register(agent=\"<your-name>\") to establish identity.",
        }, indent=2)
    record = read_agent_record(root, team, agent) or {}
    info = {
        "registered": True,
        "agent": agent,
        "team": team,
        "comms_root": str(root),
        "inboxes_dir": str(get_inboxes_dir(root, team)),
        "profile": {field: record.get(field) for field in PROFILE_FIELDS},
    }
    return json.dumps(info, indent=2, ensure_ascii=False)


def _handle_update(args, ctx):
    agent, team, root = _require_registered(ctx)
    raw = _collect_profile_args(args)
    if not raw:
        raise CommsError(f"Provide at least one profile field to update: {sorted(PROFILE_FIELDS)}.")
    fields = {k: validate_profile_field(k, v) for k, v in raw.items()}
    # Best-effort lock: a False return means the write was dropped (e.g. heartbeat
    # contention) — surface that instead of falsely reporting success.
    if not write_agent_record(root, team, agent, timeout=5, **fields):
        raise CommsError("Could not update profile (registry busy). Try again.")
    parts = ", ".join(f"{k}={v!r}" if v else f"{k} cleared" for k, v in fields.items())
    return f"Profile updated: {parts}."


def _format_profile(record, name, is_self=False):
    """Render an agent record as a readable profile block (identity + profile fields)."""
    live = is_channel_alive(record, pid_check=False)
    me = " (you)" if is_self else ""
    lines = [
        f"Profile: {name}{me}",
        f"  {'type:':<13}{record.get('type', 'unknown')}",
        f"  {'channel:':<13}{'live' if live else 'offline'}",
    ]
    for field in PROFILE_FIELDS:
        value = record.get(field)
        lines.append(f"  {field + ':':<13}{value if value else '(not set)'}")
    return "\n".join(lines)


def _handle_profile(args, ctx):
    agent, team, root = _require_registered(ctx)
    target = args.get("agent")
    if target is not None:
        validate_agent_name(target)
    else:
        target = agent
    record = read_agent_record(root, team, target)
    if not record:
        raise CommsError(f"No registered teammate named {target!r}.")
    return _format_profile(record, target, is_self=(target == agent))


_HANDLERS = {
    "teammate_register": _handle_register,
    "teammate_send": _handle_send,
    "teammate_inbox": _handle_inbox,
    "teammate_ack": _handle_ack,
    "teammate_list": _handle_list,
    "teammate_whoami": _handle_whoami,
    "teammate_update": _handle_update,
    "teammate_profile": _handle_profile,
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
