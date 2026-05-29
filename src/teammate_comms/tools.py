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
    append_group_message,
    delete_group,
    ensure_inbox,
    file_lock,
    get_agents_dir,
    get_group_dir,
    get_groups_dir,
    get_inboxes_dir,
    is_channel_alive,
    now_timestamp,
    read_agent_record,
    read_group_messages,
    read_group_meta,
    read_json_safe,
    validate_agent_name,
    validate_group_name,
    validate_profile_field,
    write_agent_record,
    write_group_meta,
    write_json_atomic,
)

_PRIORITIES = ("normal", "urgent")
_GROUP_ACTIONS = ("create", "delete", "join", "leave", "add", "members", "history")

# Per-field descriptions reused by teammate_register and teammate_update schemas.
_PROFILE_DESCRIPTIONS = {
    "project": (
        "The project/repo you're working in. Auto-filled from the current project "
        "directory at registration — set this only to override the auto-filled value."
    ),
    "role": "Your job/role on the team (e.g. 'backend / API').",
    "personality": (
        "Give the agent a bit of human soul: a persona to genuinely inhabit, not a property list. "
        "Write a PERSON — concrete, lived-in sensory detail over adjectives ('swims in water that "
        "bites the breath out of her, then grins' beats 'adventurous'); a through-line of temperament "
        "or values that ties the details together; voice cues for how they talk (deadpan, warm, gruff). "
        "Pure flavor: it colors tone and conversation, never what the agent decides, owns, or how "
        "rigorously it works. Mention NONE of its job, owned areas, or current task — those are the "
        "role/authority/status fields; if you can tell what the agent does from this, rewrite it. "
        "Durable identity: set once, change rarely (unlike status, which you refresh). The bar to hit: "
        "'Island girl, North Atlantic. Swims in water that bites the breath out of her, then grins about "
        "it. Always has tea going cold somewhere. Reads the shipping forecast like a lullaby. Quiet, dry, "
        "fierce about small kindnesses.'"
    ),
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
            "teammate_update. Re-registering later only re-establishes your identity "
            "and channel: your existing profile is preserved, so you do NOT need to "
            "re-supply role/personality/authority — pass a field only to change it."
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
            "Send a message to another teammate's inbox, OR to a group chat by passing "
            "a '#'-prefixed group name as 'to' (e.g. '#design') — a group message fans "
            "out to every member. If the recipient is a live full instance, their "
            "channel nudges them automatically; otherwise the message is queued and "
            "seen on their next start. (Manage groups with teammate_group.)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient agent name, or a '#'-prefixed group name (e.g. '#design')."},
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
    {
        "name": "teammate_group",
        "description": (
            "Manage group chats for brainstorming with multiple teammates. A group is "
            "addressed like a teammate but with a '#' prefix: post to it with "
            "teammate_send(to=\"#<group>\"), which fans out to every member and wakes "
            "them. Membership is open (anyone can join/add). Actions: create, delete "
            "(creator-only), join, leave, add (members), members (list), history (read "
            "the shared transcript)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_GROUP_ACTIONS),
                    "description": "create | delete | join | leave | add | members | history",
                },
                "group": {"type": "string", "description": "Group name (a leading '#' is optional)."},
                "members": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Member names — for 'create' (initial members) and 'add'.",
                },
                "limit": {"type": "integer", "description": "For 'history': max messages to return (default 50)."},
            },
            "required": ["action", "group"],
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


def _validate_message(args):
    """Return the trimmed message body, or raise CommsError."""
    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        raise CommsError("'message' is required and must be a non-empty string.")
    return message.strip()


def _validate_priority(args):
    priority = args.get("priority", "normal")
    if priority not in _PRIORITIES:
        raise CommsError(f"'priority' must be one of {list(_PRIORITIES)}.")
    return priority


def _handle_send(args, ctx):
    agent, team, root = _require_registered(ctx)
    to = args.get("to")
    if not isinstance(to, str) or not to.strip():
        raise CommsError("'to' is required (a teammate name, or a '#'-prefixed group name).")
    to = to.strip()

    # Group path: a '#'-prefixed recipient fans out to the group's members.
    if to.startswith("#"):
        return _send_to_group(agent, team, root, to, args)

    validate_agent_name(to)
    if to == agent:
        raise CommsError(
            "Cannot send to yourself. teammate_send targets another teammate; "
            "use teammate_inbox to read your own messages."
        )

    content = _validate_message(args)
    priority = _validate_priority(args)

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
        grp = msg.get("group")
        gtag = f" [group: {grp}]" if grp else ""
        out.append(f"\n--- id: {msg.get('id')} | from: {msg.get('from')}{gtag}{tag} ---")
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
        # project + status + authority always surface — the at-a-glance fields
        # (project matters most now that comms are global across projects).
        rows.append(f"      project:   {record.get('project') or '(not set)'}")
        rows.append(f"      status:    {record.get('status') or '(not set)'}")
        rows.append(f"      authority: {record.get('authority') or '(not set)'}")
        role = record.get("role")
        if role:
            rows.append(f"      role:      {role}")
        personality = record.get("personality")
        if personality:
            rows.append(f"      personality: {personality}")
    teammates = "Registered teammates:\n" + "\n".join(rows) if rows else "No registered teammates yet."

    # Groups section — groups are addressed like teammates (with a '#' prefix).
    group_rows = []
    groups_dir = get_groups_dir(root, team)
    if groups_dir.exists():
        for gp in sorted(p for p in groups_dir.iterdir() if p.is_dir()):
            meta = read_group_meta(root, team, gp.name)
            if not isinstance(meta, dict):
                continue
            members = meta.get("members", [])
            mark = " (member)" if _agent in members else ""
            group_rows.append(f"  - #{gp.name}{mark}: {len(members)} member(s) — {', '.join(members) or '(none)'}")
    if group_rows:
        return teammates + "\n\nGroups:\n" + "\n".join(group_rows)
    return teammates


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


# ── Group chat ────────────────────────────────────────────────────────────────

def _group_meta_file(root, team, group):
    return get_group_dir(root, team, group) / "meta.json"


def _send_to_group(agent, team, root, to_sigil, args):
    """Fan a message out to every member of a group + record it in the transcript."""
    group = validate_group_name(to_sigil)  # strips '#', validates
    sigil = f"#{group}"
    meta = read_group_meta(root, team, group)
    if meta is None:
        raise CommsError(
            f"No group {sigil!r}. Create it with "
            f"teammate_group(action=\"create\", group=\"{sigil}\")."
        )
    content = _validate_message(args)
    priority = _validate_priority(args)

    # Open membership: posting auto-joins the sender (re-read under lock to add).
    members = list(meta.get("members", []))
    if agent not in members:
        with file_lock(_group_meta_file(root, team, group)):
            meta = read_group_meta(root, team, group) or meta
            members = list(meta.get("members", []))
            if agent not in members:
                members.append(agent)
                meta["members"] = members
                write_group_meta(root, team, group, meta)

    record = {"id": now_timestamp(), "from": agent, "group": sigil,
              "priority": priority, "message": content}

    # Transcript is the canonical, ordered source of truth — write it first.
    append_group_message(root, team, group, record)

    # Best-effort fan-out into each other member's inbox. A locked/failed inbox is
    # NON-FATAL: the transcript is authoritative and the member catches up via history.
    inboxes_dir = get_inboxes_dir(root, team)
    delivered, live, deferred = [], 0, []
    for member in members:
        if member == agent:
            continue
        try:
            ensure_inbox(inboxes_dir, member)
            unread_file = inboxes_dir / f"{member}_unread.json"
            with file_lock(unread_file):
                msgs = read_json_safe(unread_file)
                msgs.append(record)
                write_json_atomic(unread_file, msgs)
            delivered.append(member)
            rec = read_agent_record(root, team, member)
            if rec and is_channel_alive(rec, pid_check=False):  # heartbeat-only (no N tasklist)
                live += 1
        except CommsError:
            deferred.append(member)

    lines = [f"Posted to {sigil} (id: {record['id']})."]
    if delivered:
        lines.append(
            f"Delivered to {len(delivered)} member(s) ({live} live, "
            f"{len(delivered) - live} queued): {', '.join(delivered)}."
        )
    else:
        lines.append("No other members yet — recorded in the transcript only.")
    if deferred:
        lines.append(f"Deferred (inbox busy; will catch up via history): {', '.join(deferred)}.")
    return "\n".join(lines)


def _edit_group_members(root, team, group, sigil, mutate):
    """Lock meta.json, apply mutate(meta)->meta, write. Raises if the group is gone."""
    with file_lock(_group_meta_file(root, team, group)):
        meta = read_group_meta(root, team, group)
        if meta is None:
            raise CommsError(f"No group {sigil!r}.")
        meta = mutate(meta)
        write_group_meta(root, team, group, meta)
        return meta


def _handle_group(args, ctx):
    agent, team, root = _require_registered(ctx)
    action = args.get("action")
    if action not in _GROUP_ACTIONS:  # MCP clients don't enforce the schema enum
        raise CommsError(f"'action' must be one of {list(_GROUP_ACTIONS)}.")
    group = validate_group_name(args.get("group"))  # raises on missing/bad
    sigil = f"#{group}"

    if action == "create":
        members = [agent]
        for m in args.get("members") or []:
            validate_agent_name(m)
            if m not in members:
                members.append(m)
        get_group_dir(root, team, group).mkdir(parents=True, exist_ok=True)  # so the .lock dir can be made
        with file_lock(_group_meta_file(root, team, group)):
            if read_group_meta(root, team, group) is not None:
                raise CommsError(f"Group {sigil!r} already exists.")
            write_group_meta(root, team, group, {
                "name": group, "members": members,
                "creator": agent, "createdAt": now_timestamp(),
            })
        return (f"Created group {sigil} with member(s): {', '.join(members)}. "
                f"Post with teammate_send(to=\"{sigil}\").")

    if action == "delete":
        meta = read_group_meta(root, team, group)
        if meta is None:
            raise CommsError(f"No group {sigil!r}.")
        creator = meta.get("creator")
        members = meta.get("members", [])
        # Creator-only, OR any member if the creator is no longer a member (orphan).
        if not (agent == creator or (creator not in members and agent in members)):
            raise CommsError(f"Only the creator ({creator}) can delete {sigil}.")
        delete_group(root, team, group)
        return f"Deleted group {sigil}."

    if action == "join":
        def mutate(meta):
            members = meta.get("members", [])
            if agent not in members:
                members.append(agent)
            meta["members"] = members
            return meta
        meta = _edit_group_members(root, team, group, sigil, mutate)
        return f"Joined {sigil}. Members: {', '.join(meta['members'])}."

    if action == "leave":
        def mutate(meta):
            meta["members"] = [m for m in meta.get("members", []) if m != agent]
            return meta
        meta = _edit_group_members(root, team, group, sigil, mutate)
        return f"Left {sigil}. Members: {', '.join(meta['members']) or '(none)'}."

    if action == "add":
        to_add = args.get("members") or []
        if not to_add:
            raise CommsError("Provide 'members' (a list of names) to add.")
        for m in to_add:
            validate_agent_name(m)

        def mutate(meta):
            members = meta.get("members", [])
            for m in to_add:
                if m not in members:
                    members.append(m)
            meta["members"] = members
            return meta
        meta = _edit_group_members(root, team, group, sigil, mutate)
        return f"Added to {sigil}. Members: {', '.join(meta['members'])}."

    if action == "members":
        meta = read_group_meta(root, team, group)
        if meta is None:
            raise CommsError(f"No group {sigil!r}.")
        mem = meta.get("members", [])
        return (f"{sigil} — creator: {meta.get('creator')}, "
                f"members ({len(mem)}): {', '.join(mem) or '(none)'}.")

    # action == "history"
    if read_group_meta(root, team, group) is None:
        raise CommsError(f"No group {sigil!r}.")
    limit = args.get("limit")
    if not isinstance(limit, int) or limit <= 0:
        limit = 50
    messages = read_group_messages(root, team, group)
    if not messages:
        return f"{sigil} has no messages yet."
    recent = messages[-limit:]
    out = [f"=== {sigil} transcript ({len(recent)} of {len(messages)} message(s)) ==="]
    for msg in recent:
        urgent = " [URGENT]" if msg.get("priority") == "urgent" else ""
        out.append(f"\n--- id: {msg.get('id')} | from: {msg.get('from')}{urgent} ---")
        out.append(str(msg.get("message", "")))
    return "\n".join(out)


_HANDLERS = {
    "teammate_register": _handle_register,
    "teammate_send": _handle_send,
    "teammate_inbox": _handle_inbox,
    "teammate_ack": _handle_ack,
    "teammate_list": _handle_list,
    "teammate_whoami": _handle_whoami,
    "teammate_update": _handle_update,
    "teammate_profile": _handle_profile,
    "teammate_group": _handle_group,
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
