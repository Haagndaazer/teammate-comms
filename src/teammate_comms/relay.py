"""Cross-machine relay: forwards comms events between machines over Tailscale.

``python -m teammate_comms.relay`` runs one relay per machine. Tools queue events as files
under ``<root>/TeammateComms/relay/outbox/<machine>/``; the relay exchanges them with each
approved peer and applies incoming events through the same local cores the tools use.
"""
import argparse
import collections
import http.client
import itertools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .comms import (
    MACHINE_NAME_PATTERN,
    RELAY_SENDER,
    CommsError,
    clean_ts,
    file_lock,
    find_group_case_variant,
    get_agents_dir,
    get_group_dir,
    get_relay_dir,
    human_presence_online,
    is_channel_alive,
    is_remote,
    now_timestamp,
    read_agent_record,
    read_group_meta,
    read_json_readonly,
    resolve_comms_root,
    split_address,
    validate_address,
    validate_agent_name,
    validate_group_name,
    validate_machine_name,
    write_group_meta,
    write_json_atomic,
)

PROTOCOL = 1
DEFAULT_PORT = 47821
PORT_ENV = "TEAMMATE_RELAY_PORT"
TAILSCALE_BIN_ENV = "TEAMMATE_TAILSCALE_BIN"
AUTOSTART_ENV = "TEAMMATE_RELAY_AUTOSTART"

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 200
MAX_QUEUED = 5000
SEEN_RING = 5000
MAX_ROSTER_AGENTS = 500
MAX_GROUP_MEMBERS = 500
MAX_APPLY_ATTEMPTS = 5
MAX_LOCK_ATTEMPTS = 30
TAILSCALE_GRACE_SECONDS = 60
STOP_WAIT_SECONDS = 8
START_CHECK_SECONDS = 1.0
APPLY_BUDGET_SECONDS = 2.0

LOOP_SECONDS = 0.25
POLL_IDLE_SECONDS = 2.0
POLL_BUSY_SECONDS = 0.5
HTTP_TIMEOUT_SECONDS = 5
ROSTER_SECONDS = 10
ROSTER_FRESH_SECONDS = 45
CONTACT_FRESH_SECONDS = 15
STATUS_FRESH_SECONDS = 20
STATUS_WRITE_SECONDS = 2
NOTICE_AFTER_SECONDS = 60
NOTICE_CHECK_SECONDS = 5
MAX_BACKOFF_SECONDS = 30
LOG_ROTATE_BYTES = 1024 * 1024

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}"
_STAMP_RE = re.compile(rf"^{_STAMP}$")
_MSG_ID_RE = re.compile(rf"^{_STAMP}(\.[0-9a-f]{{1,24}})?$")
_ROSTER_FIELDS = ("type", "project", "role", "status", "authority", "personality", "harness")
_TAILSCALE_PATHS = (
    r"C:\Program Files\Tailscale\tailscale.exe",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
)
_seq = itertools.count()


class RejectEvent(CommsError):
    """An incoming event that is invalid or not permitted; dropped, never retried."""


def peers_file(root):
    return get_relay_dir(root) / "peers.json"


def status_file(root):
    return get_relay_dir(root) / "status.json"


def stop_file(root):
    return get_relay_dir(root) / "stop"


def log_file(root):
    return get_relay_dir(root) / "relay.log"


def outbox_dir(root, machine):
    return get_relay_dir(root) / "outbox" / machine


def roster_file(root, machine):
    return get_relay_dir(root) / "roster" / f"{machine}.json"


def seen_file(root, machine):
    return get_relay_dir(root) / "seen" / f"{machine}.log"


def default_port():
    raw = os.environ.get(PORT_ENV, "").strip()
    if raw.isdigit() and 0 < int(raw) < 65536:
        return int(raw)
    return DEFAULT_PORT


def read_peers(root):
    """Approved peers as ``{machine: config}``."""
    path = peers_file(root)
    data = read_json_readonly(path)
    for _ in range(3):
        if data is not None or not path.exists():
            break
        time.sleep(0.05)
        data = read_json_readonly(path)
    peers = data.get("peers") if isinstance(data, dict) else None
    out = {}
    if isinstance(peers, dict):
        for name, cfg in peers.items():
            if isinstance(name, str) and MACHINE_NAME_PATTERN.match(name):
                out[name] = cfg if isinstance(cfg, dict) else {}
    return out


def update_peers(root, mutate):
    path = peers_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        peers = mutate(read_peers(root))
        write_json_atomic(path, {"peers": peers})
    return peers


def read_status(root):
    data = read_json_readonly(status_file(root))
    return data if isinstance(data, dict) else {}


def relay_alive(root, status=None):
    st = read_status(root) if status is None else status
    if not isinstance(st.get("pid"), int) or st.get("host") != socket.gethostname():
        return False
    try:
        age = time.time() - float(st.get("updated_epoch") or 0)
    except (TypeError, ValueError):
        return False
    return age <= STATUS_FRESH_SECONDS


def self_machine(root):
    me = read_status(root).get("self")
    return me.get("machine") if isinstance(me, dict) else None


def queued_count(root, machine):
    d = outbox_dir(root, machine)
    return sum(1 for _ in d.glob("*.json")) if d.exists() else 0


def enqueue(root, machine, kind, payload, sender=None):
    """Queue one event for ``machine``; the relay delivers it."""
    d = outbox_dir(root, machine)
    d.mkdir(parents=True, exist_ok=True)
    if queued_count(root, machine) >= MAX_QUEUED:
        raise CommsError(
            f"{MAX_QUEUED} events are already waiting for {machine!r} and none are being "
            f"delivered. Check teammate_peers(action=\"list\") before sending more."
        )
    event = {"id": uuid.uuid4().hex, "kind": kind, "payload": payload,
             "queued_epoch": time.time()}
    if sender:
        event["sender"] = sender
    name = f"{time.time_ns():020d}-{os.getpid():x}-{next(_seq):x}"
    tmp = d / f"{name}.tmp"
    tmp.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / f"{name}.json")
    return event["id"]


def read_roster(root, machine):
    data = read_json_readonly(roster_file(root, machine))
    return data if isinstance(data, dict) else None


def roster_fresh(roster):
    try:
        return time.time() - float(roster.get("received_epoch") or 0) <= ROSTER_FRESH_SECONDS
    except (AttributeError, TypeError, ValueError):
        return False


def remote_agent(root, name, machine):
    roster = read_roster(root, machine)
    for entry in (roster or {}).get("agents") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def _duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"


def describe_state(state, machine, detail="", port=None, me=None):
    port = port or default_port()
    me = me or "<this machine's Tailscale name>"
    if state == "ok":
        return f"{machine} is reachable."
    if state == "connecting":
        return f"{machine} has not been contacted yet."
    if state == "relay_stopped":
        return "the cross-machine relay is not running on this machine."
    if state == "tailscale_down":
        return (f"Tailscale is not connected on THIS machine ({detail or 'state unknown'}). "
                f"Start Tailscale or log in.")
    if state == "unknown_device":
        return (f"no device named {machine!r} is visible in this machine's tailnet. Check the "
                f"name with teammate_peers().")
    if state == "key_expired":
        return (f"{machine}'s Tailscale key has expired. Re-authenticate Tailscale on "
                f"{machine}.")
    if state == "offline":
        return (f"{machine} is offline in Tailscale (powered off, asleep, or disconnected).")
    if state == "relay_down":
        return (
            f"{machine} is online in Tailscale but nothing answers on port {port}. On "
            f"{machine}: confirm a teammate-comms session has registered since this machine "
            f"was approved there, and that its "
            f"firewall allows inbound TCP {port} from Tailscale. On Windows, as administrator: "
            f"netsh advfirewall firewall add rule name=\"teammate-comms relay\" dir=in "
            f"action=allow protocol=TCP localport={port} remoteip=100.64.0.0/10"
        )
    if state == "not_approved":
        return (f"{machine} refused this machine because it is not in its approved peers. "
                f"{remote_approval_hint(machine, me)}")
    if state == "identity_mismatch":
        return (f"{machine} knows this machine under a different Tailscale name"
                f"{' (' + detail + ')' if detail else ''}.")
    return f"{machine} cannot be reached: {detail or state}."


def peer_view(root, machine, status=None):
    """``{state, ok, text, since_epoch, queued}`` for one approved peer."""
    status = read_status(root) if status is None else status
    me = (status.get("self") or {}).get("machine")
    if not relay_alive(root, status):
        state, entry = "relay_stopped", {}
    else:
        entry = (status.get("peers") or {}).get(machine) or {}
        state = entry.get("state") or "connecting"
    text = describe_state(state, machine, entry.get("detail") or "", entry.get("port"), me)
    return {"state": state, "ok": state == "ok", "text": text,
            "since_epoch": entry.get("since_epoch"), "queued": queued_count(root, machine)}


def delivery_report(root, name, machine, started=None):
    """Lines appended to a remote send's result."""
    lines = []
    if started == "started":
        lines.append("The cross-machine relay was not running and has just been started.")
    elif started == "disabled":
        lines.append(f"⚠️ WARNING: relay autostart is disabled ({AUTOSTART_ENV}=0). Nothing "
                     f"is sent until `python -m teammate_comms.relay` runs on this machine.")
    elif started == "failed":
        lines.append("⚠️ WARNING: the cross-machine relay could not be started. Nothing is "
                     "sent until it runs; see teammate_peers(action=\"list\").")
    view = peer_view(root, machine)
    if view["ok"]:
        entry = remote_agent(root, name, machine)
        if entry is None:
            known = [e.get("name") for e in (read_roster(root, machine) or {}).get("agents") or []
                     if isinstance(e, dict) and e.get("name")]
            lines.append(
                f"NOTE: {machine} has no registered teammate named {name!r}. The message will "
                f"wait in an inbox there until someone registers under that exact name. "
                f"Registered on {machine}: {', '.join(known) or '(none)'}."
            )
        elif entry.get("live"):
            lines.append(f"{name} is live on {machine} — delivery within a few seconds.")
        else:
            lines.append(f"WARNING: {name} is not running on {machine}. The message will wait "
                         f"in their inbox there.")
    elif view["state"] == "connecting" or started == "started":
        lines.append(f"Not connected to {machine} yet. The message is queued; 'comms-relay' "
                     f"will message you if it cannot be delivered.")
    else:
        since = view.get("since_epoch")
        how_long = f" for {_duration(time.time() - since)}" if since else ""
        lines.append(
            f"⚠️ WARNING: NOT DELIVERED — {view['text']} This has been the case{how_long}. "
            f"{view['queued']} event(s) are waiting on this machine and send automatically "
            f"once {machine} is reachable."
        )
    return lines


def request_stop(root):
    path = stop_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(read_status(root).get("pid") or "any"), encoding="utf-8")


def _version_key(value):
    return tuple(int(p) if p.isdigit() else 0 for p in str(value or "").split("."))


def stop_and_wait(root, seconds=STOP_WAIT_SECONDS):
    if not relay_alive(root):
        return
    request_stop(root)
    deadline = time.time() + seconds
    while time.time() < deadline and relay_alive(root):
        time.sleep(0.1)


def forget_peer(root, machine):
    shutil.rmtree(outbox_dir(root, machine), ignore_errors=True)
    for path in (roster_file(root, machine), seen_file(root, machine)):
        try:
            path.unlink()
        except OSError:
            pass


def approval_hint(machine):
    return (f"If your user wants this machine linked to {machine}, approve it with "
            f"teammate_peers(action=\"add\", machine=\"{machine}\").")


def remote_approval_hint(machine, me):
    return (f"A session on {machine} must approve this machine with "
            f"teammate_peers(action=\"add\", machine=\"{me}\").")


def start_line(started):
    return {
        "started": "The relay was started on this machine.",
        "running": "The relay is running on this machine.",
        "disabled": f"⚠️ WARNING: relay autostart is disabled ({AUTOSTART_ENV}=0); "
                    f"run `python -m teammate_comms.relay` on this machine.",
        "failed": "⚠️ WARNING: the relay could not be started on this machine.",
        "no_peers": "No peers are approved, so the relay is not running.",
    }.get(started, "")


def approve_peer(root, machine, net=None):
    machine = validate_machine_name(machine, param="machine")
    net = net or TailscaleNet()
    me = net.self_info()
    if machine == me.get("machine"):
        raise CommsError(f"{machine!r} is this machine. Add the OTHER machine's name.")
    update_peers(root, lambda peers: {**peers, machine: peers.get(machine, {})})
    lines = [f"Approved {machine!r} as a peer machine.", start_line(ensure_relay(root))]
    if me["state"] != "running":
        lines.append(f"⚠️ WARNING: Tailscale is not connected on this machine "
                     f"({me['detail']}). Nothing is exchanged until it is.")
    elif machine not in net.devices():
        lines.append(f"⚠️ WARNING: no device named {machine!r} is visible in this tailnet. "
                     f"Visible devices: {', '.join(net.devices()) or '(none)'}.")
    mine = me.get("machine") or "<this machine's Tailscale name>"
    lines.append(f"Both sides must approve each other: on {machine}, a session runs "
                 f"teammate_peers(action=\"add\", machine=\"{mine}\"). Teammates there are "
                 f"then addressed as name@{machine}.")
    return "\n".join(lines)


def remove_peer(root, machine):
    machine = validate_machine_name(machine, param="machine")
    if machine not in read_peers(root):
        raise CommsError(f"{machine!r} is not an approved peer.")
    dropped = queued_count(root, machine)
    update_peers(root, lambda peers: {m: c for m, c in peers.items() if m != machine})
    forget_peer(root, machine)
    return (f"Removed {machine!r} from approved peers." +
            (f" {dropped} undelivered event(s) for it were discarded." if dropped else ""))


def peers_report(root, net=None):
    net = net or TailscaleNet()
    peers = read_peers(root)
    status = read_status(root)
    me = net.self_info()
    if relay_alive(root, status):
        own = status.get("self") or {}
        listening = (f"listening on port {status.get('port')}" if status.get("listening")
                     else f"⚠️ NOT LISTENING ({status.get('listener_error') or 'Tailscale not connected'})")
        lines = [f"Relay: running as {own.get('machine') or '(unknown)'} "
                 f"({own.get('ip') or 'no Tailscale address'}), {listening}."]
    elif peers:
        lines = ["Relay: ⚠️ NOT RUNNING on this machine. It starts when a teammate registers "
                 "or with teammate_peers(action=\"restart\")."]
    else:
        lines = ["Relay: not running (no peers approved)."]
    if me["state"] != "running":
        lines.append(f"Tailscale: ⚠️ NOT CONNECTED on this machine ({me['detail']}).")
    else:
        lines.append(f"Tailscale: connected as {me['machine']}.")
    if peers:
        lines.append("Approved peers:")
        for machine in sorted(peers):
            view = peer_view(root, machine, status)
            known = len((read_roster(root, machine) or {}).get("agents") or [])
            if view["ok"]:
                lines.append(f"  - {machine}: reachable, {known} teammate(s) known.")
            else:
                waiting = f" {view['queued']} event(s) waiting." if view["queued"] else ""
                lines.append(f"  - {machine}: ⚠️ {view['text']}{waiting}")
    else:
        lines.append("Approved peers: none.")
    others = [d for d in net.devices() if d not in peers]
    if others:
        lines.append(f"Other devices on this tailnet: {', '.join(others)}.")
    lines.append("Approve a machine with teammate_peers(action=\"add\", machine=\"<name>\"), "
                 "only when your user asks for it.")
    return "\n".join(lines)


def ensure_relay(root):
    """Start a detached relay if peers are configured and none is running.

    Returns ``no_peers`` | ``running`` | ``started`` | ``disabled`` | ``failed``.
    """
    try:
        if not read_peers(root):
            return "no_peers"
        status = read_status(root)
        if relay_alive(root, status):
            if _version_key(status.get("version")) >= _version_key(__version__):
                return "running"
            stop_and_wait(root)
        if os.environ.get(AUTOSTART_ENV, "").strip() == "0":
            return "disabled"
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        cmd = [sys.executable, "-m", "teammate_comms.relay", "--comms-dir", str(root)]
        dn = subprocess.DEVNULL
        get_relay_dir(root).mkdir(parents=True, exist_ok=True)
        with open(log_file(root), "ab") as err:
            kwargs = {"stdin": dn, "stdout": dn, "stderr": err, "env": env, "close_fds": True}
            if os.name == "nt":
                no_window, new_group, breakaway = 0x08000000, 0x00000200, 0x01000000
                try:
                    proc = subprocess.Popen(cmd, creationflags=no_window | new_group | breakaway,
                                            **kwargs)
                except OSError:
                    proc = subprocess.Popen(cmd, creationflags=no_window | new_group, **kwargs)
            else:
                proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
        deadline = time.monotonic() + START_CHECK_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return "running" if relay_alive(root) else "failed"
            time.sleep(0.1)
        return "started"
    except Exception:
        return "failed"


def find_tailscale():
    override = os.environ.get(TAILSCALE_BIN_ENV, "").strip()
    if override:
        return override
    found = shutil.which("tailscale")
    if found:
        return found
    for cand in _TAILSCALE_PATHS:
        if os.path.exists(cand):
            return cand
    return None


def _ipv4(ips):
    for ip in ips or []:
        if isinstance(ip, str) and "." in ip and ":" not in ip:
            return ip
    return None


class TailscaleNet:
    """Machine identity and reachability from ``tailscale status --json``."""

    CACHE_SECONDS = 15

    def __init__(self):
        self._data = None
        self._error = "Tailscale status not read yet"
        self._at = None
        self._good_at = 0.0

    def refresh(self):
        self._at = None

    def _load(self):
        now = time.monotonic()
        if self._at is not None and now - self._at < self.CACHE_SECONDS:
            return self._data
        self._at = now
        binary = find_tailscale()
        if not binary:
            self._data, self._error = None, "Tailscale is not installed (no tailscale command found)"
            return None
        try:
            kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
            out = subprocess.run([binary, "status", "--json"], capture_output=True,
                                 timeout=10, **kwargs)
            data = json.loads(out.stdout.decode("utf-8", "replace"))
            if not isinstance(data, dict):
                raise ValueError("unexpected output")
            self._data, self._error, self._good_at = data, "", now
        except Exception as e:
            if self._data is None or now - self._good_at > TAILSCALE_GRACE_SECONDS:
                self._data, self._error = None, f"could not read Tailscale status: {e}"
        return self._data

    @staticmethod
    def _label(node, suffix):
        dns = str(node.get("DNSName") or "").strip(".").lower()
        if not dns:
            return None
        label, _, rest = dns.partition(".")
        if suffix and rest != suffix:
            return None
        return label if MACHINE_NAME_PATTERN.match(label) else None

    def _suffix(self, data):
        suffix = str(data.get("MagicDNSSuffix") or "").strip(".").lower()
        if suffix:
            return suffix
        dns = str((data.get("Self") or {}).get("DNSName") or "").strip(".").lower()
        return dns.partition(".")[2]

    def self_info(self):
        data = self._load()
        if data is None:
            return {"state": "down", "detail": self._error, "machine": None, "ip": None}
        backend = data.get("BackendState")
        node = data.get("Self") or {}
        machine = self._label(node, self._suffix(data))
        ip = _ipv4(node.get("TailscaleIPs"))
        running = backend == "Running" and machine and ip
        detail = "" if running else f"Tailscale state: {backend or 'unknown'}"
        return {"state": "running" if running else "down", "detail": detail,
                "machine": machine, "ip": ip}

    def _peers(self):
        data = self._load()
        if data is None:
            return {}
        suffix = self._suffix(data)
        out, clashes = {}, set()
        for node in (data.get("Peer") or {}).values():
            if not isinstance(node, dict) or node.get("ShareeNode"):
                continue
            label = self._label(node, suffix)
            if not label:
                continue
            if label in out:
                clashes.add(label)
                continue
            out[label] = {"ip": _ipv4(node.get("TailscaleIPs")),
                          "ips": [i for i in node.get("TailscaleIPs") or [] if isinstance(i, str)],
                          "online": node.get("Online"),
                          "expired": bool(node.get("Expired"))}
        for label in clashes:
            out.pop(label, None)
        return out

    def peer_info(self, machine):
        info = self._peers().get(machine)
        return info if info and info.get("ip") else None

    def identify(self, ip):
        for machine, info in self._peers().items():
            if ip in info["ips"]:
                return machine
        return None

    def devices(self):
        return sorted(self._peers())


class _Names:
    """Localizes sender-relative names from the verified source machine."""

    def __init__(self, me, src):
        self.me, self.src = me, src

    def actor(self, value):
        if not isinstance(value, str) or is_remote(value):
            raise RejectEvent(f"actor {value!r} must be a bare name on the sending machine")
        try:
            validate_agent_name(value)
        except CommsError as e:
            raise RejectEvent(str(e))
        return f"{value}@{self.src}"

    def local(self, value):
        try:
            name, machine = split_address(validate_address(value))
        except CommsError as e:
            raise RejectEvent(str(e))
        if machine is None:
            return f"{name}@{self.src}"
        if machine == self.me:
            return name
        return f"{name}@{machine}"

    def target(self, value):
        out = self.local(value)
        if is_remote(out):
            raise RejectEvent(f"{value!r} is not on this machine")
        return out

    def many(self, values, cap):
        out = []
        for v in (values if isinstance(values, list) else [])[:cap]:
            loc = self.local(v)
            if loc not in out:
                out.append(loc)
        return out


def _clean_id(value):
    if not isinstance(value, str) or not _MSG_ID_RE.match(value):
        raise RejectEvent(f"bad message id {value!r}")
    return value


def _already_here(root, record):
    """True if this exact message was applied before; reject an id owned by someone else."""
    from . import tools
    info = tools.resolve_message(root, None, record["id"])
    if info is None:
        return False
    if info.get("from") != record["from"]:
        raise RejectEvent(f"id {record['id']} already belongs to another message")
    return True


def _machine_in_group(meta, src):
    return any(split_address(m)[1] == src for m in meta.get("members", []))


def _clean_group(value):
    try:
        return validate_group_name(value)
    except CommsError as e:
        raise RejectEvent(str(e))


def _clean_record(names, raw):
    from . import tools
    if not isinstance(raw, dict):
        raise RejectEvent("missing record")
    try:
        record = {"id": _clean_id(raw.get("id")), "from": names.actor(raw.get("from")),
                  "priority": tools._clean_priority(raw.get("priority")),
                  "message": tools._clean_message(raw.get("message"))}
        post_type = tools._clean_post_type(raw.get("post_type"))
    except RejectEvent:
        raise
    except CommsError as e:
        raise RejectEvent(str(e))
    if post_type:
        record["post_type"] = post_type
    reply_to = raw.get("reply_to")
    if isinstance(reply_to, str) and reply_to.strip():
        record["reply_to"] = reply_to.strip()[:128]
    ts = clean_ts(raw.get("ts"))
    if ts is not None:
        record["ts"] = ts
    return record


def _ensure_group(root, names, group, payload, create=True):
    """Return the local group name, creating the group from the event if it is missing."""
    group = find_group_case_variant(root, None, group) or group
    meta = read_group_meta(root, None, group)
    if meta is not None:
        if not _machine_in_group(meta, names.src):
            raise RejectEvent(
                f"{names.src} has no member in #{group} on {names.me}; a member on "
                f"{names.me} must add one first")
        return group
    if not create:
        raise RejectEvent(f"there is no group #{group} on {names.me}")
    members = names.many(payload.get("members"), MAX_GROUP_MEMBERS)
    if not any(not is_remote(m) for m in members):
        raise RejectEvent(f"group #{group} has no members on this machine")
    creator = payload.get("creator")
    creator = names.local(creator) if isinstance(creator, str) and creator else members[0]
    created = payload.get("createdAt")
    from . import tools
    get_group_dir(root, None, group).mkdir(parents=True, exist_ok=True)
    with file_lock(tools._group_meta_file(root, None, group)):
        if read_group_meta(root, None, group) is None:
            write_group_meta(root, None, group, {
                "name": group, "members": members, "creator": creator,
                "createdAt": created if isinstance(created, str) and _STAMP_RE.match(created)
                else now_timestamp(),
            })
    return group


def _apply_dm(root, names, payload):
    from . import tools
    to = names.target(payload.get("to"))
    record = _clean_record(names, payload.get("record"))
    if _already_here(root, record):
        return
    tools.deliver_dm_record(root, None, record, to)


def _apply_group_post(root, names, payload):
    from . import tools
    group = _clean_group(payload.get("group"))
    raw = payload.get("record")
    record = _clean_record(names, raw)
    group = _ensure_group(root, names, group, payload, create=False)
    record["group"] = f"#{group}"
    mentions = names.many(raw.get("mentions"), 50)
    if mentions:
        record["mentions"] = mentions
    if _already_here(root, record):
        return
    sender = record["from"]

    def mutate(meta):
        members = meta.get("members", [])
        if sender not in members:
            members.append(sender)
        meta["members"] = members
        return meta
    meta = tools._edit_group_members(root, None, group, f"#{group}", mutate)
    tools.post_group_record(root, None, group, record, meta.get("members", []), _relay=False)


def _apply_group_members(root, names, payload):
    from . import tools
    group = _clean_group(payload.get("group"))
    op = payload.get("op")
    if op == "add":
        group = _ensure_group(root, names, group, payload)
        added = names.many(payload.get("names"), MAX_GROUP_MEMBERS)

        def mutate(meta):
            members = meta.get("members", [])
            for n in added:
                if n not in members and len(members) < MAX_GROUP_MEMBERS:
                    members.append(n)
            meta["members"] = members
            return meta
    elif op == "remove":
        group = find_group_case_variant(root, None, group) or group
        meta = read_group_meta(root, None, group)
        if meta is None:
            return
        gone = {names.actor(n) for n in (payload.get("names") or [])[:MAX_GROUP_MEMBERS]}

        def mutate(meta):
            meta["members"] = [m for m in meta.get("members", []) if m not in gone]
            return meta
    else:
        raise RejectEvent(f"unknown membership op {op!r}")
    tools._edit_group_members(root, None, group, f"#{group}", mutate)


def _apply_group_delete(root, names, payload):
    from . import tools
    group = _clean_group(payload.get("group"))
    group = find_group_case_variant(root, None, group) or group
    actor = names.actor(payload.get("by"))
    meta = read_group_meta(root, None, group)
    if meta is None:
        return
    creator, members = meta.get("creator"), meta.get("members", [])
    if not (actor == creator or (creator not in members and actor in members)):
        raise RejectEvent(f"{actor} may not delete #{group}")
    tools.delete_group_local(root, None, group, actor)


def _resolve_target(root, names, target):
    """The target message, only if the source machine is a party to it."""
    from . import tools
    missing = RejectEvent(f"no message {target!r} here")
    info = tools.resolve_message(root, None, target)
    if info is None:
        raise missing
    if info.get("kind") == "group":
        try:
            group = validate_group_name(info.get("group") or "")
        except CommsError:
            group = None
        meta = read_group_meta(root, None, group) if group else None
        if meta is None:
            raise RejectEvent(f"the group holding {target} no longer exists here")
        if not _machine_in_group(meta, names.src):
            raise missing
    elif info.get("to") and names.src not in (split_address(info.get("from"))[1],
                                              split_address(info["to"])[1]):
        raise missing
    return info


def _apply_reaction(root, names, payload):
    from . import tools
    actor = names.actor(payload.get("from"))
    target = _clean_id(payload.get("target"))
    emoji = payload.get("emoji")
    if emoji not in tools._REACTIONS:
        raise RejectEvent(f"unknown emoji {emoji!r}")
    _resolve_target(root, names, target)
    tools.react(root, None, actor, target, emoji, bool(payload.get("remove")), _relay=False)


def _apply_deletion(root, names, payload):
    from . import tools
    actor = names.actor(payload.get("by"))
    target = _clean_id(payload.get("target"))
    info = _resolve_target(root, names, target)
    if info.get("from") != actor:
        raise RejectEvent(f"{actor} is not the author of {target}")
    tools.delete_message(root, None, actor, target, _relay=False)


_APPLIERS = {
    "dm": _apply_dm,
    "group_post": _apply_group_post,
    "group_members": _apply_group_members,
    "group_delete": _apply_group_delete,
    "reaction": _apply_reaction,
    "deletion": _apply_deletion,
}


def apply_event(root, me, src, kind, payload):
    """Apply one event received from verified machine ``src``."""
    fn = _APPLIERS.get(kind)
    if fn is None:
        raise RejectEvent(f"unknown event kind {kind!r}")
    if not isinstance(payload, dict):
        raise RejectEvent("payload must be an object")
    fn(root, _Names(me, src), payload)


def build_roster(root):
    agents = []
    agents_dir = get_agents_dir(root, None)
    if agents_dir.exists():
        for path in sorted(agents_dir.glob("*.json"))[:MAX_ROSTER_AGENTS]:
            record = read_agent_record(root, None, path.stem)
            if not isinstance(record, dict) or record.get("type") not in ("full", "human"):
                continue
            if record.get("type") == "human":
                live = bool(human_presence_online(record))
            else:
                live = bool(is_channel_alive(record, pid_check=False))
            entry = {"name": path.stem, "live": live}
            for field in _ROSTER_FIELDS:
                if record.get(field):
                    entry[field] = str(record[field])[:600]
            agents.append(entry)
    return {"agents": agents}


def _clean_roster(roster):
    agents = []
    for raw in (roster.get("agents") if isinstance(roster.get("agents"), list) else [])[:MAX_ROSTER_AGENTS]:
        if not isinstance(raw, dict):
            continue
        try:
            validate_agent_name(raw.get("name"))
        except CommsError:
            continue
        entry = {"name": raw["name"], "live": bool(raw.get("live"))}
        for field in _ROSTER_FIELDS:
            if isinstance(raw.get(field), str) and raw[field]:
                entry[field] = " ".join(raw[field].split())[:600]
        agents.append(entry)
    return agents


def _make_handler(relay):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 15

        def log_message(self, *args):
            pass

        def _send(self, status, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def do_POST(self):
            machine = relay.authorize(self.client_address[0])
            if machine is None:
                return self._send(403, {"error": "not_approved"})
            if self.path != "/v1/exchange":
                return self._send(404, {"error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length") or -1)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                return self._send(413, {"error": "bad_length"})
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"error": "bad_json"})
            try:
                status, obj = relay.handle_exchange(machine, body)
            except Exception as e:
                relay.log(f"exchange from {machine} crashed: {type(e).__name__}: {e}")
                status, obj = 500, {"error": "internal"}
            self._send(status, obj)

    return Handler


class Relay:
    def __init__(self, root, net=None, port=None, clock=time.time,
                 notice_after=NOTICE_AFTER_SECONDS):
        self.root = Path(root)
        self.net = net or TailscaleNet()
        self.port = port or default_port()
        self.clock = clock
        self.notice_after = notice_after
        self.machine = None
        self.ip = None
        self._tailscale = {}
        self._lock = threading.RLock()
        self._server = None
        self._server_addr = None
        self._listener_error = ""
        self._listener_retry = 0.0
        self._peers = {}
        self._seen = {}
        self._attempts = {}
        self._stop = threading.Event()
        self._status_at = 0.0
        self._status_last = None
        self._rejected_at = {}

    def log(self, message):
        try:
            path = log_file(self.root)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(path, path.with_suffix(".log.1"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{now_timestamp()} {message}\n")
        except OSError:
            pass

    def _peer(self, machine):
        with self._lock:
            st = self._peers.get(machine)
            if st is None:
                st = {"state": "connecting", "detail": "", "since": None, "last_contact": 0.0,
                      "next_due": 0.0, "fails": 0, "acks": [], "nacks": [], "inflight": {},
                      "notified": set(), "roster_sent": 0.0, "notice_at": 0.0,
                      "port": self.port}
                self._peers[machine] = st
            return st

    def _set_state(self, st, state, detail=""):
        if st["state"] != state:
            st["since"] = None if state == "ok" else self.clock()
        st["state"], st["detail"] = state, detail

    def _contact_ok(self, st):
        st["last_contact"] = self.clock()
        st["fails"] = 0
        self._set_state(st, "ok")

    def _contact_failed(self, st, state, detail=""):
        st["fails"] += 1
        st["next_due"] = self.clock() + min(MAX_BACKOFF_SECONDS, 2 ** min(st["fails"], 5))
        if self.clock() - st["last_contact"] <= CONTACT_FRESH_SECONDS:
            return
        self._set_state(st, state, detail)

    def _seen_ring(self, machine):
        ring = self._seen.get(machine)
        if ring is None:
            lines = []
            try:
                lines = [ln for ln in seen_file(self.root, machine).read_text(
                    encoding="utf-8").splitlines() if ln][-SEEN_RING:]
            except OSError:
                pass
            ids, failed = [], {}
            for line in lines:
                event_id, _, error = line.partition("\t")
                ids.append(event_id)
                if error:
                    failed[event_id] = error
            ring = {"order": collections.deque(ids), "ids": set(ids), "failed": failed,
                    "appended": 0}
            self._seen[machine] = ring
        return ring

    def _seen_add(self, machine, event_id, error=""):
        """Remember an event's outcome; a failed one keeps its reason so a resend gets it again."""
        ring = self._seen_ring(machine)
        if event_id in ring["ids"]:
            return
        error = " ".join(str(error).split())[:300]
        ring["order"].append(event_id)
        ring["ids"].add(event_id)
        if error:
            ring["failed"][event_id] = error
        while len(ring["order"]) > SEEN_RING:
            old = ring["order"].popleft()
            ring["ids"].discard(old)
            ring["failed"].pop(old, None)

        def line(i):
            return f"{i}\t{ring['failed'][i]}" if i in ring["failed"] else i
        path = seen_file(self.root, machine)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            ring["appended"] += 1
            if ring["appended"] >= SEEN_RING:
                path.write_text("\n".join(line(i) for i in ring["order"]) + "\n",
                                encoding="utf-8")
                ring["appended"] = 0
            else:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line(event_id) + "\n")
        except OSError as e:
            self.log(f"could not persist seen id for {machine}: {e}")

    def _apply_events(self, machine, events):
        applied, failed = [], []
        ring = self._seen_ring(machine)
        started = time.monotonic()
        for ev in (events if isinstance(events, list) else [])[:MAX_EVENTS]:
            event_id = ev.get("id") if isinstance(ev, dict) else None
            if not isinstance(event_id, str) or not _ID_RE.match(event_id):
                continue
            if event_id in ring["ids"]:
                if event_id in ring["failed"]:
                    failed.append({"id": event_id, "error": ring["failed"][event_id]})
                else:
                    applied.append(event_id)
                continue
            if (applied or failed) and time.monotonic() - started > APPLY_BUDGET_SECONDS:
                break
            error = ""
            try:
                apply_event(self.root, self.machine, machine, ev.get("kind"), ev.get("payload"))
                applied.append(event_id)
            except RejectEvent as e:
                error = str(e)[:300] or "rejected"
                self.log(f"rejected {ev.get('kind')} {event_id} from {machine}: {e}")
            except Exception as e:
                tries = self._attempts.get(event_id, 0) + 1
                self._attempts[event_id] = tries
                self.log(f"apply {ev.get('kind')} {event_id} from {machine} failed "
                         f"(attempt {tries}): {type(e).__name__}: {e}")
                locked = isinstance(e, CommsError) and "Could not acquire lock" in str(e)
                if tries < (MAX_LOCK_ATTEMPTS if locked else MAX_APPLY_ATTEMPTS):
                    break
                error = f"{type(e).__name__}: {e}"[:300]
            if error:
                failed.append({"id": event_id, "error": error})
            self._attempts.pop(event_id, None)
            self._seen_add(machine, event_id, error)
        return applied, failed

    def _next_batch(self, machine, st):
        d = outbox_dir(self.root, machine)
        if not d.exists():
            return []
        events, size = [], 0
        for path in sorted(d.glob("*.json"))[:MAX_EVENTS]:
            try:
                raw = path.read_text(encoding="utf-8")
                ev = json.loads(raw)
                event = {"id": ev["id"], "kind": ev["kind"], "payload": ev["payload"]}
            except (OSError, ValueError, KeyError, TypeError):
                self._quarantine(path)
                continue
            size += len(raw.encode("utf-8"))
            if events and size > MAX_BATCH_BYTES:
                break
            events.append(event)
            st["inflight"][event["id"]] = path
        return events

    def _quarantine(self, path):
        try:
            bad = get_relay_dir(self.root) / "bad"
            bad.mkdir(parents=True, exist_ok=True)
            os.replace(path, bad / path.name)
            self.log(f"quarantined unreadable event file {path.name}")
        except OSError:
            pass

    def _settle(self, st, applied, failed, machine=""):
        for item in failed if isinstance(failed, list) else []:
            if not isinstance(item, dict):
                continue
            error = str(item.get("error") or "no reason given")[:300]
            self.log(f"{machine} rejected event {item.get('id')}: {error}")
            path = st["inflight"].get(item.get("id"))
            try:
                ev = json.loads(path.read_text(encoding="utf-8"))
            except (AttributeError, OSError, ValueError):
                continue
            sender = ev.get("sender")
            if isinstance(sender, str) and not is_remote(sender) and sender != RELAY_SENDER:
                self._notify(sender, f"⚠️ REJECTED by {machine}: your {ev.get('kind')} was not "
                                     f"applied there. Reason: {error}")
        ids = [i for i in (applied if isinstance(applied, list) else []) if isinstance(i, str)]
        ids += [i.get("id") for i in (failed if isinstance(failed, list) else [])
                if isinstance(i, dict)]
        for event_id in ids:
            path = st["inflight"].pop(event_id, None)
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass

    def _store_roster(self, machine, roster):
        path = roster_file(self.root, machine)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"machine": machine, "received_epoch": time.time(),
                                 "agents": _clean_roster(roster)})

    def authorize(self, source_ip):
        """The approved machine a connection came from, or None."""
        machine = self.net.identify(source_ip)
        if machine is None:
            last = self._rejected_at.get(source_ip, 0.0)
            if time.monotonic() - last > 5:
                self._rejected_at[source_ip] = time.monotonic()
                self.net.refresh()
                machine = self.net.identify(source_ip)
        if machine is not None and machine in read_peers(self.root):
            return machine
        if len(self._rejected_at) > 1000:
            self._rejected_at.clear()
        self.log(f"refused connection from {source_ip} ({machine or 'not a tailnet device'})")
        return None

    def handle_exchange(self, machine, body):
        if not isinstance(body, dict) or body.get("v") != PROTOCOL:
            return 400, {"error": "bad_request"}
        if body.get("from") != machine:
            return 403, {"error": "identity_mismatch", "you_are": machine}
        with self._lock:
            st = self._peer(machine)
            self._settle(st, body.get("ack"), body.get("failed"), machine)
            applied, failed = self._apply_events(machine, body.get("events"))
            if isinstance(body.get("roster"), dict):
                self._store_roster(machine, body["roster"])
            self._contact_ok(st)
            out = self._next_batch(machine, st)
        return 200, {"v": PROTOCOL, "from": self.machine, "applied": applied,
                     "failed": failed, "events": out}

    def _post(self, ip, port, body):
        conn = http.client.HTTPConnection(ip, port, timeout=HTTP_TIMEOUT_SECONDS,
                                          source_address=(self.ip, 0))
        try:
            conn.request("POST", "/v1/exchange", body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read(MAX_BODY_BYTES + 1)
        finally:
            conn.close()
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            obj = {}
        return resp.status, obj if isinstance(obj, dict) else {}

    def _exchange(self, machine, cfg, st):
        now = self.clock()
        port = cfg.get("port") if isinstance(cfg.get("port"), int) else self.port
        st["port"] = port
        info = self.net.peer_info(machine)
        if info is None:
            return self._contact_failed(st, "unknown_device")
        with self._lock:
            batch = self._next_batch(machine, st)
            acks, nacks = list(st["acks"]), list(st["nacks"])
        body = {"v": PROTOCOL, "from": self.machine, "ack": acks, "failed": nacks,
                "events": batch}
        send_roster = now - st["roster_sent"] >= ROSTER_SECONDS
        if send_roster:
            body["roster"] = build_roster(self.root)
        try:
            status, resp = self._post(info["ip"], port, body)
        except Exception as e:
            if info.get("expired"):
                state = "key_expired"
            elif info.get("online") is False:
                state = "offline"
            else:
                state = "relay_down"
            return self._contact_failed(st, state, f"{type(e).__name__}: {e}")
        if status == 403:
            mismatch = resp.get("error") == "identity_mismatch"
            return self._contact_failed(
                st, "identity_mismatch" if mismatch else "not_approved",
                f"it calls this machine {resp.get('you_are')!r}" if mismatch else "")
        if status != 200:
            return self._contact_failed(st, "error", f"HTTP {status} {resp.get('error') or ''}")
        with self._lock:
            st["acks"] = [a for a in st["acks"] if a not in acks]
            st["nacks"] = [f for f in st["nacks"] if f not in nacks]
            if send_roster:
                st["roster_sent"] = now
            self._settle(st, resp.get("applied"), resp.get("failed"), machine)
            incoming = resp.get("events") if isinstance(resp.get("events"), list) else []
            applied, failed = self._apply_events(machine, incoming)
            st["acks"] += applied
            st["nacks"] += failed
            self._contact_ok(st)
            busy = bool(batch or incoming or queued_count(self.root, machine))
        progressed = bool(applied or failed)
        st["next_due"] = now if progressed else now + (
            POLL_BUSY_SECONDS if busy and not incoming else POLL_IDLE_SECONDS)

    def _notify(self, sender, text):
        from . import tools
        try:
            if read_agent_record(self.root, None, sender) is None:
                return
            tools.send_dm(self.root, None, RELAY_SENDER, sender, text, priority="urgent")
        except Exception as e:
            self.log(f"could not notify {sender}: {type(e).__name__}: {e}")

    def _notices(self, machine, st):
        now = self.clock()
        if now - st["notice_at"] < NOTICE_CHECK_SECONDS:
            return
        st["notice_at"] = now
        d = outbox_dir(self.root, machine)
        files = sorted(d.glob("*.json"))[:MAX_EVENTS] if d.exists() else []
        if not files:
            for sender in sorted(st["notified"]):
                self._notify(sender, f"✅ {machine} is reachable again. Nothing you sent "
                                     f"is still waiting for it.")
            st["notified"] = set()
            return
        if st["state"] == "ok":
            return
        overdue = set()
        for path in files:
            try:
                ev = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            sender = ev.get("sender")
            try:
                waited = now - float(ev.get("queued_epoch") or now)
            except (TypeError, ValueError):
                continue
            if (isinstance(sender, str) and not is_remote(sender) and sender != RELAY_SENDER
                    and waited >= self.notice_after and sender not in st["notified"]):
                overdue.add(sender)
        if not overdue:
            return
        text = describe_state(st["state"], machine, st["detail"], st["port"], self.machine)
        for sender in sorted(overdue):
            st["notified"].add(sender)
            self._notify(
                sender,
                f"⚠️ NOT DELIVERED: what you sent to {machine} has been waiting more than "
                f"{_duration(self.notice_after)}. Reason: {text} It stays queued on this "
                f"machine and sends automatically once {machine} is reachable."
            )

    def _ensure_listener(self, ip):
        if self._server is not None and self._server_addr == (ip, self.port):
            return
        self._close_listener()
        if time.monotonic() < self._listener_retry:
            return
        try:
            server = ThreadingHTTPServer((ip, self.port), _make_handler(self))
        except OSError as e:
            self._listener_error = f"cannot listen on {ip}:{self.port}: {e}"
            self._listener_retry = time.monotonic() + 30
            self.log(self._listener_error)
            return
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server, self._server_addr, self._listener_error = server, (ip, self.port), ""
        self.log(f"listening on {ip}:{self.port} as {self.machine}")

    def _close_listener(self):
        if self._server is not None:
            server, self._server, self._server_addr = self._server, None, None
            threading.Thread(target=lambda: (server.shutdown(), server.server_close()),
                             daemon=True).start()

    def _write_status(self, peers, force=False):
        body = {
            "pid": os.getpid(), "host": socket.gethostname(), "version": __version__,
            "port": self.port, "listening": self._server is not None,
            "listener_error": self._listener_error,
            "self": {"machine": self.machine, "ip": self.ip},
            "tailscale": {"state": self._tailscale.get("state"),
                          "detail": self._tailscale.get("detail")},
            "peers": {
                m: {"state": st["state"], "detail": st["detail"], "since_epoch": st["since"],
                    "last_contact_epoch": st["last_contact"] or None, "port": st["port"],
                    "queued": queued_count(self.root, m)}
                for m, st in list(self._peers.items()) if m in peers
            },
        }
        now = time.time()
        if not force and body == self._status_last and now - self._status_at < STATUS_WRITE_SECONDS:
            return
        self._status_last, self._status_at = body, now
        path = status_file(self.root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, {**body, "updated_epoch": now})
        except OSError as e:
            self.log(f"could not write status: {e}")

    def tick(self):
        """One pass: refresh identity, exchange with due peers, send notices, write status."""
        peers = read_peers(self.root)
        info = self.net.self_info()
        self._tailscale = info
        running = info.get("state") == "running"
        if running:
            self.machine, self.ip = info["machine"], info["ip"]
            self._ensure_listener(self.ip)
        else:
            self._close_listener()
        if peers or not peers_file(self.root).exists():
            with self._lock:
                for machine in list(self._peers):
                    if machine not in peers:
                        del self._peers[machine]
        self._write_status(peers)
        for machine, cfg in peers.items():
            st = self._peer(machine)
            if not running:
                self._set_state(st, "tailscale_down", info.get("detail") or "")
            elif machine == self.machine:
                self._set_state(st, "error", "that is this machine's own name")
            elif self.clock() >= st["next_due"]:
                self._exchange(machine, cfg, st)
                self._write_status(peers)
            self._notices(machine, st)
        self._write_status(peers)
        return peers

    def _stop_requested(self):
        path = stop_file(self.root)
        try:
            target = path.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if target in ("any", str(os.getpid())):
            try:
                path.unlink()
            except OSError:
                pass
            return True
        return False

    def stop(self):
        self._stop.set()

    def close(self):
        self._close_listener()
        try:
            st = read_status(self.root)
            if st.get("pid") == os.getpid():
                write_json_atomic(status_file(self.root), {**st, "updated_epoch": 0})
        except OSError:
            pass

    def run_forever(self):
        self.log(f"relay {__version__} starting (pid {os.getpid()}, root {self.root})")
        empty_since = None
        while not self._stop.is_set():
            if self._stop_requested():
                self.log("stop requested")
                break
            try:
                peers = self.tick()
            except Exception as e:
                self.log(f"tick failed: {type(e).__name__}: {e}")
                peers = read_peers(self.root)
            if peers:
                empty_since = None
            else:
                empty_since = empty_since or time.monotonic()
                if time.monotonic() - empty_since > 10:
                    self.log("no approved peers; exiting")
                    break
            self._stop.wait(LOOP_SECONDS)
        self.close()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="python -m teammate_comms.relay",
        description="Run the cross-machine relay for this machine's comms root.")
    parser.add_argument("--comms-dir", dest="comms_dir", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--status", action="store_true",
                        help="Print the running relay's status and exit.")
    args = parser.parse_args(argv)
    root, _source = resolve_comms_root(args.comms_dir)
    if args.status:
        status = read_status(root)
        print(json.dumps({**status, "alive": relay_alive(root, status)}, indent=2))
        return 0
    get_relay_dir(root).mkdir(parents=True, exist_ok=True)
    try:
        with file_lock(get_relay_dir(root) / "run", timeout=1):
            Relay(root, port=args.port).run_forever()
    except CommsError:
        print("a relay is already running for this comms root", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
