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
import traceback
import uuid
from datetime import datetime
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
    heartbeat_fresh,
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
# The MCP revision WE implement — always answered as-is, never an echo of the client's
# requested protocolVersion (a client requesting an unsupported version must be told the
# truth, not a false compatibility claim).
PROTOCOL_VERSION = "2025-06-18"

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
        # Monotonically incremented on every .set() call. The watcher detects a same-name
        # re-registration (post-compaction) by comparing generations and resets known_ids,
        # clocks, and cursors so stale in-memory state doesn't suppress new wakes.
        self._generation = 0
        # Minted ONCE per server process (this object is constructed once at module import)
        # and never mutated after — a UUID, unlike a PID, is never reused across process
        # restarts. WP-19: lets the register-time collision warning (S2) and the watcher's
        # flap-kill (I1) both tell "truly the same running instance" from "a different
        # instance that happens to share a name". No lock needed: immutable post-construction.
        self.instance_id = uuid.uuid4().hex
        # The epoch THIS instance minted at its own last register_identity call (None until
        # the first register). Fed to compute_heartbeat_permit's TOCTOU tie-break: a foreign
        # instance_id with an epoch that still matches ours means a competitor's heartbeat
        # write raced OUR register, not a legitimate new registration — see channel.py.
        self._epoch = None

    def get_instance_id(self):
        return self.instance_id

    def set_epoch(self, epoch):
        with self._lock:
            self._epoch = epoch

    def get_epoch(self):
        with self._lock:
            return self._epoch

    def set(self, agent, team, root, unread_file):
        with self._lock:
            if agent != self.agent:
                self._last_seen = None  # new identity → drop the previous one's seen-ids
            self._generation += 1
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

    def get_generation(self):
        """Return the current generation counter (bumped on every .set() call)."""
        with self._lock:
            return self._generation


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
    my_instance_id = _identity.get_instance_id()

    # Read whatever is currently on disk for this name BEFORE any side effect — the human
    # guard (WP-19 item 5, mandatory) must fire before ensure_inbox/write ever touch the
    # human's inbox or record: an agent may never claim the operator's identity.
    existing = read_agent_record(root, team, agent)
    if existing and existing.get("type") == "human":
        raise CommsError(
            f"Cannot register as {agent!r}: that name belongs to the human operator "
            f"(type=human, registered via teammate_dashboard). An agent may not claim "
            f"the operator's identity — register under a distinct name."
        )

    inboxes_dir = get_inboxes_dir(root, team)
    ensure_inbox(inboxes_dir, agent)
    unread_file = inboxes_dir / f"{agent}_unread.json"

    # If re-registering to a different agent, mark the old one offline.
    old_agent, old_team, old_root, _ = _identity.snapshot()
    if old_agent and old_agent != agent and old_root is not None:
        write_agent_record(old_root, old_team, old_agent, timeout=2,
                           channel=False, lastHeartbeat=now_timestamp())

    # Collision guard (I1/S2): is `existing` another CURRENTLY LIVE claimant? Checked primarily
    # via instance_id (a UUID — immune to PID reuse across process restarts, unlike a bare PID
    # compare) with is_channel_alive as the fallback signal for legacy records that predate
    # WP-19 (no instance_id yet). Policy stays "most-recent-register wins" (constraint from the
    # brief) — we warn LOUDLY in the return text, we do not refuse.
    warning = ""
    note = ""
    if existing:
        foreign_id = existing.get("instance_id")
        if foreign_id and foreign_id != my_instance_id:
            is_live_foreign = heartbeat_fresh(existing.get("lastHeartbeat"), datetime.now())
        else:
            is_live_foreign = (not foreign_id and existing.get("pid") != os.getpid()
                                and is_channel_alive(existing))
        if is_live_foreign:
            warning = (
                f"⚠️ WARNING: another live instance already holds {agent!r} "
                f"(host={existing.get('host')!r}, pid={existing.get('pid')!r}). Registering "
                f"here WINS — that instance will stop heartbeating once this completes — but "
                f"two agents sharing one name split messages unpredictably in the meantime. "
                f"If this was accidental, re-register under a distinct name.\n\n"
            )
        elif not is_channel_alive(existing):
            # Offline-adoption note (C5): silently inheriting another project's inbox/history
            # under this name is exactly the "identity has no owner" failure mode — say so.
            existing_project = existing.get("project") or ""
            new_project = profile_fields.get("project") or ""
            if existing_project and new_project and existing_project != new_project:
                note = (
                    f"\nNOTE: adopting an existing identity previously used in project "
                    f"{existing_project!r} — you inherit its inbox and transcript attribution.\n"
                )

    write_agent_record(
        root, team, agent, timeout=5, bump_epoch=True,
        type="full", channel=True, pid=os.getpid(), host=hostname,
        instance_id=my_instance_id,
        startedAt=now_timestamp(), lastHeartbeat=now_timestamp(),
        **profile_fields,
        **({"spawned_by": spawned_by} if spawned_by else {}),
    )

    # Read the just-written record ONCE — reused for both the epoch stamp (TOCTOU tie-break,
    # WP-19: fed to the watcher's flap-kill so it can tell "my own registration, heartbeat-
    # stomped by a race" from "a genuinely later registration") and the profile echo below.
    # Nothing else can have changed the record yet: this call is single-threaded up to here.
    effective = read_agent_record(root, team, agent) or {}
    _identity.set(agent, team, root, unread_file)
    _identity.set_epoch(effective.get("epoch"))
    _registered.set()

    unread = read_json_readonly(unread_file) or []
    log(f"registered: agent={agent!r} team={team!r} comms_root={root} (from {source})")
    team_str = f", team {team!r}" if team else ""

    # Echo the effective profile back so the agent is reminded who it is (incl. its
    # personality).
    set_fields = [(k, effective[k]) for k in PROFILE_FIELDS if effective.get(k)]
    if set_fields:
        profile_str = "Your profile — " + "; ".join(f"{k}: {v!r}" for k, v in set_fields) + ". "
    else:
        profile_str = (
            "No profile set — set one with teammate_update "
            "(role/personality/status/authority) so teammates know what you're doing. "
        )
    return warning + (
        f"Registered as {agent!r}{team_str}. Comms root: {root} (from {source}). "
        f"Channel armed. {profile_str}You have {len(unread)} unread message(s) — call "
        f"teammate_inbox to read them."
    ) + note


def handle(msg, ctx):
    method = msg.get("method")
    msg_id = msg.get("id")  # echoed verbatim (preserves int/str type)

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "experimental": {"claude/channel": {}},
                "tools": {},
            },
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        }
        if msg_id is not None:
            respond(msg_id, result)
    elif method == "notifications/initialized":
        _initialized.set()  # gate one of two: watcher also needs registration
    elif method == "ping":
        if msg_id is not None:
            respond(msg_id, {})
    elif method == "tools/list":
        if msg_id is not None:
            respond(msg_id, {"tools": tools_mod.TOOL_DEFINITIONS})
    elif method == "tools/call":
        # A notification-form call (no id) is still EXECUTED — only the response is skipped.
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        text, is_error = tools_mod.dispatch(name, arguments, ctx)
        if msg_id is not None:
            respond(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
    elif msg_id is not None:
        respond_error(msg_id, -32601, f"Method not found: {method}")
    # Unknown notifications (no id): ignore.


def handle_safely(msg, ctx):
    """Wrap ``handle`` so one bad request can't kill the main loop (S1). A crash mid-dispatch is
    logged to stderr (never stdout — would corrupt the JSON-RPC stream) and, if the request had
    an id, best-effort answered with -32603 so the caller isn't left hanging forever."""
    try:
        handle(msg, ctx)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        msg_id = msg.get("id")
        if msg_id is not None:
            try:
                respond_error(msg_id, -32603, "Internal error")
            except Exception:
                pass


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


def _avatar_subcommand(argv):
    """Print a cached avatar strip to stdout.  No Pillow, no network.

    Usage: teammate-comms avatar --name <Name> [--format ansi|ascii]
           teammate-comms avatar --self   [--format ansi|ascii]

    If --self doesn't resolve (e.g. no stdin JSON or missing agent key), prints
    nothing and exits 0 so the statusline degrades gracefully to blank.
    """
    import argparse
    parser = argparse.ArgumentParser(
        prog="teammate-comms avatar",
        description="Print a cached avatar strip to stdout.",
        add_help=False,
    )
    parser.add_argument("--name", default=None)
    parser.add_argument("--self", dest="self_", action="store_true")
    parser.add_argument("--format", dest="fmt", choices=("ansi", "ascii"), default="ansi")
    try:
        parsed = parser.parse_args(argv)
    except SystemExit:
        return

    name = None
    if parsed.name:
        name = parsed.name.strip() or None
    elif parsed.self_:
        try:
            import json as _json
            raw = sys.stdin.read()
            if raw:
                data = _json.loads(raw)
                name = (data.get("agent") or "").strip() or None
        except Exception:
            pass

    if not name:
        return  # print nothing, exit 0 — statusline degrades to blank

    # Validate name before constructing any path — block traversal (mirrors dashboard.py:_api_avatar).
    try:
        validate_agent_name(name)
    except CommsError:
        return  # degrade to blank/exit-0, consistent with other miss paths

    root, _ = resolve_comms_root(None)
    team = os.environ.get("TEAMMATE_TEAM") or None
    if team and not team.strip():
        team = None

    from .comms import get_avatars_dir
    ext = "ansi" if parsed.fmt == "ansi" else "txt"
    sidecar = get_avatars_dir(root, team) / f"{name}.{ext}"
    try:
        content = sidecar.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return  # no avatar / tampered sidecar; print nothing, exit 0

    print(content)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "avatar":
        _avatar_subcommand(sys.argv[2:])
        return

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
            if not isinstance(msg, dict):
                # A syntactically-valid but non-object frame (bare scalar, null, or a
                # spec-legal JSON-RPC batch array — batching is unsupported here) — a
                # single -32600 with id null per JSON-RPC 2.0, never a crash.
                respond_error(None, -32600, "Invalid Request: expected a JSON object")
                continue
            handle_safely(msg, ctx)
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
