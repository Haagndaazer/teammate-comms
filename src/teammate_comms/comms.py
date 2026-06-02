"""Storage, registry, and liveness helpers for teammate-comms.

Ported from the proven TestSVN prototype's ``common.py`` with two deliberate
changes for the plugin/MCP-server context:

1. **Comms-root resolution** replaces the prototype's ``git rev-parse``. A
   plugin-spawned server's cwd is the plugin cache directory (itself a git
   repo), so git-based resolution would scatter inboxes there. The root is
   resolved once at registration (see ``resolve_comms_root``) and then passed
   explicitly to the path/registry helpers.
2. **``validate_agent_name`` raises** ``CommsError`` instead of calling
   ``sys.exit``. The server is long-lived; a bad tool argument must surface as
   a tool error, never tear down the whole process.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# Valid agent name: alphanumeric, hyphens, underscores, dots (no traversal).
AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# Single timestamp format shared by message IDs, registry records, and liveness
# checks. Naive local time — writer and reader are always co-located.
TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%S.%f"

# Optional free-text profile fields an agent can attach to its registry record,
# mapped to a max length. All single-line: a teammate scans these in teammate_list
# (status/authority) or reads the full set via teammate_profile. Embedded newlines
# are collapsed so a value can never break the one-block-per-teammate list layout.
PROFILE_FIELDS = {
    "project": 100,
    "role": 200,
    "personality": 280,
    "status": 200,
    "authority": 500,
}


class CommsError(Exception):
    """Raised for recoverable comms failures (invalid input, missing root).

    Tool handlers convert this into an ``isError`` result. It must never escape
    as an unhandled crash.
    """


def now_timestamp():
    """Current naive-local timestamp in the shared format."""
    return datetime.now().strftime(TIMESTAMP_FMT)


def validate_agent_name(name):
    """Validate an agent name; raise CommsError on anything unsafe.

    Does NOT call sys.exit — a bad ``to``/``agent`` argument from a tool call
    must not kill the long-lived server.
    """
    if not isinstance(name, str) or not AGENT_NAME_PATTERN.match(name) or ".." in name:
        raise CommsError(
            f"Invalid agent name {name!r}. Use alphanumerics, hyphens, "
            f"underscores, and dots only (no path separators)."
        )


def validate_profile_field(name, value):
    """Normalize one optional profile field; raise CommsError on bad input.

    Coerces to a trimmed, single-line string (internal whitespace/newlines
    collapsed to single spaces so a value can never break the teammate_list block
    layout) and length-caps per ``PROFILE_FIELDS``. A value that trims to empty
    returns ``""`` — the caller stores that, which reads as "cleared".
    """
    if name not in PROFILE_FIELDS:
        raise CommsError(
            f"Unknown profile field {name!r}. Valid fields: {sorted(PROFILE_FIELDS)}."
        )
    if not isinstance(value, str):
        raise CommsError(f"Profile field {name!r} must be a string, got {type(value).__name__}.")
    collapsed = " ".join(value.split())
    max_len = PROFILE_FIELDS[name]
    if len(collapsed) > max_len:
        raise CommsError(f"Profile field {name!r} exceeds {max_len} characters.")
    return collapsed


def validate_group_name(name):
    """Validate a group name (with or without a leading ``#``); return the clean name.

    Groups are addressed with a ``#`` sigil (``teammate_send(to="#design")``) so they
    occupy a separate namespace from agents and can never collide. The stored name and
    on-disk paths use the clean (sigil-stripped) form. Raises CommsError on anything
    unsafe — same character set as an agent name.
    """
    if not isinstance(name, str):
        raise CommsError(f"Invalid group name {name!r}.")
    clean = name[1:] if name.startswith("#") else name
    if not AGENT_NAME_PATTERN.match(clean) or ".." in clean:
        raise CommsError(
            f"Invalid group name {name!r}. Use alphanumerics, hyphens, underscores, "
            f"and dots only (an optional leading '#' is allowed)."
        )
    return clean


def _looks_unset(value):
    """True if an env value is missing, blank, or an unexpanded ``${...}`` token."""
    if not value:
        return True
    v = value.strip()
    return not v or ("${" in v and "}" in v)


def resolve_comms_root(explicit=None):
    """Resolve the directory under which ``TeammateComms/`` lives.

    Order (first hit wins):
      1. ``explicit`` — a comms_dir passed to teammate_register.
      2. ``$TEAMMATE_COMMS_DIR`` — explicit env override (per-project isolation).
      3. ``$CLAUDE_CONFIG_DIR`` — the user's Claude config dir, if relocated.
      4. ``~/.claude`` — the default. This is **global by default**: every agent on
         the machine shares one comms space, so agents in different projects can
         message each other out of the box. For per-project isolation, set
         ``$TEAMMATE_COMMS_DIR`` (or pass ``comms_dir``) to the project dir.

    Note: ``$CLAUDE_PROJECT_DIR`` is no longer the default root (that isolated
    agents per repo) — it is now used only to auto-fill the ``project`` profile
    field at registration. Always resolves (never raises).

    Returns ``(root: Path, source: str)``.
    """
    if explicit and not _looks_unset(explicit):
        return Path(explicit.strip()), "comms_dir arg"

    override = os.environ.get("TEAMMATE_COMMS_DIR")
    if not _looks_unset(override):
        return Path(override.strip()), "TEAMMATE_COMMS_DIR"

    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if not _looks_unset(config):
        return Path(config.strip()), "CLAUDE_CONFIG_DIR"

    return Path.home() / ".claude", "~/.claude default"


def get_inboxes_dir(root, team=None):
    """``<root>/TeammateComms/[<team>/]inboxes`` (team namespacing optional)."""
    base = Path(root) / "TeammateComms"
    if team:
        base = base / team
    return base / "inboxes"


def get_agents_dir(root, team=None):
    """``<root>/TeammateComms/[<team>/]agents`` registry directory."""
    base = Path(root) / "TeammateComms"
    if team:
        base = base / team
    return base / "agents"


def get_groups_dir(root, team=None):
    """``<root>/TeammateComms/[<team>/]groups`` — one subdir per group."""
    base = Path(root) / "TeammateComms"
    if team:
        base = base / team
    return base / "groups"


def get_group_dir(root, team, group):
    """``<groups>/<group>/`` (group is the clean, sigil-stripped name)."""
    return get_groups_dir(root, team) / group


def read_group_meta(root, team, group):
    """Read ``groups/<group>/meta.json`` (non-destructive), or None if absent/unreadable."""
    meta = read_json_readonly(get_group_dir(root, team, group) / "meta.json")
    return meta if isinstance(meta, dict) else None


def write_group_meta(root, team, group, meta):
    """Atomically write a group's ``meta.json`` (caller holds any needed lock)."""
    group_dir = get_group_dir(root, team, group)
    group_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(group_dir / "meta.json", meta)


def read_group_messages(root, team, group):
    """Read a group's transcript (``messages.json``) non-destructively; [] if absent.

    Uses ``read_json_readonly`` (NOT read_json_safe) so a concurrent partial write is
    never mistaken for corruption and the shared transcript is never reset to []. A
    None (unreadable mid-write) is surfaced as [] to the caller for display.
    """
    msgs = read_json_readonly(get_group_dir(root, team, group) / "messages.json")
    return msgs if isinstance(msgs, list) else []


def append_group_message(root, team, group, record, timeout=10):
    """Append one record to a group's transcript under a lock (atomic write)."""
    group_dir = get_group_dir(root, team, group)
    group_dir.mkdir(parents=True, exist_ok=True)
    messages_file = group_dir / "messages.json"
    with file_lock(messages_file, timeout=timeout):
        messages = read_group_messages(root, team, group)
        messages.append(record)
        write_json_atomic(messages_file, messages)


def delete_group(root, team, group):
    """Remove a group's directory (meta + transcript). Best-effort."""
    group_dir = get_group_dir(root, team, group)
    if group_dir.exists():
        shutil.rmtree(group_dir, ignore_errors=True)


def ensure_inbox(inboxes_dir, agent):
    """Create ``<agent>_unread.json`` / ``_read.json`` if they don't exist."""
    inboxes_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("_unread.json", "_read.json"):
        filepath = inboxes_dir / f"{agent}{suffix}"
        if not filepath.exists():
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump([], f)


def read_json_safe(filepath):
    """Read a JSON file, resetting it to ``[]`` if corrupt. Holds no lock."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, ValueError):
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump([], f)
        return []


def read_json_readonly(filepath):
    """Read a JSON file WITHOUT writing on failure.

    Returns the parsed value, or None if missing or currently unreadable (e.g. a
    concurrent partial write). Never mutates the file — safe for a poller that
    holds no write lock. Callers skip on None.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def write_json_atomic(filepath, data):
    """Write JSON via a temp file + os.replace (atomic on the same volume)."""
    filepath = Path(filepath)
    tmp = filepath.with_name(filepath.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, filepath)


@contextmanager
def file_lock(lock_path, timeout=10):
    """Cross-platform lock via mkdir (atomic on all OSes). Raises on timeout."""
    lock_dir = Path(str(lock_path) + ".lock")
    start = time.time()
    while True:
        try:
            lock_dir.mkdir(parents=False, exist_ok=False)
            break
        except FileExistsError:
            if time.time() - start > timeout:
                try:
                    lock_dir.rmdir()
                except OSError:
                    pass
                try:
                    lock_dir.mkdir(parents=False, exist_ok=False)
                    break
                except FileExistsError:
                    raise CommsError(
                        f"Could not acquire lock on {lock_path.name} after {timeout}s."
                    )
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock_dir.rmdir()
        except OSError:
            pass


@contextmanager
def file_lock_optional(lock_path, timeout=2):
    """Best-effort lock that never raises. Yields True if acquired, else False."""
    lock_dir = Path(str(lock_path) + ".lock")
    start = time.time()
    acquired = False
    while True:
        try:
            lock_dir.mkdir(parents=False, exist_ok=False)
            acquired = True
            break
        except FileExistsError:
            if time.time() - start > timeout:
                break
            time.sleep(0.05)
        except OSError:
            break
    try:
        yield acquired
    finally:
        if acquired:
            try:
                lock_dir.rmdir()
            except OSError:
                pass


def write_agent_record(root, team, name, timeout=5, **fields):
    """Field-level merge of ``fields`` into ``agents/<name>.json`` under a lock.

    Only provided keys are overwritten; existing keys are preserved, so the
    register-owned ``type`` and the channel-owned ``pid``/``channel``/
    ``lastHeartbeat`` coexist. Returns True if written.

    Hardening: if the record file *exists* but currently reads as None (a
    concurrent mid-write), skip this write instead of clobbering ``type``.
    """
    agents_dir = get_agents_dir(root, team)
    agents_dir.mkdir(parents=True, exist_ok=True)
    record_path = agents_dir / f"{name}.json"
    with file_lock_optional(record_path, timeout=timeout) as acquired:
        if not acquired:
            return False
        record = read_json_readonly(record_path)
        if record is None and record_path.exists():
            return False
        if not isinstance(record, dict):
            record = {}
        record["name"] = name
        record.update(fields)
        write_json_atomic(record_path, record)
        return True


def read_agent_record(root, team, name):
    """Read ``agents/<name>.json``, or None if absent/unreadable."""
    record = read_json_readonly(get_agents_dir(root, team) / f"{name}.json")
    return record if isinstance(record, dict) else None


def _pid_alive(pid):
    """Local process liveness. Returns True/False, or None if undetermined."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        return f'"{pid}"' in out.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def is_channel_alive(record, staleness=30, pid_check=True):
    """Decide whether an agent's channel server is currently running.

    Same host with ``pid_check=True``: authoritative pid liveness (no staleness
    window). Otherwise (or if undetermined / cross-host): heartbeat freshness
    within ``staleness`` seconds. ``teammate_list`` passes ``pid_check=False`` so
    listing N agents does not spawn N ``tasklist`` subprocesses.
    """
    if not record or not record.get("channel"):
        return False
    if pid_check:
        host = record.get("host")
        if host and host == socket.gethostname():
            alive = _pid_alive(record.get("pid"))
            if alive is not None:
                return alive
    hb = record.get("lastHeartbeat")
    if not hb:
        return False
    try:
        last = datetime.strptime(hb, TIMESTAMP_FMT)
    except (ValueError, TypeError):
        return False
    return (datetime.now() - last).total_seconds() <= staleness


# ── Human-as-teammate + observability transcript ────────────────────────────────
#
# The dashboard (teammate_dashboard) registers the human operator as a first-class
# teammate so agents can see, DM, and invite them. A human record carries
# type="human" and a "presence" field, but deliberately NO "pid"/"channel": it must
# never be treated as a wakeable channel (is_channel_alive returns False with no
# "channel" key) and never trips the register-time collision guard (keyed on pid +
# is_channel_alive). The human is reachable by flat name like any other teammate.


def register_human(root, team, name):
    """Register a human operator as a teammate record (type="human") with an inbox.

    Additive over write_agent_record's field-merge: no pid, no channel — so the
    human is never a wakeable channel and never collides with a live agent of the
    same name. Idempotent (re-register just refreshes presence/host).
    """
    validate_agent_name(name)
    ensure_inbox(get_inboxes_dir(root, team), name)
    write_agent_record(
        root, team, name,
        type="human", host=socket.gethostname(),
        startedAt=now_timestamp(), presence="online",
    )


def set_human_presence(root, team, name, state):
    """Merge a presence marker ("online"/"away") into a human's record. Best-effort."""
    write_agent_record(root, team, name, presence=state)


def get_transcript_file(root, team=None):
    """``<root>/TeammateComms/[<team>/]transcript.jsonl`` — the global NDJSON log."""
    base = Path(root) / "TeammateComms"
    if team:
        base = base / team
    return base / "transcript.jsonl"


def append_transcript(root, team, record):
    """Best-effort tee of one message into the global NDJSON observability log.

    Append-only (O(1), one line per message) so the dashboard can show ALL DMs +
    group posts in one ordered stream. NEVER raises and NEVER blocks delivery:
    disabled by ``TEAMMATE_TRANSCRIPT=0``, uses a short non-blocking lock, and
    swallows every error to stderr. This is observability, not a delivery guarantee.
    """
    if os.environ.get("TEAMMATE_TRANSCRIPT", "1").strip() == "0":
        return
    try:
        path = get_transcript_file(root, team)
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock_optional(path, timeout=2) as acquired:
            if not acquired:
                return
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # observability must never affect message delivery
        print(f"[teammate-comms] transcript tee skipped: {e}", file=sys.stderr, flush=True)


def read_transcript(root, team=None, since=None, limit=200):
    """Read the global NDJSON transcript non-destructively, newest-bounded.

    Returns at most the last ``limit`` records in chronological order. With ``since``
    set, returns records whose id is ``>= since`` (the dashboard dedupes by id, so a
    boundary record repeating across polls is harmless and a same-microsecond id
    collision can never drop an unseen record). Missing file → []; bad lines skipped.
    """
    path = get_transcript_file(root, team)
    records = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if since and rec.get("id", "") < since:
                    continue
                records.append(rec)
    except (FileNotFoundError, OSError):
        return []
    if limit and limit > 0 and len(records) > limit:
        records = records[-limit:]
    return records
