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
import urllib.parse
import zlib
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

# Project profile fields mapped to max length. `path` and `status` are handled separately
# (path: uncapped, whitespace-collapsed only; status: enum, not free-text).
PROJECT_FIELDS = {
    "summary":     80,
    "description": 600,
    "tech_stack":  400,
    "repo_url":    200,
    "name":        100,
}
PROJECT_STATUS = ("active", "paused", "archived")


class CommsError(Exception):
    """Raised for recoverable comms failures (invalid input, missing root).

    Tool handlers convert this into an ``isError`` result. It must never escape
    as an unhandled crash.
    """


def now_timestamp():
    """Current naive-local timestamp in the shared format."""
    return datetime.now().strftime(TIMESTAMP_FMT)


# Windows reserved device names (G5) — checked on EVERY OS, not just Windows: a shared comms
# root can be read/written from Windows regardless of which OS created a given name, so a name
# that's fine on Linux but reserved on Windows must be rejected everywhere up front rather than
# failing opaquely later at file/lock creation on whichever host happens to touch it.
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _check_reserved_name(value, label):
    """Raise CommsError if ``value``'s FIRST dot-segment, lower-cased, is a Windows reserved
    device name (con/prn/aux/nul/com1-9/lpt1-9) — e.g. "con" and "con.helper" are rejected,
    "console" and "con-bot" are fine (G5)."""
    first_segment = value.split(".", 1)[0].lower()
    if first_segment in _WINDOWS_RESERVED_NAMES:
        raise CommsError(
            f"{label} {value!r} is a reserved device name on Windows ({first_segment!r}) — "
            f"pick a different name."
        )


def find_case_variant(root, team, name):
    """Return the exact spelling of an EXISTING agent/human record whose name matches
    ``name`` case-insensitively but differs in case, or None (G2).

    A shared comms root behaves differently by OS: Windows's case-insensitive filesystem
    would silently MERGE "Bob" and "bob" into one record, while Linux's case-sensitive
    filesystem SPLITS them into two shadow identities that never see each other — same root,
    opposite failure modes, neither detected. Rejecting the collision at register time (the
    caller raises using the returned spelling) means the retry self-corrects instead.
    """
    agents_dir = get_agents_dir(root, team)
    if not agents_dir.exists():
        return None
    lname = name.lower()
    for path in agents_dir.glob("*.json"):
        existing = path.stem
        if existing != name and existing.lower() == lname:
            return existing
    return None


def find_group_case_variant(root, team, group):
    """Return the exact spelling of an EXISTING group whose name matches ``group``
    case-insensitively but differs in case, or None (G2 — checked at group CREATE only; open
    membership means member names stay free-form addresses, not identities)."""
    groups_dir = get_groups_dir(root, team)
    if not groups_dir.exists():
        return None
    lname = group.lower()
    for path in groups_dir.iterdir():
        if not path.is_dir():
            continue
        existing = path.name
        if existing != group and existing.lower() == lname:
            return existing
    return None


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
    _check_reserved_name(name, "Agent name")


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


def validate_project_dir(path):
    """Resolve + validate a project directory (exists + is a dir). Raises CommsError.

    Used by teammate_reincarnate to validate the spawn target in the PARENT before
    launching. Resolves ``..``/symlinks so the value can't smuggle traversal.
    """
    if not isinstance(path, str) or not path.strip():
        raise CommsError("'project_dir' is required (an existing directory path).")
    try:
        p = Path(path.strip()).expanduser().resolve()
    except OSError as e:
        raise CommsError(f"Invalid project_dir {path!r}: {e}")
    if not p.exists():
        raise CommsError(f"project_dir does not exist: {p}")
    if not p.is_dir():
        raise CommsError(f"project_dir is not a directory: {p}")
    return p


_PROJECT_KEY_FORBIDDEN = re.compile(r'[:\*\?"<>|%\x00-\x1f]')


def validate_project_key(value):
    """Normalize a project key; raise CommsError on bad input.

    Normalization order (each step feeds into the next):
    1. Trim + collapse internal whitespace.
    2. Replace ``\\`` → ``/`` (Windows auto-fill uses backslashes).
    3. Lower-case fold (``Projects/Foo`` == ``projects/foo``).
    4. Strip leading/trailing slashes; collapse repeated ``/``.
    5. Reject forbidden chars: ``:``, ``*``, ``?``, ``"``, ``<``, ``>``, ``|``, ``%``
       (``%`` is forbidden so ``urllib.parse.quote`` slug encoding stays injective —
       see ``project_key_to_slug``), and ASCII control chars.
    6. Length-cap at 100.

    The resulting key is the canonical stored form. ``project_key_to_slug`` encodes it
    as a safe flat filename with no path separators (``/`` → ``%2F``); because ``%`` is
    forbidden in keys, no two distinct keys can produce the same slug.
    """
    if not isinstance(value, str):
        raise CommsError("Project key must be a string.")
    collapsed = " ".join(value.split())
    normalized = collapsed.replace("\\", "/").lower()
    normalized = normalized.strip("/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if _PROJECT_KEY_FORBIDDEN.search(normalized):
        raise CommsError(
            f"Project key contains forbidden characters (: * ? \" < > | % or control chars): {value!r}"
        )
    if not normalized:
        raise CommsError("Project key must not be empty after normalization.")
    if len(normalized) > 100:
        raise CommsError(
            f"Project key exceeds 100 characters after normalization ({len(normalized)} chars)."
        )
    for segment in normalized.split("/"):
        _check_reserved_name(segment, "Project key segment")  # G5, per "/"-component
    return normalized


def validate_project_field(name, value):
    """Normalize one project profile field; raise CommsError on bad input.

    ``status`` is validated against ``PROJECT_STATUS`` (enum, not free-text).
    ``path`` is NOT in ``PROJECT_FIELDS`` — callers handle it directly (uncapped,
    whitespace-collapse only). All other fields follow the same single-line,
    length-capped pattern as ``validate_profile_field``.
    """
    if name == "status":
        if value not in PROJECT_STATUS:
            raise CommsError(
                f"'status' must be one of {list(PROJECT_STATUS)}, got {value!r}."
            )
        return value
    if name not in PROJECT_FIELDS:
        raise CommsError(
            f"Unknown project field {name!r}. Valid fields: {sorted(PROJECT_FIELDS)} + status, path."
        )
    if not isinstance(value, str):
        raise CommsError(f"Project field {name!r} must be a string, got {type(value).__name__}.")
    collapsed = " ".join(value.split())
    max_len = PROJECT_FIELDS[name]
    if len(collapsed) > max_len:
        raise CommsError(f"Project field {name!r} exceeds {max_len} characters.")
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
    _check_reserved_name(clean, "Group name")
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


def get_avatars_dir(root, team=None):
    """``<root>/TeammateComms/[<team>/]avatars`` — pre-rendered avatar sidecars per agent."""
    base = Path(root) / "TeammateComms"
    if team:
        base = base / team
    return base / "avatars"


def get_groups_dir(root, team=None):
    """``<root>/TeammateComms/[<team>/]groups`` — one subdir per group."""
    base = Path(root) / "TeammateComms"
    if team:
        base = base / team
    return base / "groups"


def get_group_dir(root, team, group):
    """``<groups>/<group>/`` (group is the clean, sigil-stripped name)."""
    return get_groups_dir(root, team) / group


def get_projects_dir(root, team=None):
    """``<root>/TeammateComms/[<team>/]projects`` — one JSON file per project profile."""
    base = Path(root) / "TeammateComms"
    if team:
        base = base / team
    return base / "projects"


def project_key_to_slug(key):
    """Encode a normalized project key as a safe flat filename (no path separators).

    Uses percent-encoding (``urllib.parse.quote``) with ``safe=""`` so ``/`` becomes
    ``%2F`` and the result is a valid single-component filename on all OSes. Injective
    over normalized keys because ``%`` is forbidden by ``validate_project_key``, so no
    two distinct keys can produce the same slug.
    """
    return urllib.parse.quote(key, safe="")


def read_project_record(root, team, key):
    """Read a project profile by normalized key, or None if absent/unreadable."""
    path = get_projects_dir(root, team) / f"{project_key_to_slug(key)}.json"
    rec = read_json_readonly(path)
    return rec if isinstance(rec, dict) else None


def list_project_records(root, team):
    """Read all project profiles from the projects/ dir. Returns a list of dicts."""
    pdir = get_projects_dir(root, team)
    if not pdir.exists():
        return []
    out = []
    for path in sorted(pdir.glob("*.json")):
        rec = read_json_readonly(path)
        if isinstance(rec, dict) and rec.get("key"):
            out.append(rec)
    return out


def remove_project_record(root, team, key):
    """Remove a project profile file. Best-effort, never raises."""
    path = get_projects_dir(root, team) / f"{project_key_to_slug(key)}.json"
    try:
        path.unlink()
    except OSError:
        pass


def write_project_record(root, team, key, timeout=10, **fields):
    """Merge-upsert a project profile under a BLOCKING lock.

    Uses ``file_lock`` (never ``file_lock_optional``) so two simultaneous first-creates
    for the same key serialize — neither silently clobbers the other. Raises
    ``CommsError`` on lock failure (the caller retries).

    Merge rules: an omitted field is unchanged; an explicit ``""`` value clears the
    field from the record; any other value overwrites. ``created_by``/``created_at`` are
    stamped on the first create only and never overwritten. ``updated_by``/``updated_at``
    are always stamped from the SAME ``now_timestamp()`` call — on a first create
    ``created_at == updated_at``, which lets callers detect a fresh registration.
    ``status`` is exempt from the ``""``-clears contract: the enum rejects ``""`` before
    the clear path, and "active" is always a meaningful default with nothing to clear to.
    """
    pdir = get_projects_dir(root, team)
    pdir.mkdir(parents=True, exist_ok=True)
    record_path = pdir / f"{project_key_to_slug(key)}.json"
    created_by = fields.pop("created_by", None)
    updated_by = fields.pop("updated_by", None)
    _lock_acquired = False
    try:
        with file_lock(record_path, timeout=timeout):
            _lock_acquired = True
            record = read_json_readonly(record_path)
            ts = now_timestamp()
            if not isinstance(record, dict):
                record = {
                    "key": key,
                    "created_by": created_by,
                    "created_at": ts,
                }
            for fname, fval in fields.items():
                if fval == "":
                    record.pop(fname, None)
                elif fval is not None:
                    record[fname] = fval
            record["updated_by"] = updated_by
            record["updated_at"] = ts
            write_json_atomic(record_path, record)
            return record
    except CommsError:
        if not _lock_acquired:
            raise CommsError(
                f"Project {key!r} stayed locked for {timeout}s (concurrent write contention). "
                f"Retry the call."
            )
        raise


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


def write_bytes_atomic(path, data):
    """Write binary data via a temp file + os.replace (atomic on the same volume)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# A claim marker (see _claim_if_dead) lives microseconds; one older than this is an orphan
# left by a stealer killed mid-claim, and is reclaimed so a dead holder's lock can't get stuck.
CLAIM_STALE_SECONDS = 30


def _write_lock_pid(lock_dir):
    """Best-effort: record the holder's pid AND host inside the lock dir, so a contender can
    tell a DEAD-on-THIS-HOST holder (steal-able) from a slow-but-alive one, OR a holder on a
    DIFFERENT host (whose pid is meaningless locally — never steal). A failed/absent/foreign
    write reads back as 'unknown/foreign' → never stolen (fail toward the live holder)."""
    try:
        (lock_dir / "pid").write_text(f"{os.getpid()}\n{socket.gethostname()}")
    except OSError:
        pass


def _release_lock(lock_dir):
    """Remove a lock dir we own — the pid file first (the dir is otherwise non-empty)."""
    try:
        (lock_dir / "pid").unlink()
    except OSError:
        pass
    try:
        lock_dir.rmdir()
    except OSError:
        pass


def _claim_if_dead(lock_dir):
    """If the lock's holder is VERIFIED dead, atomically claim + remove the stale dir and
    return True (the caller then re-mkdir's a fresh lock). Return False — never steal — when
    the holder is alive, UNDETERMINED (``_pid_alive`` → None: absence of proof of death is not
    proof of death), the pid file is missing/unreadable, OR another contender won the claim.

    The exclusive claim is a ``mkdir`` of a sibling ``<lock>.claim`` marker — the SAME atomic
    primitive the lock itself uses, so of N concurrent stealers EXACTLY ONE wins the mkdir
    (the rest get ``FileExistsError`` → don't steal). (A naive rmdir+mkdir would let two
    stealers both 'win' and re-import the very two-writers race A-7 closes; and an
    ``os.replace`` rename of the lock DIRECTORY proved non-exclusive under concurrency on
    Windows — empirically 4/8 stealers 'won' — so mkdir is the reliable choice.) After winning
    the claim we RE-READ + RE-VERIFY the holder pid under the claim (NOT just `exists()`): the
    top-of-function evidence is stale once we've blocked on the claim, and an earlier stealer may
    have removed the dead lock and re-acquired a LIVE one — indistinguishable by existence alone —
    so a stale `exists()` check would rmtree a live holder's lock (the very A-7 two-writers race).
    The claim's mutual exclusion + the present dead dir (which blocks any re-acquire mkdir) make
    that re-read trustworthy. Only a still-present, same-host, VERIFIED-dead lock is removed; then
    the claim marker is dropped.

    Liveness is HOST-GATED (mirrors ``is_channel_alive``): ``_pid_alive`` is purely local, so a
    holder on a DIFFERENT host is never stolen (its pid is meaningless here) — a dead remote
    holder's lock is recovered only manually, bounded by the timeout raise/drop; strictly safer
    than stealing a live remote. And the ``.claim`` marker is AGE-GATED: one older than
    ``CLAIM_STALE_SECONDS`` (a stealer killed mid-claim) is reclaimed, so a dead holder's lock
    can't get permanently stuck behind a dead claim."""
    try:
        lines = (lock_dir / "pid").read_text().splitlines()
        pid = int(lines[0].strip())
    except (OSError, ValueError, IndexError):
        return False                          # missing/unreadable pid → unknown → don't steal
    host = lines[1].strip() if len(lines) > 1 else ""
    if host != socket.gethostname():
        return False                          # holder on a DIFFERENT host → local pid is meaningless
    if _pid_alive(pid) is not False:
        return False                          # alive (True) or undetermined (None) → don't steal
    claim = Path(str(lock_dir) + ".claim")
    try:
        claim.mkdir(parents=False, exist_ok=False)   # atomic exclusive — exactly one winner
    except OSError:
        # Another stealer holds the claim — normally micro-lived. If it's STALE (a stealer was
        # killed between mkdir and its finally), reclaim it: rmdir + one retry. Two concurrent
        # cleaners race the fresh mkdir → exactly one wins (the same exclusive primitive), so a
        # dead holder's lock can't get permanently stuck behind a dead claim marker.
        try:
            stale = (time.time() - claim.stat().st_mtime) > CLAIM_STALE_SECONDS
        except OSError:
            return False                      # claim vanished under us → let the caller loop
        if not stale:
            return False
        try:
            claim.rmdir()
        except OSError:
            pass
        try:
            claim.mkdir(parents=False, exist_ok=False)
        except OSError:
            return False                      # another cleaner won the fresh claim → don't steal
    try:
        # FRESH evidence under the claim. The pid/host read at the top is now STALE: between that
        # read and our winning the claim, an EARLIER stealer could have removed the dead lock AND
        # re-acquired a fresh, LIVE lock (its own pid inside) — and `lock_dir.exists()` alone can't
        # tell that live lock from the old dead one (existence is identical), so a blind rmtree
        # would destroy a LIVE holder's lock = the two-writers A-7 corruption this very defense
        # exists to prevent (ledger #9: never act on expired evidence). The held claim is the mutual
        # exclusion that makes this re-read trustworthy: while we hold it no other stealer can
        # remove+re-acquire, and the still-present dead dir makes every acquirer's
        # mkdir(exist_ok=False) fail — so the pid file is STABLE under us. Re-verify same-host +
        # VERIFIED-dead. (The SECOND _pid_alive probe is DELIBERATE, not redundant with the top one
        # — it's the only fresh-evidence check; do not "optimize" it away or the hole reopens.) Any
        # other outcome (missing/unreadable/changed/alive/undetermined/foreign) → drop, don't steal.
        try:
            if not lock_dir.exists():
                return False                  # already removed by an earlier winner
            relines = (lock_dir / "pid").read_text().splitlines()
            repid = int(relines[0].strip())
            rehost = relines[1].strip() if len(relines) > 1 else ""
        except (OSError, ValueError, IndexError):
            return False                      # lock vanished / pid now unreadable → don't steal
        if rehost != socket.gethostname() or _pid_alive(repid) is not False:
            return False                      # re-acquired (alive) / undetermined / foreign → don't steal
        shutil.rmtree(lock_dir, ignore_errors=True)
        # True means: claim won + removal ATTEMPTED — NOT "verified gone". The exclusive re-mkdir in
        # file_lock is the actual arbiter, and a lingered rmtree (a concurrent reader's open handle on
        # Windows) is DELIBERATELY routed through file_lock's FileExistsError-retry branch, which
        # resets the clock and recovers. DO NOT "harden" this into a verify-removal
        # (`if lock_dir.exists(): return False`) without first reading file_lock's raise-on-False
        # branch: a False there RAISES (no retry), so verify-removal would convert that recoverable
        # transient into a surfaced CommsError — removing the working recovery this honest-looking
        # `return True` actually relies on. (The rule-20 "True should bind its reason" itch lives
        # here as this plaque, not as code: the reason is "attempted under the claim", by design.)
        return True
    finally:
        try:
            claim.rmdir()
        except OSError:
            pass


@contextmanager
def file_lock(lock_path, timeout=10):
    """Cross-platform lock via mkdir (atomic on all OSes). Raises CommsError on timeout.

    On timeout the lock is STOLEN only from a VERIFIED-DEAD holder (pid recorded in the lock
    dir; ``_claim_if_dead`` does the atomic claim). A slow-but-alive holder is NEVER stolen
    from — the contention surfaces as a raise instead (a surfaced error beats two writers
    clobbering each other; audit A-7). A holder that hasn't written its pid yet is treated as
    unknown (never stolen), bounded by ``timeout`` (we raise, never wait forever or steal blind)."""
    lock_dir = Path(str(lock_path) + ".lock")
    start = time.time()
    while True:
        try:
            lock_dir.mkdir(parents=False, exist_ok=False)
            _write_lock_pid(lock_dir)
            break
        except FileExistsError:
            if time.time() - start > timeout:
                if _claim_if_dead(lock_dir):
                    try:
                        lock_dir.mkdir(parents=False, exist_ok=False)
                        _write_lock_pid(lock_dir)
                        break
                    except FileExistsError:
                        start = time.time()       # someone re-acquired post-claim; reset + retry
                        time.sleep(0.05)
                        continue
                raise CommsError(
                    f"Could not acquire lock on {lock_path.name} after {timeout}s "
                    f"(holder alive or undetermined — not stolen)."
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        _release_lock(lock_dir)


@contextmanager
def file_lock_optional(lock_path, timeout=2):
    """Best-effort lock that NEVER raises. Yields True if acquired, else False (caller drops).

    Same dead-holder steal discipline as ``file_lock`` (A-7) — an alive/undetermined holder is
    never stolen from; here a non-acquire simply yields False instead of raising, preserving
    the droppable/never-raise contract the transcript/reaction/deletion tees depend on."""
    lock_dir = Path(str(lock_path) + ".lock")
    start = time.time()
    acquired = False
    while True:
        try:
            lock_dir.mkdir(parents=False, exist_ok=False)
            _write_lock_pid(lock_dir)
            acquired = True
            break
        except FileExistsError:
            if time.time() - start > timeout:
                if _claim_if_dead(lock_dir):
                    try:
                        lock_dir.mkdir(parents=False, exist_ok=False)
                        _write_lock_pid(lock_dir)
                        acquired = True
                    except FileExistsError:
                        pass                      # re-acquired by someone else → drop
                break                             # alive/undetermined/lost → drop (never raise)
            time.sleep(0.05)
        except OSError:
            break
    try:
        yield acquired
    finally:
        if acquired:
            _release_lock(lock_dir)


# ── Deletion / tombstone helpers ──────────────────────────────────────────────────
#
# A "delete" of a message is a TOMBSTONE: the record keeps its id/from/to/group/
# reply_to/post_type/priority/kind, the body is replaced with DELETED_MARKER, and two
# flags are added. Tombstoning (not removal) keeps reply_to citations + group/thread
# continuity intact. The same id can live in several stores (a group post is copied
# into messages.json, every member's inbox, and the transcript) — tombstone each
# durable store under the SAME lock the normal writers use. Teammate removal is a hard
# delete of the registry + inbox files (their authored messages elsewhere stay
# attributed).

DELETED_MARKER = "— message deleted —"


def tombstone_fields(deleted_by):
    """The in-place mutation for a deleted message (single source of truth)."""
    return {"message": DELETED_MARKER, "deleted": True, "deleted_by": deleted_by}


def _apply_tombstone(records, msg_id, deleted_by):
    """Tombstone every record with a matching id in a list (mutates in place). -> found?"""
    found = False
    for rec in records:
        if isinstance(rec, dict) and rec.get("id") == msg_id:
            rec.update(tombstone_fields(deleted_by))
            found = True
    return found


def tombstone_in_inbox(root, team, member, msg_id, deleted_by):
    """Tombstone a message (by id) in a member's inbox — BOTH ``_unread.json`` and
    ``_read.json`` under the UNREAD file's lock (mirrors ``_handle_ack``, which protects
    ``_read.json`` with the unread lock; locking each file separately would race ack).
    Lock-then-read so ``read_json_safe``'s reset-corrupt-to-[] can't clobber a concurrent
    partial write. Writes a file only when it actually changed. -> found in either?"""
    inboxes_dir = get_inboxes_dir(root, team)
    unread_file = inboxes_dir / f"{member}_unread.json"
    read_file = inboxes_dir / f"{member}_read.json"
    found = False
    with file_lock(unread_file):
        for f in (unread_file, read_file):
            msgs = read_json_safe(f)
            if _apply_tombstone(msgs, msg_id, deleted_by):
                write_json_atomic(f, msgs)
                found = True
    return found


def tombstone_in_group_messages(root, team, group, msg_id, deleted_by):
    """Tombstone a message (by id) in ``groups/<group>/messages.json`` under its lock."""
    messages_file = get_group_dir(root, team, group) / "messages.json"
    with file_lock(messages_file):
        messages = read_group_messages(root, team, group)
        if _apply_tombstone(messages, msg_id, deleted_by):
            write_json_atomic(messages_file, messages)
            return True
    return False


def remove_group_messages_from_inbox(root, team, member, sigil):
    """Hard-remove a member's copies of a group's messages by the GROUP PREDICATE (any record
    with ``group == sigil``), not a pre-snapshotted id-set — used by whole-group delete.

    The predicate catches a fan-out copy that landed AFTER an id snapshot but before this
    purge, shrinking the A-5 race window. It is NOT a full fix: a ``send_group`` that passed
    its meta-exists check before the dir ``rmtree`` but whose fan-out lands AFTER this purge can
    still orphan an inbox copy — closing that needs cross-store atomicity the v0.7.0 plan
    rejected. The residual is accepted as eventually-consistent (the dashboard omits the absent
    group on reload; ``resolve_message``'s group-dir fallback finds nothing). Both inbox files
    under the unread lock; writes only on change."""
    inboxes_dir = get_inboxes_dir(root, team)
    unread_file = inboxes_dir / f"{member}_unread.json"
    read_file = inboxes_dir / f"{member}_read.json"
    with file_lock(unread_file):
        for f in (unread_file, read_file):
            msgs = read_json_safe(f)
            kept = [m for m in msgs if not (isinstance(m, dict) and m.get("group") == sigil)]
            if len(kept) != len(msgs):
                write_json_atomic(f, kept)


def remove_agent(root, team, name, timeout=2):
    """Locked, VERIFIED hard-delete of an agent's registry record + inbox files (audit I3).

    Returns the list of file paths that could NOT be removed (empty = full success) instead of
    swallowing every OSError — the old best-effort unlink let a Windows sharing violation
    (e.g. a concurrent heartbeat write) silently no-op the deletion while the caller reported
    unconditional success. The registry record is unlinked under its own ``file_lock`` (so a
    concurrent writer can never be mid-write when we check); the two inbox files share the
    unread file's lock, same as ``tombstone_in_inbox``. A short ``timeout`` (not the 10s
    default) keeps a genuinely-contended delete from hanging — a real delete should acquire
    near-instantly; a held lock past that IS the reportable failure.
    """
    agents_dir = get_agents_dir(root, team)
    inboxes_dir = get_inboxes_dir(root, team)
    record_path = agents_dir / f"{name}.json"
    unread_file = inboxes_dir / f"{name}_unread.json"
    read_file = inboxes_dir / f"{name}_read.json"
    failed = []

    def _unlink(path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass  # already gone — not a failure, the goal (absent) is already met
        except OSError:
            failed.append(str(path))

    try:
        with file_lock(record_path, timeout=timeout):
            _unlink(record_path)
    except CommsError:
        failed.append(str(record_path))  # lock contended past timeout — reported, not swallowed

    try:
        with file_lock(unread_file, timeout=timeout):
            _unlink(unread_file)
            _unlink(read_file)
    except CommsError:
        failed.append(str(unread_file))
        failed.append(str(read_file))

    return failed


def strip_member_from_groups(root, team, name):
    """Remove ``name`` from ``members[]`` of every group's meta (under each meta's lock).
    Leaves their authored posts attributed. -> list of group names they were removed from."""
    groups_dir = get_groups_dir(root, team)
    removed_from = []
    if not groups_dir.exists():
        return removed_from
    for gdir in sorted(d for d in groups_dir.iterdir() if d.is_dir()):
        group = gdir.name
        with file_lock(gdir / "meta.json"):
            meta = read_group_meta(root, team, group)
            if not isinstance(meta, dict):
                continue
            members = meta.get("members", [])
            if name in members:
                meta["members"] = [m for m in members if m != name]
                write_group_meta(root, team, group, meta)
                removed_from.append(group)
    return removed_from


def write_agent_record(root, team, name, timeout=5, bump_epoch=False, **fields):
    """Field-level merge of ``fields`` into ``agents/<name>.json`` under a lock.

    Only provided keys are overwritten; existing keys are preserved, so the
    register-owned ``type`` and the channel-owned ``pid``/``channel``/
    ``lastHeartbeat`` coexist. Returns the MERGED RECORD DICT just written (never empty — it
    always carries at least ``name``, so it's truthy) on success, or False on failure. Every
    caller does a plain truthiness check (``if not write_agent_record(...)``), so this is a
    drop-in-compatible upgrade — but a caller that needs a freshly-written field's value (e.g.
    an atomically-computed epoch) can now read it straight off the return, with NO separate
    read-back needed (a read-back is a fresh race window: see ``bump_epoch`` below).

    Hardening: if the record file *exists* but currently reads as None (a
    concurrent mid-write), skip this write instead of clobbering ``type``.

    ``bump_epoch=True`` (WP-19, register-time only) computes ``fields["epoch"]`` from the
    CURRENT on-disk record's epoch (previous + 1, absent → 1) under THIS SAME lock/read — the
    only race-free way to hand out a monotonic epoch when two callers might register the same
    name at once (any literal ``epoch`` passed in ``fields`` is overridden). A caller MUST take
    its own epoch from this call's RETURN VALUE, never from a subsequent read-back — a
    read-back is unlocked and can observe a LATER competing register's epoch instead of its
    own (the exact flap this WP's epoch tie-break exists to kill; gate finding on WP-19).
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
        if bump_epoch:
            fields = dict(fields)
            fields["epoch"] = (record.get("epoch") or 0) + 1
        record["name"] = name
        record.update(fields)
        write_json_atomic(record_path, record)
        return record


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


# Default heartbeat-freshness window (seconds) — single source of truth shared by
# is_channel_alive's cross-host fallback and the WP-19 flap-kill decision
# (compute_heartbeat_permit in channel.py), so both agree on what "fresh" means.
HEARTBEAT_STALENESS_SECONDS = 30


def heartbeat_fresh(hb, now, staleness=HEARTBEAT_STALENESS_SECONDS):
    """True if a ``lastHeartbeat`` timestamp string is within ``staleness`` seconds of ``now``
    (a real ``datetime`` — injected, never read internally, so callers can test this pure)."""
    if not hb:
        return False
    try:
        last = datetime.strptime(hb, TIMESTAMP_FMT)
    except (ValueError, TypeError):
        return False
    return (now - last).total_seconds() <= staleness


def is_channel_alive(record, staleness=HEARTBEAT_STALENESS_SECONDS, pid_check=True):
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
    return heartbeat_fresh(record.get("lastHeartbeat"), datetime.now(), staleness)


# ── Human-as-teammate + observability transcript ────────────────────────────────
#
# The dashboard (teammate_dashboard) registers the human operator as a first-class
# teammate so agents can see, DM, and invite them. A human record carries
# type="human" and a "presence" field, but deliberately NO "pid"/"channel": it must
# never be treated as a wakeable channel (is_channel_alive returns False with no
# "channel" key) and never trips the register-time collision guard (keyed on pid +
# is_channel_alive). The human is reachable by flat name like any other teammate.


# Presence counts as "online" only while presenceAt is within this many seconds of now (B1).
# Back-compat: a record with NO presenceAt key (pre-WP-21) trusts the flat `presence` flag as-is
# — see human_presence_online.
PRESENCE_STALENESS_SECONDS = 60


def human_presence_online(record, staleness=PRESENCE_STALENESS_SECONDS):
    """True if a human record should render as online (B1: a flat presence flag, set once at
    dashboard start and only ever flipped by a GRACEFUL shutdown, stays "online" forever if the
    terminal is killed). ``presence == "online"`` counts only while ``presenceAt`` is fresh.

    Back-compat (mandatory): a record with NO ``presenceAt`` key trusts the flag as-is — a
    pre-WP-21 record must not flip to "away" the moment this ships.
    """
    if not record or record.get("presence") != "online":
        return False
    presence_at = record.get("presenceAt")
    if not presence_at:
        return True  # back-compat: no presenceAt yet — trust the flag
    return heartbeat_fresh(presence_at, datetime.now(), staleness)


def register_human(root, team, name):
    """Register a human operator as a teammate record (type="human") with an inbox.

    Additive over write_agent_record's field-merge: no pid, no channel — so the
    human is never a wakeable channel and never collides with a live agent of the
    same name. Idempotent (re-register just refreshes presence/host).

    G2: raises if an existing record's name matches ``name`` case-insensitively but differs
    in case — an exact re-register is unaffected (idempotent).
    """
    validate_agent_name(name)
    existing_variant = find_case_variant(root, team, name)
    if existing_variant:
        raise CommsError(
            f"A teammate is already registered as {existing_variant!r} — register with that "
            f"exact spelling, or pick a distinct name."
        )
    ensure_inbox(get_inboxes_dir(root, team), name)
    write_agent_record(
        root, team, name,
        type="human", host=socket.gethostname(),
        startedAt=now_timestamp(), presence="online",
        # B1: presenceAt + dashboard_pid let presence be verified as fresh (human_presence_online)
        # instead of trusted as a flat flag forever, and let shutdown's clobber guard (below)
        # identify which dashboard process actually owns this "online" stamp.
        presenceAt=now_timestamp(), dashboard_pid=os.getpid(),
    )


def set_human_presence(root, team, name, state, owner_pid=None):
    """Merge a presence marker ("online"/"away") into a human's record. Best-effort.

    ``owner_pid`` (B1 clobber guard): when given, the write is SKIPPED unless the record's
    CURRENT ``dashboard_pid`` matches it — read-before-write, same best-effort discipline
    ``write_agent_record`` already has. Without this, a SECOND dashboard's shutdown could mark
    the FIRST (still-live) operator away just because they share a human name.
    """
    if owner_pid is not None:
        record = read_agent_record(root, team, name)
        if not record or record.get("dashboard_pid") != owner_pid:
            return
    write_agent_record(root, team, name, presence=state)


def group_read_positions(root, team, group, members):
    """Read-only read-receipt inference: each member's furthest-acked group-message id.

    Acked messages already move to ``<member>_read.json`` (no new write path, no ack
    change). For each member, returns the max id among their read messages tagged with
    this group's sigil — an ack/seen upper bound (gaps possible), groups-only. Returns
    ``{member: id_or_None}``; reads are non-destructive.
    """
    sigil = group if str(group).startswith("#") else f"#{group}"
    inboxes_dir = get_inboxes_dir(root, team)
    positions = {}
    for member in members:
        msgs = read_json_readonly(inboxes_dir / f"{member}_read.json") or []
        ids = [m.get("id") for m in msgs
               if isinstance(m, dict) and m.get("group") == sigil and m.get("id")]
        positions[member] = max(ids) if ids else None
    return positions


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


def _window(records, limit, oldest_first):
    """Apply a ``limit`` window to chronologically-ordered ``records``.

    ``oldest_first=False`` (default) keeps the NEWEST ``limit`` (``records[-limit:]``)
    — a tail view, correct for a fresh load that wants recent history.

    ``oldest_first=True`` keeps the OLDEST ``limit`` — forward pagination from a
    cursor. A caller that then advances its cursor to the LAST returned id resumes
    exactly there next poll, so a burst larger than ``limit`` drains across polls with
    **no record ever skipped** (the bug when a tail-slice was paired with a
    newest-id cursor advance). To guarantee the cursor strictly advances, the cut is
    extended to swallow any id-collision group straddling the boundary — never split
    records sharing one id across two pages, or the cursor could stall on them.
    (The one unhandled case — MORE than ``limit`` records sharing a single id — is
    accepted as unreachable: ids are per-write timestamps and the OS clock granularity
    (~1ms on Windows) makes hundreds in one microsecond physically impossible.)
    """
    if not (limit and limit > 0 and len(records) > limit):
        return records
    if not oldest_first:
        return records[-limit:]
    cut = limit
    boundary_id = records[cut - 1].get("id")
    while cut < len(records) and records[cut].get("id") == boundary_id:
        cut += 1
    return records[:cut]


def read_jsonl_tail(path, n_records, since=None, chunk_size=8192):
    """Return ~the last ``n_records`` VALID JSON records of an NDJSON file WITHOUT parsing the
    whole file — seek from EOF and read backward in chunks until enough complete records are
    assembled (or BOF). The C-1/C-2 read-cost relief: a fresh dashboard tail load and the
    react/resolve scans stop streaming the entire log.

    BINARY mode is mandatory: text-mode ``tell``/``seek`` lies about byte positions under CRLF
    translation. Lines are assembled from raw bytes (so a multi-byte UTF-8 char or a record
    straddling a chunk boundary is rejoined before decode), then decoded + ``.strip()``'d
    (dropping a Windows ``\\r`` and surrounding whitespace) — reproducing the line readers'
    semantics, so 'newest N' is N PARSEABLE records (blank/garbage lines skipped), NOT N raw
    lines, and the result is byte-identical to ``read_transcript(...)[-N:]``. With ``since``,
    keeps only records whose id is ``>= since``. Missing/empty file → [].

    Documented divergence (the ONLY behavioral difference from the full text-mode reader): this
    reader is MORE TOLERANT of undecodable bytes — it decodes with ``errors='replace'`` and skips
    unparseable lines, where the full reader would RAISE ``UnicodeDecodeError`` on invalid UTF-8.
    So the 'byte-identical' claim holds for every well-formed log; only a CORRUPT file diverges,
    and in the better direction (skip, never raise)."""
    if not (n_records and n_records > 0):
        return []
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size == 0:
        return []
    records = []
    try:
        with open(path, "rb") as f:
            leftover = b""        # the (possibly partial) leading line carried to the next chunk
            pos = size
            while pos > 0:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                buf = f.read(read_size) + leftover
                parts = buf.split(b"\n")
                if pos > 0:
                    leftover = parts[0]        # window started mid-line — complete it next chunk
                    complete = parts[1:]
                else:
                    complete = parts           # at BOF the leading line is complete too
                batch = []
                for raw in complete:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if since and rec.get("id", "") < since:
                        continue
                    batch.append(rec)
                records = batch + records       # prepend (we read older bytes each iteration)
                if len(records) >= n_records:
                    break
    except OSError:
        return []
    return records[-n_records:] if len(records) > n_records else records


def read_transcript(root, team=None, since=None, limit=200, oldest_first=False):
    """Read the global NDJSON transcript non-destructively, ``limit``-bounded.

    With ``since`` set, returns records whose id is ``>= since`` (the dashboard dedupes
    by id, so a boundary record repeating across polls is harmless). ``oldest_first``
    selects the windowing when more than ``limit`` records match: False keeps the newest
    ``limit`` (tail view); True keeps the oldest ``limit`` for forward pagination — the
    caller (e.g. the dashboard poll) sets this when walking a cursor so a >``limit``
    burst is paged out instead of skipped. Missing file → []; bad lines skipped.
    """
    path = get_transcript_file(root, team)
    # Fast path (C-1/C-2): a pure newest-N tail (no cursor, no forward-pagination) reads only
    # the file's tail via read_jsonl_tail instead of parsing the whole file. Byte-identical to
    # the full-read + records[-limit:] below.
    if not oldest_first and since is None and limit and limit > 0:
        return read_jsonl_tail(path, limit)
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
    return _window(records, limit, oldest_first)


# ── Transcript byte cursor (P3, audit firehose/N-1) ─────────────────────────────
#
# The dashboard's records stream walks the transcript by a BYTE offset, not an id, so a live
# cursored poll reads only the bytes appended since last poll (vs the O(file) full scan an id
# `since` forces). The opaque cursor is "<offset>|<generation>": `offset` is a byte position,
# `generation` is a crc32 of the file's first line — append-stable (byte 0 never moves on the
# append-only transcript), changing only on recreation, so a stale offset (truncation/recreation)
# is detected and the reader transparently re-tails. This ALSO fixes N-1: an out-of-order tee
# lands at EOF, so byte-streaming emits it where the id cursor (id >= since) skipped it.

GEN_BOUND = 64 * 1024   # bytes scanned for the first-line terminator when computing a generation


def _transcript_generation(path, size):
    """crc32 (as a decimal string) of the transcript's first line — bytes ``0..first \\n`` — or
    ``""`` when that line isn't yet terminated within ``GEN_BOUND`` (a cold-start torn head, an
    empty file, or a pathological mega-line). ``""`` is the fresh/unknown sentinel: it ALWAYS
    forces a re-tail, never a validation against a half-written first line. Append-stable, so an
    append never changes it; recreation does. ``size`` is the caller's single stat (no re-stat)."""
    try:
        with open(path, "rb") as f:
            head = f.read(min(size, GEN_BOUND))   # absolute position 0 on a fresh handle
    except OSError:
        return ""
    nl0 = head.find(b"\n")
    return "" if nl0 == -1 else str(zlib.crc32(head[:nl0]))


def transcript_tail_and_cursor(path, limit=200):
    """Fresh-load tail + the byte cursor to resume live-streaming from ~EOF. Returns
    ``(records, offset, generation)``.

    Stat-THEN-tail: the offset is the file size captured BEFORE the tail read, so it is ``<=`` the
    size the tail observed (the transcript only grows). A record appended BETWEEN the two
    observations is therefore both shown by the tail AND re-streamed by the next cursored poll
    (the dashboard folds the repeat by id) — never lost (the tail-then-stat order would drop it).
    The offset sits at ~EOF, so old pre-tail history is not re-streamed: subsequent polls stream
    only NEW appends, preserving today's 'fresh shows newest N, then live-stream' behavior."""
    try:
        offset = os.path.getsize(path)
    except OSError:
        return ([], 0, "")                        # no file (e.g. TEAMMATE_TRANSCRIPT=0) → cursor "0|"
    gen = _transcript_generation(path, offset)
    return (read_jsonl_tail(path, limit), offset, gen)


def read_transcript_after(path, offset, generation, limit=200):
    """Forward byte-cursor read of the append-only transcript. Returns
    ``(records, new_offset, new_generation, reset)``.

    ``records`` are the NEW complete records since ``offset`` (oldest-first), capped at ``limit``;
    ``new_offset``/``new_generation`` form the next cursor. ``reset`` is True when the offset was
    invalidated (file missing / recreated / truncated) — then ``records`` is instead the newest-
    ``limit`` TAIL (a transparent re-tail; the dashboard's id-dedup folds any re-served record).

    ONE stat per call (NIT-2); the generation read and the increment read use SEPARATE opens, each
    with an absolute seek so no file position is ever inherited (NIT-1). BINARY mode (text seek/tell
    lies under CRLF). ``new_offset`` is advanced by RAW newline geometry — every consumed byte,
    including blank/garbage lines and each ``\\n``, is counted; it is NEVER reconstructed from the
    parsed/stripped records (which would desync on the first blank/garbage/CRLF/multibyte line).
    Offsets always land on a ``\\n``-boundary, so the next seek can't split a line or a codepoint.
    A partial final line (no ``\\n`` yet) is left UNCONSUMED — torn-tail safe."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ([], 0, "", True)                  # no file → empty re-tail; caller mints "0|"
    cur_gen = _transcript_generation(path, size)  # open #1 (bounded first-line read)
    if generation == "" or cur_gen == "" or generation != cur_gen or not (0 <= offset <= size):
        # fresh/unknown sentinel, empty/torn head, recreation, truncation, OR a malformed
        # out-of-range offset (a negative offset would raise on seek and wedge the stream — a
        # hand-crafted cursor must still re-tail cleanly, never livelock) → transparent re-tail.
        return (read_jsonl_tail(path, limit), size, cur_gen, True)
    try:
        with open(path, "rb") as f:               # open #2 — absolute seek, never an inherited pos
            f.seek(offset)
            buf = f.read(size - offset)            # bounded by THIS poll's stat (a later append
    except OSError:                                # is caught next poll, keeping offset math exact)
        return ([], offset, generation, False)    # transient read error → no progress, retry
    records = []
    consumed = 0          # raw bytes consumed since `offset` (drives new_offset)
    count = 0             # complete RECORDS emitted (drives the limit cap; blank/garbage excluded)
    start = 0
    while True:
        nl = buf.find(b"\n", start)
        if nl == -1:
            break                                 # remaining bytes are a partial final line — defer
        line = buf[start:nl].decode("utf-8", "replace").strip()   # .strip() drops a CRLF's \r + ws
        if line:
            try:
                records.append(json.loads(line))
                count += 1
            except (json.JSONDecodeError, ValueError):
                pass                              # garbage line: skip the record but STILL advance
        consumed = nl + 1                         # past this line + its \n (blank/garbage included)
        start = consumed
        if limit and count >= limit:
            break                                 # burst cap: the rest pages out next poll (A-1)
    return (records, offset + consumed, cur_gen, False)


# ── Reactions ───────────────────────────────────────────────────────────────────
#
# Emoji reactions target a message BY ID, so the same store covers DMs and group posts
# without mutating the append-only records. Their own NDJSON log, ALWAYS written (NOT
# gated by TEAMMATE_TRANSCRIPT — reactions are a feature, not observability). A reaction
# wakes ONLY the author of the reacted-to message (its `target_from`), via the channel
# watcher — never the group, never the reactor, never on `remove`.

# Fixed basic emoji-reaction set (name → glyph). Single source — imported by tools.py
# (as _REACTIONS) and channel.py (to render glyphs in a reaction wake).
REACTION_EMOJI = {"thumbsup": "👍", "rofl": "🤣", "smile": "😄", "cry": "😢", "100": "💯", "fire": "🔥"}


def get_reactions_file(root, team=None):
    """``<root>/TeammateComms/[<team>/]reactions.jsonl`` — the append-only reaction log."""
    base = Path(root) / "TeammateComms"
    if team:
        base = base / team
    return base / "reactions.jsonl"


def append_reaction(root, team, record, timeout=5):
    """Append one reaction event under a BLOCKING lock — reactions are a feature, not
    observability (unlike ``append_transcript``), so a drop is feature-data loss (no wake,
    no chip, ever). Blocks up to ``timeout`` rather than skipping under contention; a
    genuine lock failure raises an actionable ``CommsError`` so the caller surfaces it
    (and retries — reaction adds fold idempotently in ``aggregate_reactions``) instead of
    losing it silently. ``timeout`` is short (the server request loop is single-threaded,
    audit A-6 — a long block stalls every tool call) and injectable so tests don't wait
    the default. Must not be called while holding any ``file_lock`` (it is not reentrant).

    The event ``id`` is stamped HERE, under the lock, and written into ``record`` in place
    (the caller reads it back). Stamping inside the lock makes file order == id order even
    when two writers contend: a caller-stamped id + a blocking wait could otherwise commit a
    LOWER id AFTER a watcher/dashboard cursor already advanced past it (since=cursor then
    excludes it forever → silent missed wake). Stamp-on-write closes that race at the source."""
    path = get_reactions_file(root, team)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with file_lock(path, timeout=timeout):
            record["id"] = now_timestamp()
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except CommsError:
        raise CommsError(
            f"Reaction not recorded: reactions.jsonl stayed locked for {timeout}s "
            f"(transient write contention). Nothing was lost — try the reaction again."
        )


def read_reactions(root, team=None, since=None, limit=None, oldest_first=False):
    """Read reaction events (non-destructive); optional ``id >= since`` + ``limit`` window.

    ``oldest_first`` selects the window when more than ``limit`` match (see ``_window``):
    False = newest ``limit`` (tail/seed view); True = oldest ``limit`` for forward
    pagination from a cursor (the dashboard poll and the channel reaction-wake driver set
    this so a burst pages out instead of scrolling past the tail)."""
    path = get_reactions_file(root, team)
    if not oldest_first and since is None and limit and limit > 0:
        return read_jsonl_tail(path, limit)       # pure newest-N tail — read only the file's tail
    out = []
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
                out.append(rec)
    except (FileNotFoundError, OSError):
        return []
    return _window(out, limit, oldest_first)


def aggregate_reactions(events):
    """Fold chronological add/remove events → ``{target: {emoji: [reactors sorted]}}``
    (last op per (target, emoji, reactor) wins; empty sets dropped)."""
    state = {}  # (target, emoji) -> set of reactors
    for e in events:
        target, emoji, who = e.get("target"), e.get("emoji"), e.get("from")
        if not (target and emoji and who):
            continue
        bucket = state.setdefault((target, emoji), set())
        if e.get("op") == "remove":
            bucket.discard(who)
        else:
            bucket.add(who)
    out = {}
    for (target, emoji), reactors in state.items():
        if reactors:
            out.setdefault(target, {})[emoji] = sorted(reactors)
    return out


# ── Deletions sub-stream ──────────────────────────────────────────────────────────
#
# An append-only event log of deletions, mirroring reactions.jsonl: its own NDJSON file
# + cursor, folded client-side by the dashboard (idempotent — keyed by target). The
# durable tombstone always lands in the inbox/group stores regardless; this stream
# exists ONLY so the dashboard can reflect a mutation live (the firehose is append-only
# and keyed by id, so an in-place tombstone never re-crosses the poll cursor). Replayed
# from the start on a fresh dashboard load — so previously-deleted messages render as
# deleted. Event shape: {id, target, kind: "message"|"group"|"teammate", by, op:"delete"}.

def get_deletions_file(root, team=None):
    """``<root>/TeammateComms/[<team>/]deletions.jsonl`` — append-only deletion events."""
    base = Path(root) / "TeammateComms"
    if team:
        base = base / team
    return base / "deletions.jsonl"


def get_deletions_set_file(root, team=None):
    """``<root>/TeammateComms/[<team>/]deletions_set.json`` — the COMPACTED deleted-set.

    A target-keyed dict ``{target: event}`` holding every deletion that has been folded out of
    the jsonl tail (see ``_compact_deletions_locked``). It is the durable BASELINE the dashboard
    unions with the live jsonl on a fresh load so a deleted message can never reappear once its
    event ages past the jsonl tail (audit C-2). Deduped by target (deletions are monotonic — op
    is always 'delete', there is no undelete)."""
    base = Path(root) / "TeammateComms"
    if team:
        base = base / team
    return base / "deletions_set.json"


# Compaction thresholds (C-2). ``RETAIN`` events stay in the live jsonl tail; older events fold
# into the set-file. The byte gate is a CHEAP getsize trip on append — it bounds the live-file
# SIZE only, it is NOT a correctness input: the dashboard's fresh-load read is the FULL live jsonl
# unioned with the full set-file, so completeness holds no matter when (or whether) the gate fires.
DELETIONS_RETAIN = 1000
DELETIONS_COMPACT_BYTES = 256 * 1024


def read_deletions_set(root, team=None):
    """Return the compacted deleted-set dict ``{target: event}`` (the C-2 baseline).

    Read-only and lock-free (write_json_atomic gives the reader old-or-new, never partial); any
    miss/corruption/non-dict → ``{}`` so the caller's ``.values()`` is always safe."""
    v = read_json_readonly(get_deletions_set_file(root, team))
    return v if isinstance(v, dict) else {}


def _compact_deletions_locked(root, team):
    """Fold all-but-the-newest ``DELETIONS_RETAIN`` deletion events into the target-keyed set-file,
    then trim the jsonl to that tail. MUST be called with the deletions.jsonl lock already held
    (so no appender interleaves the read-fold-trim). Best-effort: any failure leaves the jsonl
    intact (the set write is the durable half; the trim is pure size relief and self-heals next
    run). Ordering is load-bearing: write the SET FIRST (atomic), THEN trim the jsonl (atomic) —
    a crash/failure in between leaves the jsonl still holding everything AND the set holding the
    folded head (idempotent overlap), so a fresh load reads a superset, never a gap. The reverse
    order could drop tombstones. The mkdir-lock keys off the path STRING (a sibling .lock dir);
    os.replace swaps the FILE inode only, never the lock dir, so re-reading + replacing the jsonl
    under its own lock is deadlock-free."""
    path = get_deletions_file(root, team)
    all_events = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    all_events.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
    except (FileNotFoundError, OSError):
        return
    if len(all_events) <= DELETIONS_RETAIN:
        return
    head, tail = all_events[:-DELETIONS_RETAIN], all_events[-DELETIONS_RETAIN:]
    folded = read_deletions_set(root, team)
    for e in head:
        t = e.get("target")
        if t:
            folded[t] = e          # union by target (monotonic; last delete wins, all are deletes)
    write_json_atomic(get_deletions_set_file(root, team), folded)   # SET FIRST (atomic)
    tmp = path.with_name(path.name + ".compact.tmp")               # distinct from set-file's .tmp
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for e in tail:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, path)      # THEN trim (atomic; a lockless reader sees old-full or new-tail)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()       # orphan cleanup if os.replace raised (e.g. Windows read race)
        except OSError:
            pass


def _maybe_compact_deletions(root, team):
    """Cheap getsize gate → compact. FULLY self-contained: never raises (so neither append path
    can mislabel a successful event append as failed), best-effort. Call only with the lock held."""
    try:
        path = get_deletions_file(root, team)
        if os.path.getsize(path) > DELETIONS_COMPACT_BYTES:
            _compact_deletions_locked(root, team)
    except Exception as e:
        print(f"[teammate-comms] deletions compaction skipped: {e}", file=sys.stderr, flush=True)


def append_deletion(root, team, record, block=True, timeout=5):
    """Append one deletion event to the deletions sub-stream.

    ``block=True`` (default, used by single-message ``delete_message``): a BLOCKING lock,
    errors propagate. A lost deletion event there is *persistent* dashboard inconsistency
    — the firehose still carries the live message and nothing tombstones it on the
    dashboard even on reload — and the caller's retry is idempotent (the message stays
    resolvable, re-tombstones harmlessly).

    ``block=False`` (whole-group delete + teammate removal): best-effort, never-raise.
    Those callers destroy the group dir / agent record BEFORE this append and emit the
    event LAST by design (so a partial rmtree can't desync the dashboard); a raised append
    there would be lost permanently on retry (the re-entry guard rejects an already-gone
    group/teammate) — strictly worse. They self-heal instead: a fresh dashboard load omits
    the absent group/teammate outright. Must not be called while holding any ``file_lock``.

    The event ``id`` is stamped HERE, under whichever lock is held when the write actually
    happens (in place into ``record``) — file order == id order, so a contending writer can
    never commit a lower id after a cursor advanced past it (same race as ``append_reaction``).
    """
    if not block:
        try:
            path = get_deletions_file(root, team)
            path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock_optional(path, timeout=2) as acquired:
                if not acquired:
                    return
                record["id"] = now_timestamp()
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                _maybe_compact_deletions(root, team)   # under the lock, never raises (C-2)
        except Exception as e:
            print(f"[teammate-comms] deletion append skipped: {e}", file=sys.stderr, flush=True)
        return
    path = get_deletions_file(root, team)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with file_lock(path, timeout=timeout):
            record["id"] = now_timestamp()
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            _maybe_compact_deletions(root, team)   # under the lock, never raises (C-2)
    except CommsError:
        raise CommsError(
            f"Deletion not recorded: deletions.jsonl stayed locked for {timeout}s "
            f"(transient write contention). The message is already tombstoned — retry to "
            f"sync the dashboard."
        )


def read_deletions(root, team=None, since=None, limit=1000, oldest_first=False):
    """Read deletion events (non-destructive); optional ``id >= since`` + ``limit`` window.

    ``oldest_first`` selects the window when more than ``limit`` match (see ``_window``):
    False = newest ``limit`` (tail view); True = oldest ``limit`` for forward pagination
    from the dashboard's deletion cursor (a burst pages out instead of being skipped)."""
    path = get_deletions_file(root, team)
    if not oldest_first and since is None and limit and limit > 0:
        return read_jsonl_tail(path, limit)       # pure newest-N tail — read only the file's tail
    out = []
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
                out.append(rec)
    except (FileNotFoundError, OSError):
        return []
    return _window(out, limit, oldest_first)
