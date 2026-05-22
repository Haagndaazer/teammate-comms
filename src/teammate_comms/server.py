"""teammate-comms MCP server: a pure-stdlib JSON-RPC stdio server that is both a
tool server (the ``teammate_*`` tools) and a Claude Code channel (idle wake).

One process per full instance. The main thread reads stdin and answers
``initialize`` / ``tools/list`` / ``tools/call`` / ``ping``; a background thread
(``channel.run_watcher``) heartbeats the registry and pushes
``notifications/claude/channel`` when this agent's inbox grows. Both write stdout
under a single lock, so messages never interleave at the byte level.

Identity comes from ``$TEAMMATE_AGENT`` / ``$TEAMMATE_TEAM`` (the per-instance
differentiator). The comms root comes from ``$TEAMMATE_COMMS_DIR`` or
``$CLAUDE_PROJECT_DIR`` (see comms.resolve_comms_root). The stdio transport is
newline-delimited JSON-RPC 2.0 in BOM-free UTF-8.
"""

import argparse
import json
import os
import socket
import sys
import threading

from . import __version__
from . import channel, tools
from .comms import (
    CommsError,
    _looks_unset,
    ensure_inbox,
    get_inboxes_dir,
    is_channel_alive,
    now_timestamp,
    read_agent_record,
    resolve_comms_root,
    validate_agent_name,
    write_agent_record,
)

SERVER_NAME = "teammate-comms"

INSTRUCTIONS = (
    "This is teammate-comms. The teammate_* tools send and read agent-to-agent "
    "messages. A channel event (notifications/claude/channel) means a teammate "
    "sent you message(s) while you were idle — call teammate_inbox to read, then "
    "teammate_ack. You are a full instance: the channel wakes you, so no polling "
    "loop is needed. Reply with teammate_send."
)

_stdout_lock = threading.Lock()
_initialized = threading.Event()
_stop = threading.Event()


def log(msg):
    """Diagnostics → stderr (visible in ~/.claude/debug/<session>.txt)."""
    print(f"[teammate-comms] {msg}", file=sys.stderr, flush=True)


def send_message(obj):
    """Write one JSON-RPC message as a single BOM-free UTF-8 line + flush."""
    payload = json.dumps(obj, ensure_ascii=False)
    with _stdout_lock:
        sys.stdout.buffer.write(payload.encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()


def respond(msg_id, result):
    send_message({"jsonrpc": "2.0", "id": msg_id, "result": result})


def respond_error(msg_id, code, message):
    send_message({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def handle(msg, agent, team):
    method = msg.get("method")
    msg_id = msg.get("id")  # echoed verbatim (preserves int/str type)

    if method == "initialize":
        params = msg.get("params") or {}
        respond(msg_id, {
            # Echo the client's requested version verbatim (research-preview safe).
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {
                "experimental": {"claude/channel": {}},
                "tools": {},
            },
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        })
    elif method == "notifications/initialized":
        _initialized.set()  # gate opens: watcher may now emit (no response)
    elif method == "ping":
        respond(msg_id, {})
    elif method == "tools/list":
        respond(msg_id, {"tools": tools.TOOL_DEFINITIONS})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        text, is_error = tools.dispatch(name, arguments, agent, team)
        respond(msg_id, {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        })
    elif msg_id is not None:
        # Unknown request: report method-not-found but stay alive.
        respond_error(msg_id, -32601, f"Method not found: {method}")
    # Unknown notifications (no id), incl. notifications/cancelled: ignore.


def resolve_identity(args):
    raw_agent = args.agent if args.agent else os.environ.get("TEAMMATE_AGENT")
    agent = "" if _looks_unset(raw_agent) else raw_agent.strip()
    raw_team = args.team if args.team else os.environ.get("TEAMMATE_TEAM")
    team = None if _looks_unset(raw_team) else raw_team.strip()

    source = (
        "arg" if args.agent
        else ("env TEAMMATE_AGENT" if not _looks_unset(os.environ.get("TEAMMATE_AGENT"))
              else "none")
    )
    log(f"resolved identity: agent={agent!r} team={team!r} (from {source})")
    if not agent:
        log("ERROR: no agent identity. Set --agent or the TEAMMATE_AGENT env var "
            "before launching claude. Exiting.")
        sys.exit(1)
    try:
        validate_agent_name(agent)
    except CommsError as e:
        log(f"ERROR: {e} Exiting.")
        sys.exit(1)
    return agent, team


def main():
    parser = argparse.ArgumentParser(description="teammate-comms MCP server")
    parser.add_argument("--agent", default=None, help="Agent name (else $TEAMMATE_AGENT)")
    parser.add_argument("--team", default=None, help="Team name (else $TEAMMATE_TEAM)")
    args = parser.parse_args()

    agent, team = resolve_identity(args)
    hostname = socket.gethostname()

    # Resolve the comms root up front — without it the channel cannot work.
    try:
        root, root_source = resolve_comms_root()
        log(f"comms root: {root} (from {root_source})")
        inboxes_dir = get_inboxes_dir(team)
        ensure_inbox(inboxes_dir, agent)
    except CommsError as e:
        log(f"ERROR: {e} Exiting.")
        sys.exit(1)
    unread_file = inboxes_dir / f"{agent}_unread.json"

    # Collision guard: loudly warn (stderr) if another live channel server on
    # this host already owns this agent name — the classic TEAMMATE_AGENT
    # misconfiguration where two instances bind the same inbox.
    existing = read_agent_record(team, agent)
    if existing and existing.get("pid") != os.getpid() and is_channel_alive(existing):
        log(f"WARNING: another live channel server (pid={existing.get('pid')}, "
            f"host={existing.get('host')}) already owns agent {agent!r}. Two "
            f"instances bound to the same agent will both nudge and fight over "
            f"the registry — check TEAMMATE_AGENT.")

    if not write_agent_record(
        team, agent, timeout=5,
        type="full", channel=True, pid=os.getpid(), host=hostname,
        startedAt=now_timestamp(), lastHeartbeat=now_timestamp(),
    ):
        log("WARNING: could not write startup registry record (lock contention); "
            "send liveness reporting may be stale until the next heartbeat.")

    watcher = threading.Thread(
        target=channel.run_watcher,
        args=(send_message, agent, team, unread_file, hostname, _initialized, _stop),
        daemon=True,
    )
    watcher.start()

    try:
        for raw in iter(sys.stdin.buffer.readline, b""):
            line = raw.decode("utf-8-sig", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            handle(msg, agent, team)
    finally:
        _stop.set()
        write_agent_record(team, agent, timeout=2, channel=False, lastHeartbeat=now_timestamp())


if __name__ == "__main__":
    main()
