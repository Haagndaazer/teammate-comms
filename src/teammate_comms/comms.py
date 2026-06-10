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


def remove_messages_from_inbox(root, team, member, msg_ids):
    """Hard-remove messages (by id) from a member's inbox (both files) under the unread
    lock. Used by whole-group delete — the group is gone, so there's no thread to keep."""
    ids = set(msg_ids)
    if not ids:
        return
    inboxes_dir = get_inboxes_dir(root, team)
    unread_file = inboxes_dir / f"{member}_unread.json"
    read_file = inboxes_dir / f"{member}_read.json"
    with file_lock(unread_file):
        for f in (unread_file, read_file):
            msgs = read_json_safe(f)
            kept = [m for m in msgs if not (isinstance(m, dict) and m.get("id") in ids)]
            if len(kept) != len(msgs):
                write_json_atomic(f, kept)


def remove_agent(root, team, name):
    """Hard-delete an agent's registry record + inbox files (best-effort, never raises)."""
    agents_dir = get_agents_dir(root, team)
    inboxes_dir = get_inboxes_dir(root, team)
    for path in (agents_dir / f"{name}.json",
                 inboxes_dir / f"{name}_unread.json",
                 inboxes_dir / f"{name}_read.json"):
        try:
            path.unlink()
        except OSError:
            pass  # already gone / locked — best-effort, matches the codebase style


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
