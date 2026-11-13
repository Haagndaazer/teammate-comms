"""Once-a-day check for a newer released plugin version, with a notice for the user.

Run by ``hooks/session-start.sh`` as ``python -m teammate_comms.update_check``. It reads the
marketplace's pinned commit for this plugin, then the version in the plugin manifest at that
commit, and compares version strings. Two read-only HTTPS GETs; nothing is installed. Always
writes its throttle stamp, always exits 0, and prints either nothing or one line: the
notice, JSON-escaped for the hook to embed.
"""
import http.client
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import harness
from .whats_new import emit, read_installed_version, version_key

STAMP_FILENAME = "update-check.json"
ENV_VAR = "TEAMMATE_UPDATE_NUDGE"
OFF_VALUES = frozenset({"off", "0", "false", "no"})
REQUEST_TIMEOUT_SECONDS = 3.0
WALL_CLOCK_SECONDS = 8.0
MAX_RESPONSE_BYTES = 1_000_000
_VERSION_RE = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6}){0,3}", re.ASCII)
_SHA_RE = re.compile(r"[0-9a-f]{7,64}", re.ASCII)

NUDGE_TEMPLATE = (
    "## Update Available: teammate-comms v{remote}\n"
    "You are running v{installed}.\n"
    "- Update with: {cta}\n"
    "- Then RESTART every {harness} session on this machine. The new version is not active "
    "in a session until it restarts.\n"
    "- TELL THE HUMAN this is waiting. Do not update on their behalf: it is always their "
    "call, and nothing is blocked while they decide.\n"
    "- Silence it with TEAMMATE_UPDATE_NUDGE=off."
)


def http_get_json(url, timeout):
    """GET a URL as JSON; any failure is None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "plugin-update-check"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read(MAX_RESPONSE_BYTES))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
        return None


def find_entry(data):
    if not isinstance(data, dict):
        return None
    plugins = data.get("plugins")
    if isinstance(plugins, list):
        for entry in plugins:
            if isinstance(entry, dict) and entry.get("name") == harness.PLUGIN_NAME:
                return entry
    entry = data.get(harness.PLUGIN_NAME)
    return entry if isinstance(entry, dict) else None


def fetch_remote_version(timeout):
    """The released version at the marketplace's pinned commit, or None."""
    policy = harness.current()
    entry = find_entry(http_get_json(
        harness.MARKETPLACE_URL_TEMPLATE.format(path=policy.marketplace_manifest_path), timeout))
    source = entry.get("source") if entry else None
    sha = source.get("sha") if isinstance(source, dict) else None
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        return None
    data = http_get_json(
        harness.PLUGIN_URL_TEMPLATE.format(sha=sha, path=policy.manifest_path), timeout)
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and _VERSION_RE.fullmatch(version) else None


def fetch_bounded(timeout, wall_clock):
    """Run the fetch on a daemon thread and give up after ``wall_clock`` seconds."""
    result = {}

    def worker():
        result["remote"] = fetch_remote_version(timeout)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(wall_clock)
    return None if thread.is_alive() else result.get("remote")


def write_stamp(plugin_data, remote):
    try:
        data_dir = Path(plugin_data)
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = data_dir / f"{STAMP_FILENAME}.tmp"
        tmp.write_text(json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "remote_version": remote or "",
        }), encoding="utf-8")
        os.replace(tmp, data_dir / STAMP_FILENAME)
    except OSError:
        pass


def check(plugin_root, plugin_data, timeout=REQUEST_TIMEOUT_SECONDS, wall_clock=WALL_CLOCK_SECONDS):
    """The notice to show, or "" when up to date or the check could not complete."""
    installed = read_installed_version(plugin_root)
    remote = fetch_bounded(timeout, wall_clock)
    write_stamp(plugin_data, remote)
    remote_key, installed_key = version_key(remote), version_key(installed)
    if remote_key is None or installed_key is None or remote_key <= installed_key:
        return ""
    policy = harness.current()
    return NUDGE_TEMPLATE.format(remote=remote, installed=installed, cta=policy.install_cta,
                                 harness=policy.display_name)


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
