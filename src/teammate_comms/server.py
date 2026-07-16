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
import re
import socket
import subprocess
import sys
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from . import __version__, channel
from . import tools as tools_mod
from .comms import (
    COMPACT_BROKER_SENDER,
    PROFILE_FIELDS,
    CommsError,
    _looks_unset,
    ensure_inbox,
    find_case_variant,
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
    write_json_atomic,
)

SERVER_NAME = "teammate-comms"
# The MCP revision WE implement — always answered as-is, never an echo of the client's
# requested protocolVersion (a client requesting an unsupported version must be told the
# truth, not a false compatibility claim).
PROTOCOL_VERSION = "2025-06-18"

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
            self._generation += 1
            self.agent, self.team, self.root, self.unread_file = agent, team, root, unread_file

    def snapshot(self):
        with self._lock:
            return (self.agent, self.team, self.root, self.unread_file)

    def snapshot_with_generation(self):
        """Same as ``snapshot()`` + ``get_generation()`` but under ONE lock acquisition (W4) —
        two separate acquisitions could have a ``set()`` land in between, pairing a STALE
        root/inbox with a NEW generation for one watcher tick."""
        with self._lock:
            return (self.agent, self.team, self.root, self.unread_file, self._generation)

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


# G3: path-derived labels (_project_label above) split the SAME repo cloned at different paths
# on two machines into two roster entries — the mirror-image of the F-4 fix that introduced
# them. A git remote URL is stable across clones/machines, so it's tried FIRST; the path label
# stays the fallback (a non-repo dir, a repo with no origin, git missing, or an unparseable URL).
_GIT_REMOTE_SSH = re.compile(r"^[\w.-]+@[\w.-]+:([^/].*)$")
_GIT_REMOTE_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+/(.+)$")


def _project_label_from_remote(url):
    """Pure: parse a git remote URL into an ``owner/repo`` label, or None if it doesn't match a
    recognized shape. Handles ``https://host/owner/repo(.git)`` and
    ``git@host:owner/repo(.git)`` (SSH shorthand); anything else (a bare host with no path,
    garbage, empty) → None — deliberately conservative, since a WRONG guess is worse than no
    guess (profiles are keyed on this label). Never raises.
    """
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    if url.endswith(".git"):
        url = url[:-4]
    m = _GIT_REMOTE_SSH.match(url)
    path = m.group(1) if m else None
    if path is None:
        m2 = _GIT_REMOTE_URL.match(url)
        if not m2:
            return None
        path = m2.group(1)
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[-2], parts[-1]
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


def _project_label_from_git_remote(proj_dir):
    """Best-effort: resolve ``proj_dir``'s git ``origin`` remote → an owner/repo label via
    ``_project_label_from_remote``. Returns None on ANY failure (not a git repo, no origin, git
    not on PATH, timeout, unparseable URL) — the caller falls back to the path-derived label.
    3s timeout so a hung/misbehaving git (an interactive credential prompt, a slow remote
    helper) can never stall registration. ``CREATE_NO_WINDOW`` on Windows so this subprocess
    never flashes a console during what is otherwise a silent, headless registration.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(proj_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _project_label_from_remote(result.stdout.strip())


def register_identity(agent, team, comms_dir, profile=None, manager=None):
    """Establish identity + start watching. Raises CommsError on bad input.

    Used by the teammate_register tool and by the optional env auto-register.
    ``profile`` is an optional dict of free-text profile fields (role/personality/
    status/authority) validated and written onto the registry record. ``manager``
    is the explicit teammate_register param (WP-36) — see the precedence note
    below; None means "not given" (distinct from an explicit empty-string clear).
    Returns a human-readable status string.
    """
    validate_agent_name(agent)
    # WP-37 item 6: reserve the broker's sender label — same shape as the human-operator
    # rejection below, case-insensitively (the G2 case-variant lesson applies). Without this,
    # any agent could register as "compact-broker" (or "Compact-Broker") and forge the audit
    # DMs / completion notices that name identifies. Checked before any side effect.
    if agent.lower() == COMPACT_BROKER_SENDER:
        raise CommsError(
            f"Cannot register as {agent!r}: that name is reserved for the compaction-broker's "
            f"sender label (audit DMs, completion/expiry notices) — an agent may not claim it."
        )
    profile = dict(profile or {})
    # Auto-fill `project` from the current project dir (Claude Code injects
    # CLAUDE_PROJECT_DIR) unless the agent set it explicitly. With a global-by-
    # default comms root, this is how teammate_list shows who is working where.
    if "project" not in profile:
        proj_dir = os.environ.get("CLAUDE_PROJECT_DIR")
        if not _looks_unset(proj_dir):
            stripped_dir = proj_dir.strip()
            # G3: prefer the git remote-derived label (stable across clones/machines) over the
            # path-derived one (which splits the SAME repo cloned at different paths). The
            # remote label needs the SAME truncation guard _project_label applies internally —
            # an auto-fill convenience must not be able to raise out of validate_profile_field.
            label = _project_label_from_git_remote(stripped_dir)
            if label:
                label = label[:PROFILE_FIELDS["project"]]
            else:
                label = _project_label(stripped_dir)
            profile["project"] = label
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

    # Manager field (WP-36, command authority — NOT provenance; see spawned_by above for the
    # different fact). Precedence (gate finding, item 4 — revised from the first pass): a
    # VALID $AGENT_MANAGER env wins outright; env ABSENT *or* MALFORMED falls through to the
    # explicit `manager` param (a malformed value RAISES — the caller typed it, unlike a
    # garbled env var, which is dropped like spawned_by). A malformed env value must never
    # VETO a deliberately typed valid param — that would silently discard real intent.
    # Neither given → omit the key so write_agent_record's field-merge preserves the existing
    # value on disk.
    manager_fields = {}
    env_manager = os.environ.get("AGENT_MANAGER")
    env_valid = False
    if not _looks_unset(env_manager):
        candidate = env_manager.strip()
        try:
            validate_agent_name(candidate)
            manager_fields["manager"] = candidate
            env_valid = True
        except CommsError:
            pass  # malformed env: dropped, falls through to the explicit param below

    if not env_valid and manager is not None:
        # Gate finding, item 2: a non-string explicit param (e.g. an int) must raise a clean
        # CommsError, not an AttributeError from .strip() surfacing as "failed unexpectedly".
        if not isinstance(manager, str):
            raise CommsError(
                f"'manager' must be a string agent name, got {type(manager).__name__}."
            )
        # CLEAR MECHANICS (peer-review finding): validate_agent_name("") always RAISES, so an
        # empty/whitespace explicit value is handled as the clear sentinel BEFORE validation
        # ever runs — only a non-empty explicit value goes through validate_agent_name.
        if not manager.strip():
            manager_fields["manager"] = None
        else:
            validate_agent_name(manager.strip())
            manager_fields["manager"] = manager.strip()

    # WezTerm pane binding (WP-36) — captured at register only, no heartbeat refresh. Always
    # computed (even to None) so it's always PASSED to write_agent_record below: a re-register
    # outside WezTerm must overwrite a stale binding with None, not silently keep it (the field-
    # merge otherwise preserves an omitted key, which here would mean the broker keeps typing
    # into a pane this agent no longer owns).
    pane_id = None
    pane_env = os.environ.get("WEZTERM_PANE")
    if not _looks_unset(pane_env):
        try:
            pane_id = int(pane_env.strip())
        except ValueError:
            pane_id = None  # malformed value never fails registration
    wezterm_socket = None
    socket_env = os.environ.get("WEZTERM_UNIX_SOCKET")
    if not _looks_unset(socket_env):
        wezterm_socket = Path(socket_env.strip()).name or None

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
    # G2: reject a case-variant collision (an EXACT re-register of the same spelling is
    # untouched — find_case_variant excludes it). A shared comms root behaves differently by
    # OS otherwise: Windows merges "Bob"/"bob" into one record, Linux splits them into shadow
    # identities that never see each other.
    case_variant = find_case_variant(root, team, agent)
    if case_variant:
        raise CommsError(
            f"A teammate is already registered as {case_variant!r} — register with that "
            f"exact spelling, or pick a distinct name."
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

    effective = write_agent_record(
        root, team, agent, timeout=5, bump_epoch=True,
        type="full", channel=True, pid=os.getpid(), host=hostname,
        instance_id=my_instance_id,
        # G4: persist the resolved comms root (already logged below) so a divergent peer —
        # one that resolved a DIFFERENT root and can therefore never exchange a message with
        # this instance — is diagnosable after the fact via teammate_whoami/doctor instead of
        # looking textually identical to a genuinely-offline recipient.
        comms_root=str(root),
        startedAt=now_timestamp(), lastHeartbeat=now_timestamp(),
        pane_id=pane_id, wezterm_socket=wezterm_socket,
        **profile_fields,
        **manager_fields,
        **({"spawned_by": spawned_by} if spawned_by else {}),
    ) or {}
    # Take the epoch straight off THIS call's return (WP-19 gate CR) — NEVER from a
    # subsequent read-back. A read-back is unlocked: between our write and the read, a
    # COMPETITOR'S register can land (epoch N+1), and our read-back would then return N+1 —
    # both instances would store N+1 as "my epoch" and the TOCTOU tie-break would have BOTH
    # sides re-claim forever, exactly the flap the tie-break exists to kill. The return value
    # is race-free by construction (computed under the same lock as the write).
    _identity.set(agent, team, root, unread_file)
    _identity.set_epoch(effective.get("epoch"))
    # S4: ANY successful register clears a stale auto-register failure — the agent is
    # registered now (whether this call WAS the auto-register retry or a manual one), so the
    # earlier failure note would be misleading noise from here on.
    global _auto_register_error
    _auto_register_error = None
    unread = read_json_readonly(unread_file) or []
    _registered.set()

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


# S4: a CommsError during $TEAMMATE_AGENT auto-register (the reincarnate-child path) used to
# be a stderr-only log — the child looked alive to its parent but never appeared in the
# roster, with nothing in-conversation ever saying why. Stored here so teammate_whoami and
# _require_registered's error text can both surface it; cleared by ANY later successful
# register (see register_identity).
_auto_register_error = None


def _get_auto_register_error():
    return _auto_register_error


def _maybe_auto_register():
    """Auto-register from $TEAMMATE_AGENT if it's set (best-effort convenience)."""
    global _auto_register_error
    env_agent = os.environ.get("TEAMMATE_AGENT")
    if _looks_unset(env_agent):
        return
    env_team = os.environ.get("TEAMMATE_TEAM")
    team = None if _looks_unset(env_team) else env_team.strip()
    try:
        register_identity(env_agent.strip(), team, None)
    except CommsError as e:
        _auto_register_error = str(e)
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


def _write_plugin_runtime_pointer():
    """WP-38: a broker daemon launched outside the plugin's own tooling (Task Scheduler,
    bare PowerShell) cannot derive the plugin's versioned cache path, and bare
    ``python -m teammate_comms.deliver`` is a ``ModuleNotFoundError`` on any system Python.
    Drop a pointer file the broker reads FRESH per invocation, then runs
    ``<python> -m teammate_comms.deliver ...`` — ``sys.executable`` is the venv interpreter
    THIS live server imports the package from, so ``-m`` resolves; the file self-heals
    across plugin version bumps because every server start rewrites it. Uses the DEFAULT
    root resolution (no explicit comms_dir — identity isn't established yet at this point
    in startup), same as the auto-register path below. Best-effort: a failure here (an
    unwritable dir, e.g.) must NEVER block server boot."""
    try:
        root, _source = resolve_comms_root(None)
        plugin_root = Path(__file__).resolve().parent.parent  # the package's PARENT dir
        pointer = {
            "v": 1,
            "python": sys.executable,
            "plugin_root": str(plugin_root),
            "version": __version__,
            "written_at": now_timestamp(),
        }
        pointer_path = Path(root) / "TeammateComms" / "plugin-runtime.json"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(pointer_path, pointer)
    except Exception as e:
        log(f"plugin-runtime.json pointer write skipped: {e}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "avatar":
        _avatar_subcommand(sys.argv[2:])
        return

    # The running server's serverInfo.version is spawn-frozen for this process's whole
    # lifetime, while a mid-session plugin update changes what's on disk — this line is
    # the diagnostic anchor for "was this session running stale code" in the debug log
    # (~/.claude/debug/<session>.txt).
    log(f"starting teammate-comms v{__version__}")
    _write_plugin_runtime_pointer()

    ctx = {"identity": _identity, "register": register_identity,
           "auto_register_error": _get_auto_register_error}

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
