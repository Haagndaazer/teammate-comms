"""Post-update "what changed" note, shown once per version at session start.

Run by ``hooks/session-start.sh`` as ``python -m teammate_comms.whats_new``. Content comes
from a hand-curated ``whats-new.json`` in the plugin manifest folder; the last version shown
is kept in ``whats-new-seen`` in the plugin's data folder. Stdlib-only, no network, always exits 0,
and prints either nothing or one line: the note, JSON-escaped for the hook to embed.
"""
import json
import os
import sys
from pathlib import Path

from . import harness

SEEN_MARKER_FILENAME = "whats-new-seen"
ENV_VAR = "TEAMMATE_WHATS_NEW"
OFF_VALUES = frozenset({"off", "0", "false", "no"})
MAX_VERSIONS = 3
MAX_BULLETS = 9
UNSEEN_FLOOR = "0.0.0"

INSTRUCTION_LINE = "INSTRUCTION: Tell the user what changed in teammate-comms before continuing."
UPDATED_HEADER = "teammate-comms updated (v{seen} -> v{installed}) - new since your last session:"
FIRST_HEADER = "teammate-comms v{installed} - recent changes:"
OVERFLOW_LINE = "(...and older versions - see CHANGELOG.md.)"
FOOTER_LINE = ("(Shown once per update. Full details: the plugin's CHANGELOG.md. "
               "Disable these notices with TEAMMATE_WHATS_NEW=off.)")


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


def write_seen(plugin_data, version):
    """Store the bare version with no trailing newline; the hook compares it as a string."""
    try:
        data_dir = Path(plugin_data)
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = data_dir / f"{SEEN_MARKER_FILENAME}.tmp"
        tmp.write_bytes(version.encode("utf-8"))
        os.replace(tmp, data_dir / SEEN_MARKER_FILENAME)
    except OSError:
        pass


def check(plugin_root, plugin_data):
    """The note to show, or "" when there is nothing new. Advances the seen marker."""
    installed = read_installed_version(plugin_root)
    installed_key = version_key(installed)
    if installed_key is None:
        return ""
    seen = read_seen(plugin_data)
    first = seen is None
    seen_key = version_key(UNSEEN_FLOOR if first else seen)
    write_seen(plugin_data, installed)
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
    return "\n".join(lines)


def emit(note):
    """Print a note as one JSON-escaped ASCII line, safe to embed in the hook's JSON."""
    if note:
        sys.stdout.write(json.dumps(note)[1:-1])


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
        emit(check(plugin_root, plugin_data))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
