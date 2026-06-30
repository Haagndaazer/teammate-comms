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
import os
import re
import sys
import traceback

from .comms import (
    DELETED_MARKER,
    PROFILE_FIELDS,
    PROJECT_FIELDS,
    PROJECT_STATUS,
    REACTION_EMOJI,
    CommsError,
    aggregate_reactions,
    append_deletion,
    append_group_message,
    append_reaction,
    append_transcript,
    delete_group,
    ensure_inbox,
    file_lock,
    get_agents_dir,
    get_group_dir,
    get_groups_dir,
    get_inboxes_dir,
    get_transcript_file,
    group_read_positions,
    is_channel_alive,
    list_project_records,
    now_timestamp,
    read_agent_record,
    read_group_messages,
    read_group_meta,
    read_json_readonly,
    read_json_safe,
    read_jsonl_tail,
    read_project_record,
    read_reactions,
    read_transcript,
    remove_agent,
    remove_group_messages_from_inbox,
    remove_project_record,
    strip_member_from_groups,
    tombstone_in_group_messages,
    tombstone_in_inbox,
    validate_agent_name,
    validate_group_name,
    validate_profile_field,
    validate_project_dir,
    validate_project_field,
    validate_project_key,
    write_agent_record,
    write_group_meta,
    write_json_atomic,
    write_project_record,
)

_PRIORITIES = ("normal", "urgent")
_GROUP_ACTIONS = ("create", "delete", "join", "leave", "add", "members", "history",
                  "mute", "unmute", "reads")
# Optional post label (a decision trail axis) — distinct from a message's transport
# `kind` (dm/group) and an agent record's `type` (full/human). Default: untyped.
_POST_TYPES = ("decision", "blocker", "fyi", "chatter")
# Fixed basic emoji-reaction set (name → glyph) — single source in comms.py.
_REACTIONS = REACTION_EMOJI
# Max message length (chars). Profile fields were capped at v0.2.0 but message bodies never
# were (audit D-3); an unbounded body is stored and re-served to every poller forever. Kept
# well UNDER the dashboard's 1 MB POST-body cap so an over-long message returns this
# informative error (→ 400) rather than the opaque 413 the transport cap would give.
MAX_MESSAGE_CHARS = 64 * 1024
# Cap on _read.json (acked-message history) entries — trimmed to the most recent N on ack so
# the append-forever log can't grow without bound (audit C-3).
_READ_CAP = 1000
# react()/resolve_message() resolve a target id from the transcript newest-first. The target is
# almost always recent, so scan the last _RESOLVE_TAIL records first (read only the file's tail,
# C-1) and only full-scan the whole stream if that window is saturated and missed (an older
# target, or a log longer than the window).
_RESOLVE_TAIL = 500


def _reaction_summary(reactions_by_emoji):
    """Render a {emoji: [reactors]} dict listing the reactor NAMES per emoji
    ('👍 alice, bob; 🔥 carol') — the single shared form for the inbox AND group history (F-3:
    the inbox used to show bare counts '👍 2', so a wake said someone reacted but the inbox
    couldn't say who). Callers wrap it as '    reactions: <this>'."""
    return "; ".join(f"{_REACTIONS.get(e, e)} {', '.join(who)}" for e, who in reactions_by_emoji.items())

# Per-field descriptions reused by teammate_register and teammate_update schemas.
_PROFILE_DESCRIPTIONS = {
    "project": (
        "The project/repo you're working in. Auto-filled from the current project "
        "directory at registration — set this only to override the auto-filled value."
    ),
    "role": "Your job/role on the team (e.g. 'backend / API').",
    "personality": (
        "A persona to inhabit — concrete sensory detail, temperament, and voice cues. "
        "Never the agent's job, owned areas, or current task (those are role/authority/status). "
        "See SKILL.md for full guidance."
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


_PROJECT_DESCRIPTIONS = {
    "summary":     "One-liner shown in list_projects — keep terse.",
    "description": "Short paragraph describing the project.",
    "tech_stack":  "Primary languages/frameworks, comma-separated.",
    "repo_url":    "Optional remote URL.",
    "name":        "Human-friendly display name (defaults to the key if omitted).",
    "status":      "Project status.",
    "path":        "Local filesystem path (auto-filled from $CLAUDE_PROJECT_DIR when omitted).",
    "key":         "Project key (defaults to your normalized project label).",
}


def _project_schema_properties():
    """inputSchema 'properties' for project profile fields."""
    props = {
        name: {
            "type": "string",
            "description": f"{_PROJECT_DESCRIPTIONS[name]} Max {PROJECT_FIELDS[name]} chars, single line.",
        }
        for name in PROJECT_FIELDS
    }
    props["status"] = {
        "type": "string",
        "enum": list(PROJECT_STATUS),
        "description": _PROJECT_DESCRIPTIONS["status"],
    }
    props["path"] = {"type": "string", "description": _PROJECT_DESCRIPTIONS["path"]}
    props["key"] = {"type": "string", "description": _PROJECT_DESCRIPTIONS["key"]}
    return props

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
                "post_type": {
                    "type": "string",
                    "enum": list(_POST_TYPES),
                    "description": "Optional post label — turns the thread into a decision trail (decision | blocker | fyi | chatter). Default untyped.",
                },
                "reply_to": {
                    "type": "string",
                    "description": "Optional id of the message this replies to (a threading hint/citation; rendered flat as '↳ re <id>').",
                },
            },
            "required": ["to", "message"],
        },
    },
    {
        "name": "teammate_inbox",
        "description": "Read your own unread messages (or just the unread count). Optional 'since'/'limit' page a large inbox. Bodies of messages already shown this session are suppressed by default (pass show_all=True to re-read them).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count_only": {"type": "boolean", "description": "If true, return only the unread count."},
                "since": {"type": "string", "description": "Only show unread messages with id >= this cursor (page forward through a large inbox)."},
                "limit": {"type": "integer", "description": "Show only the most recent N unread (after any 'since' filter). ack(\"all\") then clears only what was shown."},
                "show_all": {"type": "boolean", "description": "Re-show bodies of messages already read this session (default suppresses them to avoid re-dumping context you've already seen)."},
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
                "id": {"type": "string", "description": "Message id to ack, or \"all\". Note: \"all\" with no prior teammate_inbox read this session drains the whole inbox (startup-drain); after a read it clears only what that read showed, preserving messages that arrived since."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "teammate_list",
        "description": "List registered teammates with their type and liveness. Defaults to your project only; pass all=True for a global view.",
        "inputSchema": {"type": "object", "properties": {
            "all": {
                "type": "boolean",
                "description": "Show teammates from all projects (default shows only your project). Cross-project authority owners are hidden in default view.",
            },
        }},
    },
    {
        "name": "teammate_whoami",
        "description": "Report this instance's registration state, identity, comms dir, and your own profile.",
        "inputSchema": {"type": "object", "properties": {
            "verbose": {
                "type": "boolean",
                "description": (
                    "Also include a read-only `doctor` diagnostics section: comms root, each "
                    "agent's heartbeat freshness/liveness, sub-stream file sizes, unread counts, "
                    "and any leftover lock directories. Useful when comms seem stuck."
                ),
            },
        }},
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
            "them. The full ordered thread is kept in a shared transcript — use "
            "action='history' as the canonical record (it survives inbox acks). "
            "Membership is open (anyone can join/add). Actions: create, delete "
            "(creator-only), join, leave, add (members), members (list), history (read "
            "the shared transcript; optionally filter by 'sender', 'post_type', and a "
            "'since' id cursor — the decision trail)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_GROUP_ACTIONS),
                    "description": "create | delete | join | leave | add | members | history | mute | unmute (silence/restore this group's channel wakes for you; messages still arrive) | reads (who has acked up to where)",
                },
                "group": {"type": "string", "description": "Group name (a leading '#' is optional)."},
                "members": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Member names — for 'create' (initial members) and 'add'.",
                },
                "limit": {"type": "integer", "description": "For 'history': max messages to return (default 50). Applied AFTER the sender/post_type/since filters, so it's the N most-recent MATCHING messages (a narrow filter can still reach far back)."},
                "sender": {"type": "string", "description": "For 'history': only show messages from this teammate."},
                "post_type": {"type": "string", "enum": list(_POST_TYPES), "description": "For 'history': only show posts of this type (the decision trail)."},
                "since": {"type": "string", "description": "For 'history': only show messages with id >= this cursor (e.g. 'everything since I last checked')."},
                "reply_to": {"type": "string", "description": "For 'history': only show replies to this message id (a thread)."},
            },
            "required": ["action", "group"],
        },
    },
    {
        "name": "teammate_react",
        "description": (
            "React to a message (by its id) with a basic emoji — thumbsup, rofl, smile, "
            "cry, 100, or fire — a lightweight acknowledgement without sending a message. "
            "It wakes ONLY the author of the reacted-to message (never the group, never on "
            "remove); everyone sees it in teammate_inbox / teammate_group history / the "
            "dashboard. Pass remove=true to take your reaction back."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to_message": {"type": "string", "description": "Id of the message to react to (shown in inbox/history)."},
                "emoji": {"type": "string", "enum": list(_REACTIONS), "description": "thumbsup | rofl | smile | cry | 100 | fire"},
                "remove": {"type": "boolean", "description": "Remove your reaction instead of adding it."},
            },
            "required": ["to_message", "emoji"],
        },
    },
    {
        "name": "teammate_reincarnate",
        "description": (
            "Spawn a NEW Claude Code teammate in a new terminal window, in a given "
            "project directory, as a named teammate (often a known offline one). It "
            "auto-registers + arms its channel and becomes reachable on the shared comms. "
            "GATED: disabled unless TEAMMATE_REINCARNATE_ENABLED is truthy (it launches OS "
            "processes). Confirms LAUNCH, not registration — verify with teammate_list a "
            "few seconds later. The spawned window may need one human approval to arm the "
            "custom channel."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Teammate name to (re)spawn."},
                "project_dir": {"type": "string", "description": "Existing directory to launch in (becomes the child's cwd AND CLAUDE_PROJECT_DIR)."},
                "prompt": {"type": "string", "description": "Optional first instruction (defaults to an inbox-drain bootstrap)."},
                "team": {"type": "string", "description": "Optional team (namespaced inboxes)."},
                "comms_dir": {"type": "string", "description": "Optional comms-root override (default: inherit the shared global root)."},
            },
            "required": ["agent", "project_dir"],
        },
    },
    {
        "name": "teammate_dashboard",
        "description": (
            "Open a local web console (a Slack-style window in the browser) showing all "
            "teammate messaging — group chats AND direct messages — plus a live roster, "
            "and register the human operator as a first-class teammate so agents can "
            "teammate_send to them and invite them to groups exactly like any teammate. "
            "Runs a localhost-only, token-secured HTTP server inside this instance's "
            "process and returns the URL; the console is up while this instance runs. "
            "The human is registered under 'human_name' (default $TEAMMATE_HUMAN_NAME, "
            "else 'human')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer", "description": "Preferred port (default 7842; scans onward if taken)."},
                "open_browser": {"type": "boolean", "description": "Open the page in a browser (default true)."},
                "human_name": {
                    "type": "string",
                    "description": "Name the human appears as to the team (default $TEAMMATE_HUMAN_NAME, else 'human').",
                },
            },
        },
    },
    {
        "name": "teammate_delete",
        "description": (
            "Delete a message OR remove a teammate. Provide EXACTLY ONE of 'message' or "
            "'teammate'. message=<id>: tombstones that message everywhere it was written "
            "(a group post in the shared transcript AND every member's inbox copy; a DM in "
            "the recipient's inbox) — the body becomes a deleted-marker but its id/author/"
            "reply threads are kept, so citations still resolve. Allowed for the message's "
            "author (or the operator via the dashboard). teammate=<name>: hard-removes an "
            "OFFLINE teammate (registry record + inbox + group memberships); their past "
            "messages stay attributed. A live teammate or yourself can't be removed. "
            "Deletions reflect in the dashboard live."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Id of the message to delete (tombstone). Mutually exclusive with 'teammate'."},
                "teammate": {"type": "string", "description": "Name of the OFFLINE teammate to remove. Mutually exclusive with 'message'."},
            },
        },
    },
    {
        "name": "project_register",
        "description": (
            "Define/update the profile for a project. By convention only register or edit "
            "the project matching your own working directory, unless the user asks you to "
            "document another."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _project_schema_properties(),
        },
    },
    {
        "name": "list_projects",
        "description": (
            "List all registered project profiles. Returns exactly three things per "
            "project: display name, live teammate roster, and summary. Use project_profile "
            "for full details."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "project_profile",
        "description": (
            "Read full details for a project profile including its live teammate roster "
            "(agent name, role, status, liveness). Omit 'key' to read your own project's profile."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Project key (defaults to your project)."},
            },
        },
    },
    {
        "name": "project_delete",
        "description": (
            "Remove a project profile. By convention only delete the profile for your own "
            "project, unless the user asks otherwise."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Project key to delete (defaults to your project)."},
            },
        },
    },
    {
        "name": "teammate_set_avatar",
        "description": (
            "Set or clear a profile avatar image for a registered teammate. "
            "Requires Pillow (install teammate-comms[images]). "
            "Provide 'path' (local filesystem path to an image) OR 'image_base64' (base64-encoded bytes). "
            "Supported formats: PNG, JPEG, GIF, WEBP, and any format Pillow can open. "
            "Images are automatically resized to 256×256, centred on a black canvas. "
            "Pass clear=true to remove an existing avatar. "
            "On success the ASCII preview strip is returned so you can confirm the result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Name of the agent whose avatar to set. Defaults to yourself.",
                },
                "path": {
                    "type": "string",
                    "description": "Filesystem path to the source image (PNG, JPEG, etc.).",
                },
                "image_base64": {
                    "type": "string",
                    "description": "Base64-encoded image bytes (alternative to path).",
                },
                "clear": {
                    "type": "boolean",
                    "description": "Remove the avatar instead of setting one (default false).",
                },
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


def _clean_message(message):
    """Return the trimmed message body, or raise CommsError (single chokepoint for both the
    MCP teammate_send and the dashboard, so the length cap covers both)."""
    if not isinstance(message, str) or not message.strip():
        raise CommsError("'message' is required and must be a non-empty string.")
    cleaned = message.strip()
    if len(cleaned) > MAX_MESSAGE_CHARS:
        raise CommsError(f"'message' exceeds the {MAX_MESSAGE_CHARS}-character limit "
                         f"({len(cleaned)} chars).")
    return cleaned


def _clean_priority(priority):
    priority = priority or "normal"
    if priority not in _PRIORITIES:
        raise CommsError(f"'priority' must be one of {list(_PRIORITIES)}.")
    return priority


def _clean_post_type(value):
    """Validate the optional post label. None/'' → None (untyped; store no key)."""
    if value is None or value == "":
        return None
    if value not in _POST_TYPES:
        raise CommsError(f"'post_type' must be one of {list(_POST_TYPES)}.")
    return value


_MENTION_RE = re.compile(r"@([a-zA-Z0-9][a-zA-Z0-9._-]*)")


def _parse_mentions(content, members):
    """Extract @name tokens from the body, keep only actual group members (no phantom
    mentions of non-members), de-duped + order-preserved."""
    members = set(members or [])
    seen, out = set(), []
    for name in _MENTION_RE.findall(content or ""):
        if name in members and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def send_dm(root, team, sender, to, message, priority="normal", post_type=None, reply_to=None):
    """Core: deliver a 1:1 DM as ``sender``; return a dict for the wrapper to format.

    Sender-explicit (not derived from any MCP identity) so the dashboard can post AS
    the human. Writes the recipient's inbox (the delivery guarantee, which wakes a
    live recipient via their own channel watcher), THEN tees the global transcript
    (best-effort, LAST — observability never precedes or delays delivery).
    """
    validate_agent_name(to)
    if to == sender:
        raise CommsError(
            "Cannot send to yourself. teammate_send targets another teammate; "
            "use teammate_inbox to read your own messages."
        )
    content = _clean_message(message)
    priority = _clean_priority(priority)
    pt = _clean_post_type(post_type)

    inboxes_dir = get_inboxes_dir(root, team)
    ensure_inbox(inboxes_dir, to)
    unread_file = inboxes_dir / f"{to}_unread.json"

    record = {"id": now_timestamp(), "from": sender, "priority": priority, "message": content}
    if pt:
        record["post_type"] = pt  # additive: flows to inbox + NDJSON transcript
    if isinstance(reply_to, str) and reply_to.strip():
        record["reply_to"] = reply_to.strip()  # unvalidated hint (a citation)
    with file_lock(unread_file):
        messages = read_json_safe(unread_file)
        messages.append(record)
        write_json_atomic(unread_file, messages)

    to_record = read_agent_record(root, team, to)
    to_type = to_record.get("type") if to_record else None
    live = bool(to_record and to_type == "full" and is_channel_alive(to_record))

    append_transcript(root, team, {**record, "to": to, "kind": "dm"})  # tee LAST
    return {"id": record["id"], "to": to, "to_type": to_type, "live": live}


def _handle_send(args, ctx):
    agent, team, root = _require_registered(ctx)
    to = args.get("to")
    if not isinstance(to, str) or not to.strip():
        raise CommsError("'to' is required (a teammate name, or a '#'-prefixed group name).")
    to = to.strip()

    # Group path: a '#'-prefixed recipient fans out to the group's members.
    if to.startswith("#"):
        return _send_to_group(agent, team, root, to, args)

    res = send_dm(root, team, agent, to, args.get("message"), args.get("priority", "normal"),
                  post_type=args.get("post_type"), reply_to=args.get("reply_to"))

    lines = [f"Message sent to {to} (id: {res['id']})."]
    if res["to_type"] == "full":
        if res["live"]:
            lines.append(f"{to}'s channel is live — they will be nudged automatically.")
        else:
            lines.append(
                f"WARNING: {to}'s channel is not running. The message is queued and "
                f"will be seen when they next start their instance."
            )
    elif res["to_type"] == "human":
        lines.append(f"{to} is the human operator — they'll see this in their dashboard.")
    else:
        lines.append(
            f"NOTE: no teammate named {to!r} is registered (no agent record). The message is "
            f"queued in their inbox and will be delivered if/when they register under that exact "
            f"name — a typo'd recipient queues silently. If {to} is a spawned subagent, their "
            f"lead must SendMessage-nudge them to read it."
        )
    return "\n".join(lines)


def _handle_inbox(args, ctx):
    agent, team, root = _require_registered(ctx)
    inboxes_dir = get_inboxes_dir(root, team)
    ensure_inbox(inboxes_dir, agent)
    all_unread = read_json_safe(inboxes_dir / f"{agent}_unread.json")

    if args.get("count_only"):
        return str(len(all_unread))

    # Optional windowing (C-3): 'since' (id >= cursor) then 'limit' (the most recent N). For an
    # unbounded inbox the agent can page rather than pull everything at once.
    messages = all_unread
    since = args.get("since")
    if isinstance(since, str) and since.strip():
        s = since.strip()
        messages = [m for m in messages if m.get("id", "") >= s]
    limit = args.get("limit")
    if isinstance(limit, int) and limit > 0 and len(messages) > limit:
        messages = messages[-limit:]                       # most recent N within the filter

    # last_seen accumulates every id SHOWN this session (UNION, not replace), pruned to the
    # CURRENT unread ids. Two contracts ride on this: (1) ack("all") clears only what was
    # actually shown, so a limited read can't drain unshown messages (silent loss); (2) an id
    # shown on an EARLIER page must STAY seen when a later windowed read shows a different
    # page — else it falls out of last_seen while still unread → the watcher re-counts it as
    # unseen and re-nudges a message the agent already read (the v0.4.2 contract; two
    # individually-correct changes composing into a regression). Pruning to current-unread lets
    # acked/removed ids leave naturally (bounded, exactly like the watcher's known_ids). A
    # count-only read (above) returns before here, so it never counts as a read.
    _prev_seen = ctx["identity"].get_last_seen() or set()
    _shown = {m.get("id") for m in messages}
    _unread_ids = {m.get("id") for m in all_unread}
    ctx["identity"].set_last_seen((_prev_seen | _shown) & _unread_ids)

    if not messages:
        return ("No unread messages." if not (since or limit)
                else f"No unread messages match the filter ({len(all_unread)} total unread).")

    # Body suppression: messages already shown this session don't re-dump their body.
    # _prev_seen is captured BEFORE set_last_seen above — NEVER-MISS invariant: a message
    # showing for the first time this call lands in new_msgs and always renders full.
    # Suppressed messages remain unread — ack("all") and ack(id) still work normally.
    show_all = bool(args.get("show_all"))
    new_msgs = [m for m in messages if m.get("id") not in _prev_seen]
    seen_msgs = [m for m in messages if m.get("id") in _prev_seen]

    if not show_all and not new_msgs:
        n = len(seen_msgs)
        if since or limit:
            return (f"No new messages in this window ({n} already read this session). "
                    f"Remove since/limit or pass show_all=True to re-read.")
        return (f"No new messages. {n} unread already read this session. "
                f"Pass show_all=True to re-read.")

    render_msgs = messages if show_all else new_msgs

    rx_all = aggregate_reactions(read_reactions(root, team))
    header = f"=== {len(messages)} unread message(s) for {agent}"
    if len(messages) < len(all_unread):
        header += f" (showing {len(messages)} of {len(all_unread)}; page with since/limit)"
    header += " ==="
    out = [header]
    for msg in render_msgs:
        tag = " [URGENT]" if msg.get("priority") == "urgent" else ""
        grp = msg.get("group")
        gtag = f" [👥 group: {grp}]" if grp else ""
        ptag = f" [{msg['post_type'].upper()}]" if msg.get("post_type") else ""
        mtag = " 🔔(@you)" if agent in (msg.get("mentions") or []) else ""
        out.append(f"\n--- id: {msg.get('id')} | from: {msg.get('from')}{gtag}{ptag}{tag}{mtag} ---")
        if msg.get("reply_to"):
            out.append(f"    ↳ re {msg['reply_to']}")
        out.append(str(msg.get("message", "")))
        rx = rx_all.get(msg.get("id"))
        if rx:
            out.append(f"    reactions: {_reaction_summary(rx)}")   # names, matching group history (F-3)
    if not show_all and seen_msgs:
        out.append(f"\n({len(seen_msgs)} message(s) already read this session not re-shown. "
                   f"Pass show_all=True to re-read.)")
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
            last_seen = ctx["identity"].get_last_seen()
            if last_seen is None:
                # Never read this session → clear everything (startup-drain behavior).
                acked, unread = unread, []
                result = f"Acknowledged all {len(acked)} message(s)."
            else:
                # Only ack messages the agent has actually SEEN; preserve arrivals that
                # landed after the last teammate_inbox read.
                acked = [m for m in unread if m.get("id") in last_seen]
                unread = [m for m in unread if m.get("id") not in last_seen]
                if not acked:
                    return ("No seen messages to acknowledge — new arrivals since your "
                            "last read are kept. Call teammate_inbox to read them first.")
                result = f"Acknowledged {len(acked)} seen message(s)."
                if unread:
                    result += (f" Kept {len(unread)} that arrived since your last read — "
                               f"call teammate_inbox to see them.")
            read.extend(acked)
        else:
            to_ack = next((m for m in unread if m.get("id") == msg_id), None)
            if to_ack is None:
                available = ", ".join(m.get("id", "?") for m in unread) or "(none)"
                raise CommsError(f"No unread message with id {msg_id!r}. Available ids: {available}")
            read.append(to_ack)
            unread = [m for m in unread if m.get("id") != msg_id]
            result = f"Acknowledged message {msg_id} from {to_ack.get('from')}."

        # Cap _read.json growth (C-3): it's an append-forever acked-history log. Keep the most
        # recent _READ_CAP entries — read receipts (group_read_positions reads the max acked id
        # per member) only ever care about recent positions, so trimming ancient acked history
        # is safe. Bounds the file the watcher never reads but ack rewrites each time.
        if len(read) > _READ_CAP:
            read = read[-_READ_CAP:]
        write_json_atomic(unread_file, unread)
        write_json_atomic(read_file, read)
    return result


def _handle_list(args, ctx):
    _agent, team, root = _require_registered(ctx)
    show_all = bool(args.get("all"))
    # Project-scoped filter (display-only — routing/delivery is always global).
    # Stale-project edge case: if the caller updated their working project without
    # re-registering, my_project may lag behind; pass all=True to see everyone.
    my_record = read_agent_record(root, team, _agent) or {}
    my_project = my_record.get("project") or ""

    agents_dir = get_agents_dir(root, team)
    if not agents_dir.exists():
        return "No registered teammates yet."

    rows = []
    filtered_count = 0
    for path in sorted(agents_dir.glob("*.json")):
        record = read_agent_record(root, team, path.stem)
        if not isinstance(record, dict):
            continue
        # Always show self; filter others to the caller's project when both sides are known.
        # Agents with no project set (human operator, legacy) are never filtered out.
        if not show_all and path.stem != _agent and my_project:
            agent_project = record.get("project") or ""
            if agent_project and agent_project != my_project:
                filtered_count += 1
                continue
        kind = record.get("type", "unknown")
        me = " (you)" if path.stem == _agent else ""
        if kind == "human":
            # A human has no channel — show their dashboard presence instead, marked
            # distinctly so agents KNOW it's the operator (not just another agent).
            rows.append(f"  - {path.stem}{me}: type=human 🧑 (operator), "
                        f"presence={record.get('presence', 'away')}")
        else:
            # Heartbeat-freshness only (no per-agent liveness subprocess).
            live = is_channel_alive(record, pid_check=False)
            rows.append(f"  - {path.stem}{me}: type={kind}, channel={'live' if live else 'offline'}")
        # project + status + authority always surface — the at-a-glance fields
        # (project matters most now that comms are global across projects).
        rows.append(f"      project:   {record.get('project') or '(not set)'}")
        rows.append(f"      status:    {record.get('status') or '(not set)'}")
        rows.append(f"      authority: {record.get('authority') or '(not set)'}")
        role = record.get("role")
        if role:
            rows.append(f"      role:      {role}")
        # personality intentionally excluded from list — use teammate_profile for full details
    teammates = "Registered teammates:\n" + "\n".join(rows) if rows else "No registered teammates yet."
    if filtered_count:
        teammates += (
            f"\n\n({filtered_count} teammate(s) in other projects omitted, including any "
            f"cross-project authority owners — pass all=True to see all.)"
        )

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


def _doctor_report(root, team):
    """Read-only diagnostics for teammate_whoami(verbose=True) (G-5): comms layout, per-agent
    heartbeat FRESHNESS (the same heartbeat-only signal teammate_list uses — NOT a pid probe, which
    the heartbeat-only liveness decision deliberately avoids), sub-stream file sizes, unread counts,
    and leftover lock dirs. Best-effort throughout: a vanishing/locked path under a concurrent
    writer (Windows dir races) is skipped, never raised; nothing is ever stolen or removed."""
    base = get_inboxes_dir(root, team).parent      # .../TeammateComms[/team]
    rep = {"comms_root": str(root)}
    agents = {}
    try:
        for f in sorted(get_agents_dir(root, team).glob("*.json")):
            r = read_json_readonly(f)
            if isinstance(r, dict):
                agents[r.get("name", f.stem)] = {
                    "type": r.get("type"),
                    # pid_check=False is LOAD-BEARING: the doctor walks EVERY agent, and a
                    # per-agent pid probe spawns N `tasklist` subprocesses (~5s each on a stale
                    # same-host record) — the exact storm teammate_list's heartbeat-only liveness
                    # avoids (c362e41c838f), and this tool is reached for WHEN comms feel slow.
                    "alive": bool(is_channel_alive(r, pid_check=False)) if r.get("type") == "full" else None,
                    "lastHeartbeat": r.get("lastHeartbeat"),
                }
    except OSError:
        pass
    rep["agents"] = agents
    files = {}
    for label, fname in (("transcript", "transcript.jsonl"),
                         ("reactions", "reactions.jsonl"), ("deletions", "deletions.jsonl")):
        try:
            files[label] = {"bytes": (base / fname).stat().st_size}
        except OSError:
            pass                                   # absent / unreadable → omit
    rep["files"] = files
    unread = {}
    try:
        for f in sorted(get_inboxes_dir(root, team).glob("*_unread.json")):
            rec = read_json_readonly(f)
            unread[f.name.rsplit("_", 1)[0]] = len(rec) if isinstance(rec, list) else None
    except OSError:
        pass
    rep["unread_counts"] = unread
    locks = []                                     # leftover lock DIRS — enumerate read-only, never steal
    try:
        for p in base.rglob("*.lock"):
            try:
                if p.is_dir():
                    locks.append(str(p.relative_to(base)))
            except OSError:
                continue                           # vanished mid-enum (Windows) — skip
    except OSError:
        pass
    rep["lock_dirs"] = locks
    return rep


def _handle_whoami(args, ctx):
    agent, team, root, _ = ctx["identity"].snapshot()
    verbose = bool(args.get("verbose") or args.get("doctor"))
    if agent is None:
        out = {
            "registered": False,
            "hint": "Call teammate_register(agent=\"<your-name>\") to establish identity.",
        }
        if verbose:
            out["doctor"] = {"note": "not registered — no comms root resolved yet."}
        return json.dumps(out, indent=2)
    record = read_agent_record(root, team, agent) or {}
    info = {
        "registered": True,
        "agent": agent,
        "team": team,
        "comms_root": str(root),
        "inboxes_dir": str(get_inboxes_dir(root, team)),
        "profile": {field: record.get(field) for field in PROFILE_FIELDS},
    }
    if record.get("spawned_by"):
        info["spawned_by"] = record["spawned_by"]  # F-5 provenance breadcrumb, surfaced here
    if verbose:
        info["doctor"] = _doctor_report(root, team)   # G-5
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


def _format_profile(record, name, is_self=False, root=None, team=None):
    """Render an agent record as a readable profile block (identity + profile fields)."""
    kind = record.get("type", "unknown")
    me = " (you)" if is_self else ""
    lines = [f"Profile: {name}{me}", f"  {'type:':<13}{kind}"]
    if kind == "human":
        lines.append(f"  {'presence:':<13}{record.get('presence', 'away')} (human operator)")
    else:
        live = is_channel_alive(record, pid_check=False)
        lines.append(f"  {'channel:':<13}{'live' if live else 'offline'}")
    for field in PROFILE_FIELDS:
        value = record.get(field)
        lines.append(f"  {field + ':':<13}{value if value else '(not set)'}")
    avatar_meta = record.get("avatar")
    if isinstance(avatar_meta, dict) and root is not None:
        from . import avatars as _avatars_mod
        strip = _avatars_mod.read_avatar_strip(root, team, name, fmt="txt")
        if strip:
            lines.append(f"  {'avatar:':<13}[hash: {avatar_meta.get('hash', '?')}]")
            lines.append(strip)
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
    return _format_profile(record, target, is_self=(target == agent), root=root, team=team)


def _handle_set_avatar(args, ctx):
    agent, team, root = _require_registered(ctx)
    target = args.get("agent")
    if target is not None:
        validate_agent_name(target)
    else:
        target = agent
    clear = bool(args.get("clear", False))
    path = args.get("path")
    image_base64 = args.get("image_base64")
    from . import avatars as _avatars_mod
    ascii_strip = _avatars_mod.ingest_avatar(
        root, team, target,
        path=path,
        image_base64=image_base64,
        clear=clear,
    )
    if clear:
        return f"Avatar cleared for {target!r}."
    return f"Avatar set for {target!r}.\n{ascii_strip}"


# ── Group chat ────────────────────────────────────────────────────────────────

def _group_meta_file(root, team, group):
    return get_group_dir(root, team, group) / "meta.json"


def send_group(root, team, sender, to_sigil, message, priority="normal", post_type=None, reply_to=None):
    """Core: post to a group as ``sender``; return fan-out accounting for the wrapper.

    Sender-explicit so the dashboard can post AS the human. Writes the canonical
    group transcript first (delivery), then best-effort fan-out into each other
    member's inbox, then tees the global observability transcript LAST. Returns
    ``{id, sigil, delivered, live, deferred}`` (the counts are computed in the loop
    and are load-bearing for the wrapper's strings).
    """
    group = validate_group_name(to_sigil)  # strips '#', validates
    sigil = f"#{group}"
    meta = read_group_meta(root, team, group)
    if meta is None:
        raise CommsError(
            f"No group {sigil!r}. Create it with "
            f"teammate_group(action=\"create\", group=\"{sigil}\")."
        )
    content = _clean_message(message)
    priority = _clean_priority(priority)
    pt = _clean_post_type(post_type)

    # Open membership: posting auto-joins the sender (re-read under lock to add).
    members = list(meta.get("members", []))
    if sender not in members:
        with file_lock(_group_meta_file(root, team, group)):
            meta = read_group_meta(root, team, group) or meta
            members = list(meta.get("members", []))
            if sender not in members:
                members.append(sender)
                meta["members"] = members
                write_group_meta(root, team, group, meta)

    record = {"id": now_timestamp(), "from": sender, "group": sigil,
              "priority": priority, "message": content}
    if pt:
        record["post_type"] = pt  # additive: flows to inbox + group transcript + NDJSON
    # @mentions: shared list on the one record; each member's own watcher checks if IT is
    # mentioned (no per-member records). Only real members (no phantoms).
    mentions = _parse_mentions(content, members)
    if mentions:
        record["mentions"] = mentions
    if isinstance(reply_to, str) and reply_to.strip():
        record["reply_to"] = reply_to.strip()  # unvalidated hint (a citation)

    # Group transcript is the canonical, ordered source of truth — write it first.
    append_group_message(root, team, group, record)

    # Best-effort fan-out into each other member's inbox. A locked/failed inbox is
    # NON-FATAL: the transcript is authoritative and the member catches up via history.
    inboxes_dir = get_inboxes_dir(root, team)
    delivered, live, deferred = [], 0, []
    for member in members:
        if member == sender:
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

    append_transcript(root, team, {**record, "kind": "group"})  # tee LAST (firehose)
    return {"id": record["id"], "sigil": sigil,
            "delivered": delivered, "live": live, "deferred": deferred}


def _send_to_group(agent, team, root, to_sigil, args):
    """Thin wrapper: format send_group()'s accounting into the human-readable summary."""
    res = send_group(root, team, agent, to_sigil, args.get("message"), args.get("priority", "normal"),
                     post_type=args.get("post_type"), reply_to=args.get("reply_to"))
    delivered, live, deferred = res["delivered"], res["live"], res["deferred"]
    lines = [f"Posted to {res['sigil']} (id: {res['id']})."]
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
        out = (f"Created group {sigil} with member(s): {', '.join(members)}. "
               f"Post with teammate_send(to=\"{sigil}\").")
        # F-2: open membership is intentional, so an unregistered member is NOT an error — but
        # warn so a typo'd name doesn't silently join a phantom (it just won't receive posts until
        # it registers under that exact name).
        unknown = [m for m in members if m != agent and read_agent_record(root, team, m) is None]
        if unknown:
            out += (f"\nNOTE: no agent record yet for {', '.join(unknown)} — they'll receive "
                    f"posts in their inbox if/when they register under those exact names.")
        return out

    if action == "delete":
        meta = read_group_meta(root, team, group)
        if meta is None:
            raise CommsError(f"No group {sigil!r}.")
        creator = meta.get("creator")
        members = meta.get("members", [])
        # Creator-only, OR any member if the creator is no longer a member (orphan).
        if not (agent == creator or (creator not in members and agent in members)):
            raise CommsError(f"Only the creator ({creator}) can delete {sigil}.")
        # Purge the fan-out copies that linger in member inboxes BEFORE the rmtree. (The old
        # bug: delete removed only the group dir, leaving those copies + the transcript behind,
        # so a "deleted" group's messages still showed.) Purge by the GROUP PREDICATE
        # (group == sigil), NOT a pre-snapshot id-set, so a copy that lands between the snapshot
        # and the purge is still caught (A-5 window-shrink; the residual post-rmtree-recreate
        # orphan is accepted as eventually-consistent — see remove_group_messages_from_inbox).
        # Order: clean inboxes -> rmtree -> emit the deletion event LAST, so a best-effort
        # partial rmtree can't desync the dashboard.
        msg_count = len([m for m in read_group_messages(root, team, group)
                         if isinstance(m, dict) and m.get("id")])
        for member in members:
            remove_group_messages_from_inbox(root, team, member, sigil)
        delete_group(root, team, group)
        # best-effort (block=False): the group dir is already gone, so emitting the event
        # LAST is the recorded v0.7.0 ordering (a partial rmtree can't desync the
        # dashboard) and a blocking raise here would lose the event permanently on retry
        # (re-entry hits "No group"). A fresh dashboard load omits the absent group anyway.
        append_deletion(root, team, {"target": sigil,  # id stamped under the lock inside append_deletion
                                     "kind": "group", "by": agent, "op": "delete"}, block=False)
        return (f"Deleted group {sigil} (purged ~{msg_count} message(s) from "
                f"member inboxes).")

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

    if action in ("mute", "unmute"):
        # Per-member preference on the caller's OWN agent record (the watcher already
        # reads that record). Muting silences the channel WAKE for the group; messages
        # still land in your inbox + the transcript. Never affects 1:1 DMs.
        rec = read_agent_record(root, team, agent) or {}
        muted = set(rec.get("muted_groups", []))
        if action == "mute":
            muted.add(sigil)
        else:
            muted.discard(sigil)
        if not write_agent_record(root, team, agent, timeout=5, muted_groups=sorted(muted)):
            raise CommsError("Could not update mute settings (registry busy). Try again.")
        verb = "Muted" if action == "mute" else "Unmuted"
        return (f"{verb} {sigil}. " + (
            f"Its messages still land in your inbox (no channel wake). Now muted: "
            f"{', '.join(sorted(muted)) or '(none)'}." if action == "mute"
            else f"You'll be woken by {sigil} again. Still muted: "
                 f"{', '.join(sorted(muted)) or '(none)'}."))

    if action == "reads":
        # Read-only read receipts (ack/seen position per member, inferred from _read.json).
        meta = read_group_meta(root, team, group)
        if meta is None:
            raise CommsError(f"No group {sigil!r}.")
        members = meta.get("members", [])
        positions = group_read_positions(root, team, group, members)
        lines = [f"{sigil} read positions (furthest-acked group message per member):"]
        for m in members:
            lines.append(f"  - {m}: {positions.get(m) or '(none acked)'}")
        return "\n".join(lines)

    # action == "history"
    if read_group_meta(root, team, group) is None:
        raise CommsError(f"No group {sigil!r}.")
    limit = args.get("limit")
    if not isinstance(limit, int) or limit <= 0:
        limit = 50
    messages = read_group_messages(root, team, group)
    # Filters compose, applied BEFORE the last-`limit` slice (so limit counts post-filter):
    # sender, post_type, and a `since` id cursor (lexical >=, reusing read_transcript's
    # scheme — ids are zero-padded TIMESTAMP_FMT, so string compare is chronological).
    filt = []
    sender = args.get("sender")
    if sender is not None:
        validate_agent_name(sender)
        messages = [m for m in messages if m.get("from") == sender]
        filt.append(f"from {sender}")
    post_type = _clean_post_type(args.get("post_type"))
    if post_type:
        messages = [m for m in messages if m.get("post_type") == post_type]
        filt.append(f"type={post_type}")
    since = args.get("since")
    if isinstance(since, str) and since:
        messages = [m for m in messages if m.get("id", "") >= since]
        filt.append(f"since {since}")
    reply_to = args.get("reply_to")
    if isinstance(reply_to, str) and reply_to:
        messages = [m for m in messages if m.get("reply_to") == reply_to]
        filt.append(f"replies to {reply_to}")
    by = (" [" + ", ".join(filt) + "]") if filt else ""
    if not messages:
        return f"{sigil} has no messages{by} yet."
    total = len(messages)
    recent = messages[-limit:]
    rx_all = aggregate_reactions(read_reactions(root, team))
    out = [f"=== {sigil} transcript{by} ({len(recent)} of {total} message(s)) ==="]
    for msg in recent:
        urgent = " [URGENT]" if msg.get("priority") == "urgent" else ""
        ptag = f" [{msg['post_type'].upper()}]" if msg.get("post_type") else ""
        out.append(f"\n--- id: {msg.get('id')} | from: {msg.get('from')}{ptag}{urgent} ---")
        if msg.get("reply_to"):
            out.append(f"    ↳ re {msg['reply_to']}")
        out.append(str(msg.get("message", "")))
        rx = rx_all.get(msg.get("id"))
        if rx:
            out.append(f"    reactions: {_reaction_summary(rx)}")   # shared helper (F-3)
    return "\n".join(out)


def _scan_transcript_for_id(root, team, target_id):
    """Find a transcript record by id, newest-first, without parsing the whole file in the
    common (recent-target) case.

    Reads the last ``_RESOLVE_TAIL`` records via ``read_jsonl_tail`` and scans them recent-first;
    falls back to a full reverse stream ONLY when that window is saturated (``>= _RESOLVE_TAIL``
    records) and the target wasn't in it — i.e. the target predates the window. A window that
    held the whole log (``< _RESOLVE_TAIL`` records) IS the full scan, so a miss there is final.
    Returns the matching record dict or None. Honors TEAMMATE_TRANSCRIPT=0 implicitly: an
    empty/absent transcript yields [] from both reads → None (callers keep their own fallbacks).
    """
    path = get_transcript_file(root, team)
    tail = read_jsonl_tail(path, _RESOLVE_TAIL)
    for rec in reversed(tail):
        if rec.get("id") == target_id:
            return rec
    if len(tail) < _RESOLVE_TAIL:
        return None  # the window held the entire log — no older records exist to scan
    for rec in reversed(read_transcript(root, team, limit=None)):  # older than the window
        if rec.get("id") == target_id:
            return rec
    return None


def react(root, team, reactor, target, emoji, remove=False):
    """Core: append a reaction add/remove event as ``reactor`` (sender-explicit so the
    dashboard reacts AS the human).

    Stamps ``target_from`` (the reacted-to message's author, resolved from the durable
    transcript) so the watcher can wake ONLY that author. If the author can't be resolved
    (target not in the transcript, or TEAMMATE_TRANSCRIPT=0) the reaction still records but
    won't wake anyone.
    """
    if not isinstance(target, str) or not target.strip():
        raise CommsError("'to_message' is required (the id of the message to react to).")
    if emoji not in _REACTIONS:
        raise CommsError(f"'emoji' must be one of {list(_REACTIONS)}.")
    target = target.strip()
    _hit = _scan_transcript_for_id(root, team, target)  # tail-first; full-scan fallback (C-1)
    target_from = _hit.get("from") if _hit else None
    # id is stamped inside append_reaction under the lock (file order == id order — see its
    # docstring); we read it back off `record` for the return value.
    record = {"target": target, "from": reactor,
              "emoji": emoji, "op": "remove" if remove else "add"}
    if target_from:
        record["target_from"] = target_from
    append_reaction(root, team, record)
    return record


def _handle_react(args, ctx):
    agent, team, root = _require_registered(ctx)
    remove = bool(args.get("remove"))
    emoji = args.get("emoji")
    rec = react(root, team, agent, args.get("to_message"), emoji, remove)
    glyph = _REACTIONS[emoji]
    if remove:
        return f"Removed your {glyph} ({emoji}) from message {rec['target']}."
    return f"Reacted {glyph} ({emoji}) to message {rec['target']}."


def resolve_message(root, team, msg_id):
    """Resolve a message id → ``{from, kind, group?, to?}`` or None if not found.

    Primary source is the global transcript (it carries from/kind/to/group for every DM
    and group post). Falls back to scanning group transcripts then inboxes when the
    firehose is disabled (TEAMMATE_TRANSCRIPT=0) or rotated. Returns None on a clean miss.
    """
    rec = _scan_transcript_for_id(root, team, msg_id)  # tail-first; full-scan fallback (C-1)
    if rec is not None:
        kind = rec.get("kind") or ("group" if rec.get("group") else "dm")
        out = {"from": rec.get("from"), "kind": kind}
        if rec.get("group"):
            out["group"] = rec["group"]
        if rec.get("to"):
            out["to"] = rec["to"]
        return out
    # Fallback: group transcripts (canonical for group posts).
    groups_dir = get_groups_dir(root, team)
    if groups_dir.exists():
        for gdir in sorted(d for d in groups_dir.iterdir() if d.is_dir()):
            for rec in read_group_messages(root, team, gdir.name):
                if isinstance(rec, dict) and rec.get("id") == msg_id:
                    return {"from": rec.get("from"), "kind": "group",
                            "group": rec.get("group") or f"#{gdir.name}"}
    # Fallback: inboxes (DMs). The record has 'from'; the owning file name IS the
    # recipient. Non-destructive read (no lock held during resolution).
    inboxes_dir = get_inboxes_dir(root, team)
    if inboxes_dir.exists():
        for f in (sorted(inboxes_dir.glob("*_unread.json"))
                  + sorted(inboxes_dir.glob("*_read.json"))):
            for rec in (read_json_readonly(f) or []):
                if isinstance(rec, dict) and rec.get("id") == msg_id:
                    if rec.get("group"):
                        return {"from": rec.get("from"), "kind": "group", "group": rec["group"]}
                    owner = f.name.rsplit("_", 1)[0]  # strip _unread.json / _read.json
                    return {"from": rec.get("from"), "kind": "dm", "to": owner}
    return None


def delete_message(root, team, caller, msg_id, is_operator=False):
    """Tombstone a message everywhere it was written (author-or-operator).

    Returns a summary string; raises CommsError if not found or not permitted. The
    durable tombstone lands in the group transcript + member inboxes (group) or the
    recipient inbox (DM); a deletion event is appended for the dashboard sub-stream.
    """
    if not isinstance(msg_id, str) or not msg_id.strip():
        raise CommsError("'message' is required (the id of the message to delete).")
    msg_id = msg_id.strip()
    info = resolve_message(root, team, msg_id)
    if info is None:
        raise CommsError(f"No message with id {msg_id!r} found.")
    author = info.get("from")
    if not is_operator and caller != author:
        raise CommsError(f"Only the author ({author}) or the operator can delete that message.")
    if info.get("kind") == "group":
        group = validate_group_name(info.get("group") or "")
        tombstone_in_group_messages(root, team, group, msg_id, caller)
        meta = read_group_meta(root, team, group)
        for member in (meta.get("members", []) if meta else []):
            tombstone_in_inbox(root, team, member, msg_id, caller)
        where = f"#{group}"
    else:
        to = info.get("to")
        if to:
            tombstone_in_inbox(root, team, to, msg_id, caller)
        if author:
            tombstone_in_inbox(root, team, author, msg_id, caller)  # self-copy safety
        where = f"DM to {to}" if to else "DM"
    append_deletion(root, team, {"target": msg_id,  # id stamped under the lock inside append_deletion
                                 "kind": "message", "by": caller, "op": "delete"})
    return f"Deleted message {msg_id} ({where}) — tombstoned everywhere it appeared."


def remove_teammate(root, team, caller, name, is_operator=False):
    """Hard-remove an OFFLINE teammate: registry record + inbox files + group
    memberships. Their authored messages stay attributed. Raises CommsError on a guard
    violation (self, the human operator, a missing name, or a LIVE teammate).
    """
    validate_agent_name(name)
    if name == caller:
        raise CommsError("You can't remove yourself.")
    record = read_agent_record(root, team, name)
    if record is None:
        raise CommsError(f"No teammate named {name!r} is registered.")
    if record.get("type") == "human":
        raise CommsError(f"{name!r} is the human operator — not removable via teammate_delete.")
    if is_channel_alive(record, pid_check=False):
        raise CommsError(
            f"{name!r} is live — ask them to exit (or wait for their heartbeat to go "
            f"stale) before removing; a live teammate's heartbeat would just re-create "
            f"the record."
        )
    removed_from = strip_member_from_groups(root, team, name)
    remove_agent(root, team, name)
    # best-effort (block=False): the record is already unlinked, so a blocking raise here
    # would lose the event permanently on retry (re-entry hits "No teammate named …"). A
    # fresh dashboard load omits the absent teammate; emit-event-LAST stays consistent.
    append_deletion(root, team, {"target": "@" + name,  # id stamped under the lock inside append_deletion
                                 "kind": "teammate", "by": caller, "op": "delete"}, block=False)
    extra = (f" Removed from group(s): {', '.join('#' + g for g in removed_from)}."
             if removed_from else "")
    return f"Removed teammate {name} (registry + inbox).{extra}"


def _handle_delete(args, ctx):
    agent, team, root = _require_registered(ctx)
    msg = args.get("message")
    who = args.get("teammate")
    has_msg = bool(isinstance(msg, str) and msg.strip())
    has_who = bool(isinstance(who, str) and who.strip())
    if has_msg == has_who:  # neither, or both
        raise CommsError("Provide exactly one of 'message' or 'teammate'.")
    if has_msg:
        return delete_message(root, team, agent, msg, is_operator=False)
    return remove_teammate(root, team, agent, who.strip(), is_operator=False)


def _reincarnate_enabled():
    v = os.environ.get("TEAMMATE_REINCARNATE_ENABLED")
    return bool(v) and v.strip().lower() not in ("", "0", "false", "no", "off")


def _handle_reincarnate(args, ctx):
    # Gate first (cheap) — opt-in only; spawning OS processes from a tool is high-power.
    if not _reincarnate_enabled():
        raise CommsError(
            "teammate_reincarnate is disabled. Set TEAMMATE_REINCARNATE_ENABLED=1 in the "
            "server's environment to enable it (it launches a new OS terminal + Claude "
            "instance)."
        )
    agent, team, root = _require_registered(ctx)
    target = args.get("agent")
    validate_agent_name(target)
    project_dir = validate_project_dir(args.get("project_dir"))
    # Best-effort live-name collision guard (TOCTOU — the child's own auto-register only
    # WARNS, server.py): refuse only if the name is already a LIVE channel. Reincarnating
    # an OFFLINE name is the whole point.
    existing = read_agent_record(root, team, target)
    if existing and is_channel_alive(existing):
        raise CommsError(
            f"{target!r} is already live (pid={existing.get('pid')}, "
            f"host={existing.get('host')}). Reincarnate is for OFFLINE teammates."
        )
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = (f"You are {target}. Call teammate_inbox to drain any queued messages, "
                  f"then await instructions.")
    team_arg = (args.get("team") or "").strip() or team
    comms_dir = args.get("comms_dir")

    from . import spawn
    argv = spawn.build_claude_command(prompt)
    env = spawn.build_child_env(os.environ, target, str(project_dir), team_arg, comms_dir,
                                spawned_by=agent)  # provenance breadcrumb (F-5)
    try:
        spawn.spawn_in_terminal(argv, project_dir, env)
    except FileNotFoundError as e:
        raise CommsError(f"Could not launch a terminal/claude: {e}")
    except OSError as e:
        raise CommsError(f"Spawn failed: {e}")
    return (
        f"Launched a new terminal for teammate {target!r} in {project_dir}.\n"
        f"This confirms LAUNCH, not registration. Expect it to auto-register and arm its channel "
        f"within ~10-20s (it will register with spawned_by={agent!r}). If the new window shows a "
        f"channel-load/trust prompt it won't register until that's approved — and in a headless / "
        f"no-click context it may never register, which looks identical to success here. Verify "
        f"with teammate_list: {target} appears once it's live; if it hasn't after ~30s, check the "
        f"new window."
    )


def _handle_dashboard(args, ctx):
    _agent, team, root = _require_registered(ctx)
    port = args.get("port")
    if not isinstance(port, int) or port <= 0:
        port = 7842
    open_browser = args.get("open_browser")
    open_browser = True if open_browser is None else bool(open_browser)
    human_name = (args.get("human_name") or os.environ.get("TEAMMATE_HUMAN_NAME") or "human").strip()
    validate_agent_name(human_name)
    # Import lazily so the HTTP server module is only loaded when the tool is used.
    from . import dashboard
    info = dashboard.start_dashboard(root, team, human_name, port=port, open_browser=open_browser)
    return (
        f"Dashboard {info['status']} at {info['url']}\n"
        f"You are '{human_name}' to the team — teammates can teammate_send to you and "
        f"invite you to groups like any teammate. The console stays up while this "
        f"instance runs."
    )


def _resolve_caller_project_key(agent, team, root):
    """Return the normalized project key for the calling agent, or raise CommsError."""
    record = read_agent_record(root, team, agent)
    raw = record.get("project") if isinstance(record, dict) else None
    if not raw:
        raise CommsError(
            "No project key: pass 'key' explicitly or register with a project field set."
        )
    return validate_project_key(raw)


def _derive_project_roster(root, team, project_key):
    """Scan all agent records; return those whose normalized project == project_key, humans excluded."""
    agents_dir = get_agents_dir(root, team)
    roster = []
    if not agents_dir.exists():
        return roster
    for path in sorted(agents_dir.glob("*.json")):
        rec = read_agent_record(root, team, path.stem)
        if not isinstance(rec, dict):
            continue
        if rec.get("type") == "human":
            continue
        raw_proj = rec.get("project") or ""
        try:
            agent_key = validate_project_key(raw_proj)
        except CommsError:
            continue
        if agent_key == project_key:
            roster.append(rec)
    return roster


def _handle_project_register(args, ctx):
    agent, team, root = _require_registered(ctx)
    raw_key = args.get("key")
    if raw_key is not None:
        key = validate_project_key(raw_key)
    else:
        key = _resolve_caller_project_key(agent, team, root)

    validated = {}
    for fname in PROJECT_FIELDS:
        if fname in args:
            validated[fname] = validate_project_field(fname, args[fname])
    if "status" in args:
        validated["status"] = validate_project_field("status", args["status"])

    # path: uncapped, whitespace-collapse only; auto-fill from env if omitted and record has none
    path_val = args.get("path")
    if path_val is not None:
        if not isinstance(path_val, str):
            raise CommsError("'path' must be a string.")
        validated["path"] = " ".join(path_val.split())  # whitespace-collapse only, no cap
    else:
        existing = read_project_record(root, team, key)
        if not (existing and existing.get("path")):
            env_path = os.environ.get("CLAUDE_PROJECT_DIR", "")
            if env_path.strip():
                validated["path"] = " ".join(env_path.strip().split())

    record = write_project_record(
        root, team, key, created_by=agent, updated_by=agent, **validated
    )
    display_name = record.get("name") or key
    verb = "Registered" if record.get("created_at") == record.get("updated_at") else "Updated"
    return (
        f"{verb} project profile for {key!r} (display name: {display_name!r}).\n"
        f"Use project_profile to read full details; list_projects to see all projects."
    )


def _handle_list_projects(args, ctx):
    agent, team, root = _require_registered(ctx)
    records = list_project_records(root, team)

    # Collect all agent project labels (normalized) — for undocumented + near-miss detection
    agents_dir = get_agents_dir(root, team)
    agent_labels = []  # list of (agent_name, raw_project, normalized_key_or_None)
    if agents_dir.exists():
        for path in sorted(agents_dir.glob("*.json")):
            rec = read_agent_record(root, team, path.stem)
            if not isinstance(rec, dict) or rec.get("type") == "human":
                continue
            raw = rec.get("project") or ""
            if not raw:
                continue
            try:
                norm = validate_project_key(raw)
            except CommsError:
                norm = None
            agent_labels.append((path.stem, raw, norm))

    registered_keys = {r["key"] for r in records if r.get("key")}

    if not records:
        out = ["No registered project profiles yet."]
    else:
        out = []
        for rec in records:
            key = rec["key"]
            display_name = rec.get("name") or key
            summary = rec.get("summary") or "(no summary)"
            roster = _derive_project_roster(root, team, key)
            roster_names = [r.get("name") or r.get("agent") or "?" for r in roster
                            if isinstance(r, dict)]
            members_str = ", ".join(roster_names) if roster_names else "(none)"
            out.append(f"  {display_name} [{key}]")
            out.append(f"    teammates: {members_str}")
            out.append(f"    summary:   {summary}")

    # Aggregate: undocumented project labels (agents carry a label with no profile)
    undocumented = sorted({norm for _, _, norm in agent_labels
                           if norm and norm not in registered_keys})
    # Near-miss: agents whose raw project differs from the stored key but normalizes to it
    near_misses = []
    for aname, raw, norm in agent_labels:
        if norm and norm in registered_keys and raw != norm:
            near_misses.append(f"    {aname}: stored as {raw!r}, normalizes to {norm!r}")
    # Unparseable: agents whose project label contains forbidden chars — cannot be grouped
    unparseable = [(aname, raw) for aname, raw, norm in agent_labels if norm is None]

    if records:
        header = f"Registered projects ({len(records)}):\n" + "\n".join(out)
    else:
        header = out[0]

    aggregate = []
    if undocumented:
        aggregate.append(
            "Undocumented project labels (agents active, no profile yet):\n"
            + "\n".join(f"  {k}" for k in undocumented)
        )
    if near_misses:
        aggregate.append(
            "Near-miss agents (project field differs from canonical key — will still "
            "group correctly after normalization):\n" + "\n".join(near_misses)
        )
    if unparseable:
        aggregate.append(
            "Agents with an unparseable project label (cannot be grouped):\n"
            + "\n".join(f"  {aname}: {raw!r}" for aname, raw in unparseable)
        )

    if aggregate:
        return header + "\n\n" + "\n\n".join(aggregate)
    return header


def _handle_project_profile(args, ctx):
    agent, team, root = _require_registered(ctx)
    raw_key = args.get("key")
    if raw_key is not None:
        key = validate_project_key(raw_key)
    else:
        key = _resolve_caller_project_key(agent, team, root)

    record = read_project_record(root, team, key)
    if record is None:
        raise CommsError(
            f"No project profile for {key!r}. "
            f"Create one with project_register(key={key!r})."
        )

    roster = _derive_project_roster(root, team, key)
    lines = [f"Project: {record.get('name') or key}  [{key}]",
             f"  status:      {record.get('status') or 'active'}"]
    for fname in PROJECT_FIELDS:
        if fname == "name":
            continue
        val = record.get(fname)
        if val:
            lines.append(f"  {fname + ':':<13}{val}")
    if record.get("path"):
        lines.append(f"  path:        {record['path']}")
    lines.append(f"  created_by:  {record.get('created_by') or '(unknown)'}")
    lines.append(f"  created_at:  {record.get('created_at') or '(unknown)'}")
    lines.append(f"  updated_by:  {record.get('updated_by') or '(unknown)'}")
    lines.append(f"  updated_at:  {record.get('updated_at') or '(unknown)'}")
    if not roster:
        lines.append("  teammates:   (none)")
    else:
        lines.append(f"  teammates ({len(roster)}):")
        for rec in roster:
            name = rec.get("name") or "?"
            live = is_channel_alive(rec, pid_check=False)
            role = rec.get("role") or "(no role)"
            status = rec.get("status") or "(no status)"
            lines.append(f"    - {name}: {'live' if live else 'offline'}, {role}, {status}")
    return "\n".join(lines)


def _handle_project_delete(args, ctx):
    agent, team, root = _require_registered(ctx)
    raw_key = args.get("key")
    if raw_key is not None:
        key = validate_project_key(raw_key)
    else:
        key = _resolve_caller_project_key(agent, team, root)

    record = read_project_record(root, team, key)
    if record is None:
        raise CommsError(f"No project profile for {key!r}.")
    remove_project_record(root, team, key)
    return f"Deleted project profile for {key!r}."


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
    "teammate_react": _handle_react,
    "teammate_reincarnate": _handle_reincarnate,
    "teammate_dashboard": _handle_dashboard,
    "teammate_delete": _handle_delete,
    "project_register": _handle_project_register,
    "list_projects": _handle_list_projects,
    "project_profile": _handle_project_profile,
    "project_delete": _handle_project_delete,
    "teammate_set_avatar": _handle_set_avatar,
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
        # A non-CommsError is a PROGRAMMING bug, not bad input — trace it to stderr (never
        # stdout: that's the JSON-RPC stream) so a refactor regression surfaces in the logs
        # instead of hiding behind a generic isError (audit A-9). The loop still survives.
        print(f"[teammate-comms] unexpected error in tool {name!r}:", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return (f"{name} failed unexpectedly: {e}", True)
