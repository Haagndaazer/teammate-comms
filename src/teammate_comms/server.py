"""teammate-comms MCP server: a pure-stdlib JSON-RPC stdio server that is both a
tool server (the ``teammate_*`` tools) and a Claude Code channel (idle wake).

The server starts **identity-less**. An agent calls ``teammate_register`` once at
session start (the setup.py equivalent) to establish its name; that registers the
inbox, writes the registry record, and arms the channel watcher. Identity is NOT
baked into the MCP launch config. (As a convenience, if ``$TEAMMATE_AGENT`` is
already in the environment the server auto-registers with it at startup.)

The main thread reads stdin and answers initialize / tools/list / tools/call /
ping; a background thread (channel.run_watcher) heartbeats the registry and pushes
notifications/claude/channel once registered. Both write stdout under one lock, so
messages never interleave at the byte level. The stdio transport is
newline-delimited JSON-RPC 2.0 in BOM-free UTF-8.
"""

import json
import os
import socket
import sys
import threading
from pathlib import Path

from . import __version__, channel
from . import tools as tools_mod
from .instructions import INSTRUCTIONS
from .comms import (
    PROFILE_FIELDS,
    CommsError,
    _looks_unset,
    ensure_inbox,
    get_inboxes_dir,
    is_channel_alive,
    now_timestamp,
    read_agent_record,
    read_json_readonly,
    resolve_comms_root,
    validate_agent_name,
    validate_profile_field,
    write_agent_record,
)

SERVER_NAME = "teammate-comms"

# INSTRUCTIONS lives in instructions.py (single source of truth) so the compact-matched
# SessionStart hook (hooks/reinject-instructions.sh) can re-emit the exact same text.

_stdout_lock = threading.Lock()
_initialized = threading.Event()
_registered = threading.Event()
_stop = threading.Event()


class Identity:
    """Thread-safe holder for this instance's resolved identity + comms root."""

    def __init__(self):
        self._lock = threading.Lock()
        self.agent = None
        self.team = None
        self.root = None
        self.unread_file = None
        # Ids returned by the most recent full teammate_inbox read this session, so
        # ack("all") only clears what the agent has actually SEEN (arrivals after the
        # last read are preserved). None = never read this session (ack-all then clears
        # everything, preserving startup-drain). Cleared to None on identity change.
        self._last_seen = None

    def set(self, agent, team, root, unread_file):
        with self._lock:
            if agent != self.agent:
                self._last_seen = None  # new identity → drop the previous one's seen-ids
            self.agent, self.team, self.root, self.unread_file = agent, team, root, unread_file

    def snapshot(self):
        with self._lock:
            return (self.agent, self.team, self.root, self.unread_file)

    def set_last_seen(self, ids):
        """Record the set of message ids shown by the latest full inbox read."""
        with self._lock:
            self._last_seen = set(ids)

    def get_last_seen(self):
        """Return a copy of the last-seen id set, or None if never read this session."""
        with self._lock:
            return None if self._last_seen is None else set(self._last_seen)


_identity = Identity()


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


def _project_label(proj_dir):
    """Two-component ``parent/name`` label for ``CLAUDE_PROJECT_DIR`` so two repos that share a
    basename (e.g. two ``api`` checkouts) are distinguishable in ``teammate_list`` (F-4). Falls
    back to the bare name when there is no usable parent (a drive root, a UNC share root) rather
    than emit a leading-slash value. PRE-TRUNCATED to the ``project`` field cap so a deep path can
    never raise out of ``validate_profile_field`` and BREAK registration — an auto-fill convenience
    must not be a registration footgun."""
    p = Path(proj_dir)
    name, parent = p.name, p.parent.name
    label = f"{parent}/{name}" if (parent and name) else (name or proj_dir)
    return label[:PROFILE_FIELDS["project"]]


def register_identity(agent, team, comms_dir, profile=None):
    """Establish identity + start watching. Raises CommsError on bad input.

    Used by the teammate_register tool and by the optional env auto-register.
    ``profile`` is an optional dict of free-text profile fields (role/personality/
    status/authority) validated and written onto the registry record. Returns a
    human-readable status string.
    """
    validate_agent_name(agent)
    profile = dict(profile or {})
    # Auto-fill `project` from the current project dir (Claude Code injects
    # CLAUDE_PROJECT_DIR) unless the agent set it explicitly. With a global-by-
    # default comms root, this is how teammate_list shows who is working where.
    if "project" not in profile:
        proj_dir = os.environ.get("CLAUDE_PROJECT_DIR")
        if not _looks_unset(proj_dir):
            profile["project"] = _project_label(proj_dir.strip())
    profile_fields = {k: validate_profile_field(k, v) for k, v in profile.items()}
    # Provenance (F-5): a reincarnated child records WHO spawned it. `build_child_env` sets
    # TEAMMATE_SPAWNED_BY unconditionally (never inherited), so a present value is trustworthy.
    # This is a registry-RECORD field (NOT a profile field — it's provenance, not self-description);
    # write_agent_record's field-level merge preserves it across heartbeats, like `type`. Validated
    # as an agent name; a malformed value is dropped rather than allowed to break registration.
    spawned_by = os.environ.get("TEAMMATE_SPAWNED_BY")
    spawned_by = None if _looks_unset(spawned_by) else spawned_by.strip()
    if spawned_by:
        try:
            validate_agent_name(spawned_by)
        except CommsError:
            spawned_by = None
    root, source = resolve_comms_root(comms_dir)
    hostname = socket.gethostname()

    inboxes_dir = get_inboxes_dir(root, team)
    ensure_inbox(inboxes_dir, agent)
    unread_file = inboxes_dir / f"{agent}_unread.json"

    # If re-registering to a different agent, mark the old one offline.
    old_agent, old_team, old_root, _ = _identity.snapshot()
    if old_agent and old_agent != agent and old_root is not None:
        write_agent_record(old_root, old_team, old_agent, timeout=2,
                           channel=False, lastHeartbeat=now_timestamp())

    # Collision guard: warn (stderr) if another live channel server on this host
    # already owns this agent name (the classic same-name misconfiguration).
    existing = read_agent_record(root, team, agent)
    if existing and existing.get("pid") != os.getpid() and is_channel_alive(existing):
        log(f"WARNING: another live channel server (pid={existing.get('pid')}, "
            f"host={existing.get('host')}) already owns agent {agent!r}. Two "
            f"instances bound to the same agent will both nudge and fight over the "
            f"registry — check the name you registered.")

    write_agent_record(
        root, team, agent, timeout=5,
        type="full", channel=True, pid=os.getpid(), host=hostname,
        startedAt=now_timestamp(), lastHeartbeat=now_timestamp(),
        **profile_fields,
        **({"spawned_by": spawned_by} if spawned_by else {}),
    )

    _identity.set(agent, team, root, unread_file)
    _registered.set()

    unread = read_json_readonly(unread_file) or []
    log(f"registered: agent={agent!r} team={team!r} comms_root={root} (from {source})")
    team_str = f", team {team!r}" if team else ""

    # Echo the effective profile back so the agent is reminded who it is (incl. its
    # personality) — read the record so persisted fields from a prior registration
    # show too, not just what this call passed.
    effective = read_agent_record(root, team, agent) or {}
    set_fields = [(k, effective[k]) for k in PROFILE_FIELDS if effective.get(k)]
    if set_fields:
        profile_str = "Your profile — " + "; ".join(f"{k}: {v!r}" for k, v in set_fields) + ". "
    else:
        profile_str = (
            "No profile set — set one with teammate_update "
            "(role/personality/status/authority) so teammates know what you're doing. "
        )
    return (
        f"Registered as {agent!r}{team_str}. Comms root: {root} (from {source}). "
        f"Channel armed. {profile_str}You have {len(unread)} unread message(s) — call "
        f"teammate_inbox to read them."
    )


def handle(msg, ctx):
    method = msg.get("method")
    msg_id = msg.get("id")  # echoed verbatim (preserves int/str type)

    if method == "initialize":
        params = msg.get("params") or {}
        respond(msg_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {
                "experimental": {"claude/channel": {}},
                "tools": {},
            },
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        })
    elif method == "notifications/initialized":
        _initialized.set()  # gate one of two: watcher also needs registration
    elif method == "ping":
        respond(msg_id, {})
    elif method == "tools/list":
        respond(msg_id, {"tools": tools_mod.TOOL_DEFINITIONS})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        text, is_error = tools_mod.dispatch(name, arguments, ctx)
        respond(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
    elif msg_id is not None:
        respond_error(msg_id, -32601, f"Method not found: {method}")
    # Unknown notifications (no id): ignore.


def _maybe_auto_register():
    """Auto-register from $TEAMMATE_AGENT if it's set (best-effort convenience)."""
    env_agent = os.environ.get("TEAMMATE_AGENT")
    if _looks_unset(env_agent):
        return
    env_team = os.environ.get("TEAMMATE_TEAM")
    team = None if _looks_unset(env_team) else env_team.strip()
    try:
        register_identity(env_agent.strip(), team, None)
    except CommsError as e:
        log(f"auto-register from $TEAMMATE_AGENT skipped: {e}")


def main():
    ctx = {"identity": _identity, "register": register_identity}

    watcher = threading.Thread(
        target=channel.run_watcher,
        args=(send_message, _identity, _initialized, _registered, _stop),
        daemon=True,
    )
    watcher.start()

    _maybe_auto_register()

    try:
        for raw in iter(sys.stdin.buffer.readline, b""):
            line = raw.decode("utf-8-sig", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            handle(msg, ctx)
    finally:
        _stop.set()
        agent, team, root, _ = _identity.snapshot()
        if agent and root is not None:
            write_agent_record(root, team, agent, timeout=2,
                               channel=False, lastHeartbeat=now_timestamp())
        # Stop the dashboard HTTP server if one was launched this session. Wrapped so a
        # shutdown hiccup can never prevent the offline-record write above.
        try:
            from . import dashboard
            dashboard.shutdown_dashboard()
        except Exception as e:
            log(f"dashboard shutdown skipped: {e}")


if __name__ == "__main__":
    main()
