"""Storage, registry, and liveness helpers for teammate-comms.

Ported from the proven TestSVN prototype's ``common.py`` with two deliberate
changes for the plugin/MCP-server context:

1. **Comms-root resolution** replaces the prototype's ``git rev-parse``. A
   plugin-spawned server's cwd is the plugin cache directory (itself a git
   repo), so git-based resolution would scatter inboxes into the cache. We
   resolve an explicit root instead (see ``resolve_comms_root``).
2. **``validate_agent_name`` raises** ``ValueError`` instead of calling
   ``sys.exit``. The server is long-lived; a bad tool argument must surface as
   a tool error, never tear down the whole process.
"""

import json
import os
import re
import socket
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# Valid agent name: alphanumeric, hyphens, underscores, dots (no traversal).
AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# Single timestamp format shared by message IDs, registry records, and liveness
# checks. Naive local time — writer and reader are always co-located.
TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%S.%f"


class CommsError(Exception):
    """Raised for recoverable comms failures (invalid input, missing root).

    Tool handlers convert this into an ``isError`` result; startup code catches
    it to log and exit cleanly. It must never escape as an unhandled crash.
    """


def now_timestamp():
    """Current naive-local timestamp in the shared format."""
    return datetime.now().strftime(TIMESTAMP_FMT)


def validate_agent_name(name):
    """Validate an agent name; raise CommsError on anything unsafe.

    Unlike the prototype this does NOT call sys.exit — the server is long-lived
    and a bad ``to``/``id`` argument from a tool call must not kill it.
    """
    if not isinstance(name, str) or not AGENT_NAME_PATTERN.match(name) or ".." in name:
        raise CommsError(
            f"Invalid agent name {name!r}. Use alphanumerics, hyphens, "
            f"underscores, and dots only (no path separators)."
        )


def _looks_unset(value):
    """True if an env value is missing, blank, or an unexpanded ``${...}`` token.

    Claude Code's manifest ``${VAR}`` expansion may leave a literal token if the
    referenced shell var is unset; treat that as "not provided" rather than a
    real value.
    """
    if not value:
        return True
    v = value.strip()
    return not v or ("${" in v and "}" in v)


def resolve_comms_root():
    """Resolve the directory under which ``TeammateComms/`` lives.

    Resolution order (first hit wins):
      1. ``$TEAMMATE_COMMS_DIR`` — explicit override (cross-project / global).
      2. ``$CLAUDE_PROJECT_DIR`` — the authoritative project root that Claude
         Code injects into a spawned server's environment.
    Never falls back to cwd/git (a plugin server's cwd is the plugin cache).
    Raises CommsError if neither is usable.

    Returns ``(root: Path, source: str)`` so callers can report which won.
    """
    override = os.environ.get("TEAMMATE_COMMS_DIR")
    if not _looks_unset(override):
        return Path(override.strip()), "TEAMMATE_COMMS_DIR"

    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if not _looks_unset(project):
        return Path(project.strip()), "CLAUDE_PROJECT_DIR"

    raise CommsError(
        "No comms root. Set TEAMMATE_COMMS_DIR, or run inside a project where "
        "Claude Code provides CLAUDE_PROJECT_DIR."
    )


def get_inboxes_dir(team=None):
    """``<root>/TeammateComms/[<team>/]inboxes`` (team namespacing optional)."""
    root, _ = resolve_comms_root()
    base = root / "TeammateComms"
    if team:
        base = base / team
    return base / "inboxes"


def get_agents_dir(team=None):
    """``<root>/TeammateComms/[<team>/]agents`` registry directory."""
    root, _ = resolve_comms_root()
    base = root / "TeammateComms"
    if team:
        base = base / team
    return base / "agents"


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

    Returns the parsed value, or None if the file is missing or currently
    unreadable (e.g. a concurrent partial write). Never mutates the file — safe
    for a high-frequency poller that holds no write lock. Callers skip on None.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def write_json_atomic(filepath, data):
    """Write JSON via a temp file + os.replace (atomic on the same volume).

    A concurrent reader sees either the old contents or the new — never a
    half-written truncation.
    """
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
    """Best-effort lock that never raises. Yields True if acquired, else False.

    Safe to call from a background thread (heartbeat loop): a failed acquire
    simply skips the write and catches up next cycle.
    """
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


def write_agent_record(team, name, timeout=5, **fields):
    """Field-level merge of ``fields`` into ``agents/<name>.json`` under a lock.

    Only provided keys are overwritten; existing keys are preserved, so
    ``setup``-owned fields (``type``) and channel-owned fields
    (``pid``/``channel``/``lastHeartbeat``) coexist. Returns True if written.

    Hardening over the prototype: if the record file *exists* but currently
    reads as None (a concurrent mid-write), skip this write instead of
    clobbering it with an empty dict — that would transiently drop ``type``.
    """
    agents_dir = get_agents_dir(team)
    agents_dir.mkdir(parents=True, exist_ok=True)
    record_path = agents_dir / f"{name}.json"
    with file_lock_optional(record_path, timeout=timeout) as acquired:
        if not acquired:
            return False
        record = read_json_readonly(record_path)
        if record is None and record_path.exists():
            # Exists but unreadable right now — don't clobber; retry next cycle.
            return False
        if not isinstance(record, dict):
            record = {}
        record["name"] = name
        record.update(fields)
        write_json_atomic(record_path, record)
        return True


def read_agent_record(team, name):
    """Read ``agents/<name>.json``, or None if absent/unreadable."""
    record = read_json_readonly(get_agents_dir(team) / f"{name}.json")
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
    window). Otherwise (or if the pid check is undetermined / cross-host): fall
    back to heartbeat freshness within ``staleness`` seconds.

    ``teammate_list`` passes ``pid_check=False`` so listing N agents does not
    spawn N ``tasklist`` subprocesses.
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
