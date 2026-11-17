"""Post-update "what changed" note, shown at session start until an agent confirms it.

Run by ``hooks/session-start.sh`` as ``python -m teammate_comms.whats_new``. Content comes
from a hand-curated ``whats-new.json`` in the plugin manifest folder. ``whats-new-seen`` in
the plugin's data folder holds the last version confirmed; it advances when an agent calls
teammate_readme(changes_seen=true), or after MAX_SHOWINGS unconfirmed showings. Stdlib-only,
no network, always exits 0, and prints either nothing or one line: the note, JSON-escaped
for the hook to embed.
"""
import json
import os
import secrets
import sys
from pathlib import Path

from . import harness

SEEN_MARKER_FILENAME = "whats-new-seen"
PENDING_FILENAME = "whats-new-pending.json"
MAX_SHOWINGS = 5
ENV_VAR = "TEAMMATE_WHATS_NEW"
OFF_VALUES = frozenset({"off", "0", "false", "no"})
MAX_VERSIONS = 3
MAX_BULLETS = 9
UNSEEN_FLOOR = "0.0.0"

INSTRUCTION_LINE = "INSTRUCTION: Tell the user what changed in teammate-comms before continuing."
UPDATED_HEADER = "teammate-comms updated (v{seen} -> v{installed}) - new since your last session:"
FIRST_HEADER = "teammate-comms v{installed} - recent changes:"
OVERFLOW_LINE = "(...and older versions - see CHANGELOG.md.)"
FOOTER_LINE = (f"(Shown at session start until confirmed, at most {MAX_SHOWINGS} times. Full details: the "
               "plugin's CHANGELOG.md. Disable these notices with TEAMMATE_WHATS_NEW=off.)")
CONFIRM_LINE = ("Once the user has been told, call teammate_readme(changes_seen=true) so this "
                "note stops appearing.")

CONFIRMED = "Recorded: the what-changed note for v{version} will not be shown again."
NOTHING_TO_CONFIRM = "Nothing to confirm: this session has no what-changed note."
STALE_SERVER = ("Not recorded: this server is running v{running} but the installed plugin is "
                "v{installed}. Reconnect or restart the session, then confirm again.")
NOT_RECORDED = "Could not record the confirmation; the note may appear once more."


def version_key(value):
    """A comparable tuple for a dotted-int version, or None if it is not one."""
    if not isinstance(value, str) or not value:
        return None
    parts = value.split(".")
    if len(value) > 40 or not all(p.isascii() and p.isdigit() for p in parts):
        return None
    key = tuple(int(p) for p in parts)
    return key + (0,) * max(0, 6 - len(key))


def read_installed_version(plugin_root):
    path = Path(plugin_root) / harness.current().manifest_path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version else None


def load_map(plugin_root):
    try:
        data = json.loads((Path(plugin_root) / harness.WHATS_NEW_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_seen(plugin_data):
    try:
        value = (Path(plugin_data) / SEEN_MARKER_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _write_atomic(plugin_data, filename, text):
    data_dir = Path(plugin_data)
    tmp = data_dir / f"{filename}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, data_dir / filename)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def write_seen(plugin_data, version):
    """Store the bare version with no trailing newline; the hook compares it as a string."""
    return _write_atomic(plugin_data, SEEN_MARKER_FILENAME, version)


def read_shown(plugin_data, installed):
    """How many times the note for this version has been printed without confirmation."""
    try:
        data = json.loads((Path(plugin_data) / PENDING_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict) or data.get("version") != installed:
        return 0
    shown = data.get("shown")
    if not isinstance(shown, int) or isinstance(shown, bool) or shown < 0:
        return 0
    return shown


def write_shown(plugin_data, installed, shown):
    return _write_atomic(plugin_data, PENDING_FILENAME,
                         json.dumps({"version": installed, "shown": shown}))


def clear_shown(plugin_data):
    try:
        (Path(plugin_data) / PENDING_FILENAME).unlink()
    except OSError:
        pass


def mark_seen(plugin_data, installed):
    ok = write_seen(plugin_data, installed) or write_seen(plugin_data, installed)
    if ok:
        clear_shown(plugin_data)
    return ok


def check(plugin_root, plugin_data):
    """The note to show, or "" when there is nothing new. Records the showing."""
    note, commit = plan(plugin_root, plugin_data)
    commit()
    return note


def plan(plugin_root, plugin_data):
    """(note, commit). commit() records the showing; call it only once the note is printed."""
    installed = read_installed_version(plugin_root)
    installed_key = version_key(installed)
    if installed_key is None:
        return "", lambda: None

    def finish():
        mark_seen(plugin_data, installed)

    seen = read_seen(plugin_data)
    note = _note(plugin_root, seen, installed, installed_key)
    if not note:
        return "", finish
    shown = read_shown(plugin_data, installed) + 1
    if shown >= MAX_SHOWINGS:
        return note, finish
    return note, lambda: write_shown(plugin_data, installed, shown)


def confirm(running_version):
    """Record that the user has been told. Returns the line the tool shows."""
    plugin_root = os.environ.get(harness.PLUGIN_ROOT_ENV, "")
    plugin_data = os.environ.get(harness.PLUGIN_DATA_ENV, "")
    if not plugin_root or not plugin_data:
        return NOTHING_TO_CONFIRM
    installed = read_installed_version(plugin_root)
    if version_key(installed) is None:
        return NOTHING_TO_CONFIRM
    if installed != running_version:
        return STALE_SERVER.format(running=running_version, installed=installed)
    if not mark_seen(plugin_data, installed):
        return NOT_RECORDED
    return CONFIRMED.format(version=installed)


def _note(plugin_root, seen, installed, installed_key):
    first = seen is None
    seen_key = version_key(UNSEEN_FLOOR if first else seen)
    if seen_key is None or seen_key >= installed_key:
        return ""

    candidates = []
    for version, bullets in load_map(plugin_root).items():
        key = version_key(version)
        if key is None or not seen_key < key <= installed_key or not isinstance(bullets, list):
            continue
        clean = [b for b in bullets if isinstance(b, str) and b]
        if clean:
            candidates.append((key, clean))
    if not candidates:
        return ""
    candidates.sort(key=lambda c: c[0], reverse=True)
    bullets = [b for _key, group in candidates[:MAX_VERSIONS] for b in group][:MAX_BULLETS]

    header = (FIRST_HEADER.format(installed=installed) if first
              else UPDATED_HEADER.format(seen=seen, installed=installed))
    lines = [INSTRUCTION_LINE, header] + [f"- {b}" for b in bullets]
    if len(candidates) > MAX_VERSIONS:
        lines.append(OVERFLOW_LINE)
    lines.append(FOOTER_LINE)
    lines.append(CONFIRM_LINE)
    return "\n".join(lines)


def emit(note):
    """Print a note as one JSON-escaped ASCII line, safe to embed in the hook's JSON."""
    if note:
        sys.stdout.write(json.dumps(note)[1:-1])
        sys.stdout.flush()


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args:
        print(f"unknown option: {args[0]}", file=sys.stderr)
        return 2
    if os.environ.get(ENV_VAR, "").strip().lower() in OFF_VALUES:
        return 0
    plugin_root = os.environ.get(harness.PLUGIN_ROOT_ENV, "")
    plugin_data = os.environ.get(harness.PLUGIN_DATA_ENV, "")
    if not plugin_root or not plugin_data:
        return 0
    try:
        note, commit = plan(plugin_root, plugin_data)
        emit(note)
        commit()
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
