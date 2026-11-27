"""Cross-machine relay: forwards comms events between machines over Tailscale.

``python -m teammate_comms.relay`` runs one relay per machine. Tools queue events as files
under ``<root>/TeammateComms/relay/outbox/<machine>/``; the relay exchanges them with each
approved peer and applies incoming events through the same local cores the tools use.
"""
import argparse
import calendar
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
from . import agentmail
from . import availability
from . import mailbox as mailbox_mod
from .comms import (
    MACHINE_NAME_PATTERN,
    MIGRATED_MARKER,
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
    message_ts,
    new_message_id,
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
    validate_project_dir,
    write_group_meta,
    write_json_atomic,
    write_shutdown_request,
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
CONTROL_KINDS = ("spawn", "shutdown")
CONTROL_TTL_SECONDS = 45
CLOCK_SLACK_SECONDS = 2
SPAWN_RATE = 5
SPAWN_RATE_SECONDS = 60
SPAWN_NAME_COOLDOWN_SECONDS = 120
REGISTER_WAIT_SECONDS = 90
SHUTDOWN_CONFIRM_SECONDS = 20
AWAIT_CHECK_SECONDS = 5
MAX_PROMPT_BYTES = 8192
MAX_PROJECT_DIR_CHARS = 1024
ORIG_PYTHONPATH_ENV = "TEAMMATE_RELAY_ORIG_PYTHONPATH"
_NO_PYTHONPATH = "-"
PROMPT_DASH_ERROR = ("a prompt cannot start with '-': the new session would read it as a "
                     "command-line option. Start it with a word")
ACCEPT_ACTION = "accept_remote_control"
REFUSE_ACTION = "refuse_remote_control"

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


def settings_file(root):
    return get_relay_dir(root) / "settings.json"


def spawned_file(root):
    return get_relay_dir(root) / "spawned.json"


def awaiting_file(root):
    return get_relay_dir(root) / "awaiting.json"


def read_awaiting(root):
    data = read_json_readonly(awaiting_file(root))
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and isinstance(v.get("by"), str)
            and v.get("kind") in CONTROL_KINDS}


def _await(root, target, by, kind):
    """Remember a request this machine carried out, so its outcome can be reported."""
    data = read_awaiting(root)
    for _ in range(3):
        if data or not awaiting_file(root).exists():
            break
        time.sleep(0.05)
        data = read_awaiting(root)
    data[target] = {"by": by, "kind": kind, "since": None}
    try:
        write_json_atomic(awaiting_file(root), data)
    except OSError as e:
        log_line(root, f"could not record the watch on {target}: {e}")


def log_line(root, message):
    try:
        path = log_file(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(path, path.with_suffix(".log.1"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{now_timestamp()} {message}\n")
    except OSError:
        pass


def accepts_remote_control(root):
    """True only when this machine's user turned remote control on. Unreadable means off."""
    data = read_json_readonly(settings_file(root))
    return isinstance(data, dict) and data.get("accept_remote_control") is True


def set_remote_control(root, on):
    path = settings_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        write_json_atomic(path, {"accept_remote_control": bool(on)})
    log_line(root, f"remote control turned {'ON' if on else 'off'}")
    if not on:
        return ("Remote control is OFF: no other machine can start or shut down agent "
                "sessions on this one.")
    peers = sorted(read_peers(root))
    return ("⚠️ Remote control is ON. Every approved peer machine, and any machine approved "
            "later, can now start agent sessions on this machine in any folder with any "
            "starting prompt and with permission prompts bypassed, and can shut down agent "
            f"sessions here. Approved peers: {', '.join(peers) or '(none yet)'}. Turn it off "
            f"with teammate_peers(action=\"{REFUSE_ACTION}\").")


def peer_accepts_control(root, machine):
    """True/False as the peer last reported, or None when it has not said (older version)."""
    value = (read_roster(root, machine) or {}).get("accepts_remote_control")
    return value if isinstance(value, bool) else None


def require_remote_control(root, machine):
    """Raise unless a control request to ``machine`` can be sent right now."""
    if is_mailbox(root, machine):
        raise CommsError(f"NOT SENT: {machine} is a cloud mailbox. An agent there cannot be "
                         f"started or shut down from here.")
    view = peer_view(root, machine)
    if not view["ok"]:
        raise CommsError(
            f"NOT SENT: {machine} cannot be reached right now, and a request like this is "
            f"never held for later. {view['text']}")
    roster = read_roster(root, machine)
    if not roster_fresh(roster):
        raise CommsError(
            f"NOT SENT: nothing current has been received from {machine} yet. Retry in "
            f"about {ROSTER_SECONDS} seconds.")
    accepts = peer_accepts_control(root, machine)
    if accepts is None:
        raise CommsError(
            f"NOT SENT: {machine} runs an older teammate-comms that cannot be controlled "
            f"from another machine. Update the plugin there and restart its sessions.")
    if not accepts:
        raise CommsError(
            f"NOT SENT: {machine} does not accept remote control. Its user can turn it on "
            f"from a session on {machine}: teammate_peers(action=\"{ACCEPT_ACTION}\"). If "
            f"that was just done, retry in about {ROSTER_SECONDS} seconds.")


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


def read_mailboxes(root):
    """Linked cloud mailboxes. A name that is also an approved peer is ignored."""
    peers = read_peers(root)
    return {name: cfg for name, cfg in mailbox_mod.read_mailboxes(root).items()
            if name not in peers}


def is_mailbox(root, machine):
    return machine in read_mailboxes(root)


def read_links(root):
    """Everything addressed as ``name@machine``: approved peers and linked mailboxes."""
    links = {name: {"mailbox": True} for name in read_mailboxes(root)}
    links.update(read_peers(root))
    return links


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
    if state == "mailbox_unreachable":
        return (f"the {machine} mailbox could not be reached from this machine"
                f"{' (' + detail + ')' if detail else ''}. Check this machine's internet "
                f"connection and that the mailbox is still deployed.")
    if state == "mailbox_refused":
        return (f"the {machine} mailbox does not accept this machine's key. The key was "
                f"changed or removed in the mailbox configuration; link again with "
                f"the new key file (your user runs: {link_command()}).")
    if state == "mailbox_error":
        return f"the {machine} mailbox answered with an error: {detail or 'no detail'}."
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
    if is_mailbox(root, machine):
        if started == "started":
            view = {**view, "ok": False, "state": "connecting"}
        return lines + _cloud_report(root, name, machine, view)
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


def _cloud_report(root, name, machine, view):
    if not view["ok"]:
        if view["state"] == "connecting":
            return [f"Not connected to the {machine} mailbox yet. The message is queued; "
                    f"'comms-relay' will message you if it cannot be delivered."]
        return [f"⚠️ WARNING: NOT DELIVERED — {view['text']} {view['queued']} message(s) are "
                f"waiting on this machine and send automatically once it works again."]
    entry = remote_agent(root, name, machine)
    if entry is None:
        known = [e.get("name") for e in (read_roster(root, machine) or {}).get("agents") or []
                 if isinstance(e, dict) and e.get("name")]
        return [f"NOTE: the {machine} mailbox has no cloud agent named {name!r}, so it will "
                f"refuse this message and 'comms-relay' will tell you. Cloud agents there: "
                f"{', '.join(known) or '(none reported yet)'}."]
    lines = [f"{name} is a cloud agent. The message goes to the {machine} mailbox within a "
             f"few seconds and {name} reads it when its next run starts; it is not "
             f"interrupted while a run is already going."]
    if entry.get("doorbell_error"):
        lines.append(f"⚠️ WARNING: {name}'s doorbell is not working: {entry['doorbell_error']}. "
                     f"It will see the message only when it next checks its mail.")
    elif not entry.get("doorbell"):
        lines.append(f"NOTE: no doorbell is set up for {name}, so nothing wakes it. It sees "
                     f"the message when it next checks its mail.")
    return lines


def request_stop(root, pid=None):
    path = stop_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid or read_status(root).get("pid") or "any"), encoding="utf-8")


def _version_key(value):
    return tuple(int(p) if p.isdigit() else 0 for p in str(value or "").split("."))


def stop_and_wait(root, seconds=STOP_WAIT_SECONDS, pid=None):
    if not relay_alive(root):
        return
    request_stop(root, pid)
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
    if machine in mailbox_mod.read_mailboxes(root):
        raise CommsError(
            f"{machine!r} is the name of a linked cloud mailbox, so it cannot also be a peer "
            f"machine. Unlink the mailbox first with "
            f"teammate_peers(action=\"{mailbox_mod.UNLINK_ACTION}\", machine=\"{machine}\").")
    update_peers(root, lambda peers: {**peers, machine: peers.get(machine, {})})
    lines = [f"Approved {machine!r} as a peer machine.", start_line(ensure_relay(root))]
    if accepts_remote_control(root):
        lines.append(f"⚠️ WARNING: remote control is ON for this machine, so {machine} can now "
                     f"start and shut down agent sessions here. Turn it off with "
                     f"teammate_peers(action=\"{REFUSE_ACTION}\") if that is not wanted.")
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


def link_mailbox(root, machine, url, key_file, net=None, post=None):
    net = net or TailscaleNet()
    me = net.self_info().get("machine")
    taken = {name: "it is an approved peer machine" for name in read_peers(root)}
    taken.update({name: "a device with that name is on this machine's tailnet"
                  for name in net.devices() if name not in taken})
    if me:
        taken[me] = "it is this machine's own name"
    name, you_are, bots = mailbox_mod.link(root, machine, url, key_file, taken, post=post)
    forget_peer(root, name)
    log_line(root, f"linked cloud mailbox {name} as {you_are}")
    lines = [f"Linked the cloud mailbox {name!r}. The mailbox knows this machine as "
             f"{you_are!r}.", start_line(ensure_relay(root))]
    if bots:
        lines.append("Cloud agents there, addressed for direct messages only: "
                     + ", ".join(f"{b}@{name}" for b in bots) + ".")
    lines.append("The key is now stored with this machine's comms data. Delete the key file, "
                 "or keep it in a password manager; do not leave a copy in a shared or "
                 "synced folder.")
    lines.append("A cloud agent runs outside your machines and can be steered by anything it "
                 "reads there. Cloud agents on one connection can use each other's names, and "
                 "anyone holding the connection's token can write as them. Their messages "
                 "arrive like any teammate's, with no warning line. Decide how far your agents "
                 "should act on them and say so in the standing instructions file your agents "
                 "read. The README section \"Trusting a cloud agent\" lists the risks.")
    return "\n".join(line for line in lines if line)


def link_command(root=None):
    """The terminal command a user runs to link a mailbox. Agents cannot link one."""
    plugin_root = Path(__file__).resolve().parents[2]
    where = f' --comms-dir "{root}"' if root else ""
    return (f'uv run --no-sync --directory "{plugin_root}" python -m teammate_comms.peers '
            f'{mailbox_mod.LINK_ACTION} <short name, e.g. cloudbot> <mailbox https address> '
            f'"<path of this machine\'s key file>"{where}')


def unlink_mailbox(root, machine):
    if not isinstance(machine, str) or not machine.strip():
        raise CommsError("'machine' is required: the name of a linked cloud mailbox, for "
                         "example cloudbot. teammate_peers() lists them.")
    name = mailbox_mod.unlink(root, machine)
    dropped = queued_count(root, name)
    forget_peer(root, name)
    log_line(root, f"unlinked cloud mailbox {name}")
    return (f"Unlinked the cloud mailbox {name!r}; this machine no longer contacts it." +
            (f" {dropped} undelivered message(s) for it were discarded." if dropped else "") +
            " To cut the link for good, also remove this machine's key from the mailbox "
            "configuration.")


def _mailbox_lines(root, status):
    boxes = read_mailboxes(root)
    hint = ("A cloud mailbox (for a cloud agent such as a Grok Bot) is linked by your user, "
            "who runs this in their own terminal; an agent never does: " + link_command(root))
    broken = [f"  - {name}: ⚠️ its stored key cannot be read by this user on this machine (the "
              f"comms folder was copied or restored from elsewhere). Unlink it with "
              f"teammate_peers(action=\"{mailbox_mod.UNLINK_ACTION}\", machine=\"{name}\"), "
              f"then your user links it again."
              for name in mailbox_mod.unreadable(root)]
    if not boxes and not broken:
        return ["Cloud mailboxes: none. " + hint]
    lines = ["Cloud mailboxes (direct messages only):"] + broken
    for name in sorted(boxes):
        view = peer_view(root, name, status)
        lines.append(f"  - {name}: " + (f"reachable as {boxes[name]['you_are']}." if view["ok"]
                                         else f"⚠️ {view['text']}"))
        for entry in (read_roster(root, name) or {}).get("agents") or []:
            lines.append(f"      {entry.get('name')}@{name}: {cloud_agent_state(entry)}")
    return lines + [hint]


def cloud_agent_state(entry, now=None):
    now = time.time() if now is None else now
    last = entry.get("last_seen")
    seen = (f"last contacted its mailbox {_duration(now - last)} ago"
            if isinstance(last, (int, float)) else "has never contacted its mailbox")
    waiting = entry.get("waiting") or 0
    parts = [seen, f"{waiting} message(s) waiting for it" if waiting else "nothing waiting"]
    if entry.get("doorbell_error"):
        parts.append(f"⚠️ doorbell problem: {entry['doorbell_error']}")
    elif not entry.get("doorbell"):
        parts.append("no doorbell set up")
    return "; ".join(parts)


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
    elif peers or read_mailboxes(root):
        lines = ["Relay: ⚠️ NOT RUNNING on this machine. It starts when a teammate registers "
                 "or with teammate_peers(action=\"restart\")."]
    else:
        lines = ["Relay: not running (no peers approved)."]
    if me["state"] == "running":
        lines.append(f"Tailscale: connected as {me['machine']}.")
    elif peers or not read_mailboxes(root):
        lines.append(f"Tailscale: ⚠️ NOT CONNECTED on this machine ({me['detail']}).")
    else:
        lines.append("Tailscale: not connected (not needed for a cloud mailbox).")
    if accepts_remote_control(root):
        lines.append("Remote control: ⚠️ ON. Every approved peer can start and shut down "
                     "agent sessions on this machine.")
    else:
        lines.append("Remote control: off. No other machine can start or shut down agent "
                     "sessions on this one.")
    if peers:
        lines.append("Approved peers:")
        for machine in sorted(peers):
            view = peer_view(root, machine, status)
            known = len((read_roster(root, machine) or {}).get("agents") or [])
            if view["ok"]:
                accepts = peer_accepts_control(root, machine)
                control = ("accepts remote control from this machine" if accepts else
                           "does not accept remote control" if accepts is False else
                           "older version, cannot be remote controlled")
                lines.append(f"  - {machine}: reachable, {known} teammate(s) known; {control}.")
            else:
                waiting = f" {view['queued']} event(s) waiting." if view["queued"] else ""
                lines.append(f"  - {machine}: ⚠️ {view['text']}{waiting}")
    else:
        lines.append("Approved peers: none.")
    lines += _mailbox_lines(root, status)
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
        if not read_links(root) or (get_relay_dir(root).parent / MIGRATED_MARKER).exists():
            return "no_peers"
        status = read_status(root)
        current = relay_alive(root, status) and (
            _version_key(status.get("version")) >= _version_key(__version__))
        if current:
            return "running"
        if os.environ.get(AUTOSTART_ENV, "").strip() == "0":
            return "disabled"
        if relay_alive(root, status):
            stop_and_wait(root, pid=status.get("pid"))
        env = dict(os.environ)
        env[ORIG_PYTHONPATH_ENV] = env.get("PYTHONPATH") or _NO_PYTHONPATH
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
    tools.deliver_dm_record(root, None, record, to, report_live=False)


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


def _child_base_env():
    """The relay's environment without the PYTHONPATH entry it was started with."""
    env = dict(os.environ)
    orig = env.pop(ORIG_PYTHONPATH_ENV, None)
    if orig is not None:
        if orig == _NO_PYTHONPATH:
            env.pop("PYTHONPATH", None)
        else:
            env["PYTHONPATH"] = orig
    return env


def _require_control(root, names):
    if not accepts_remote_control(root) or names.src not in read_peers(root):
        raise RejectEvent(
            f"{names.me} does not accept remote control. Its user can turn it on from a "
            f"session on {names.me}: teammate_peers(action=\"{ACCEPT_ACTION}\")")


_spawn_times = {}


def _spawn_slot(root, src, target):
    """Refuse a burst from one machine or a second launch of one name; else record this one."""
    now = time.monotonic()
    recent = _spawn_times.setdefault(src, collections.deque())
    while recent and now - recent[0] > SPAWN_RATE_SECONDS:
        recent.popleft()
    if len(recent) >= SPAWN_RATE:
        raise RejectEvent(f"more than {SPAWN_RATE} spawn requests from {src} in a minute; "
                          f"wait before asking again")
    data = read_json_readonly(spawned_file(root))
    launched = data if isinstance(data, dict) else {}
    wall = time.time()
    key = target.lower()
    try:
        since = wall - float(launched.get(key) or 0)
    except (TypeError, ValueError):
        since = SPAWN_NAME_COOLDOWN_SECONDS
    if abs(since) < SPAWN_NAME_COOLDOWN_SECONDS:
        raise RejectEvent(f"{target} was launched here {max(0, int(since))} seconds ago; wait "
                          f"for it to register instead of launching it again")
    launched = {k: v for k, v in launched.items()
                if isinstance(v, (int, float)) and abs(wall - v) < SPAWN_NAME_COOLDOWN_SECONDS}
    launched[key] = wall
    try:
        write_json_atomic(spawned_file(root), launched)
    except OSError as e:
        raise RejectEvent(f"could not record the launch: {e}")
    recent.append(now)


def _release_spawn_slot(root, target):
    target = target.lower()
    data = read_json_readonly(spawned_file(root))
    if isinstance(data, dict) and target in data:
        data.pop(target)
        try:
            write_json_atomic(spawned_file(root), data)
        except OSError:
            pass


def _folder_trust(name, project_dir):
    from . import harness
    return harness.folder_trusted(name, project_dir)


def _apply_spawn(root, names, payload):
    from . import harness, spawn
    _require_control(root, names)
    by = names.actor(payload.get("by"))
    target = names.target(payload.get("agent"))
    record = read_agent_record(root, None, target)
    if record and record.get("type") == "human":
        raise RejectEvent(f"{target} is the human operator on {names.me}")
    if record and is_channel_alive(record):
        raise RejectEvent(f"{target} is already live on {names.me}")
    try:
        project_dir = validate_project_dir(payload.get("project_dir"))
    except CommsError as e:
        raise RejectEvent(f"on {names.me}: {e}")
    kind = harness.HARNESSES[harness.DEFAULT]
    if payload.get("harness"):
        kind = harness.by_name(payload.get("harness"))
        if kind is None:
            raise RejectEvent(f"unknown harness {payload.get('harness')!r}")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = kind.spawn_prompt.format(agent=target, project_dir=project_dir)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise RejectEvent(f"prompt is larger than {MAX_PROMPT_BYTES} bytes")
    if prompt.lstrip().startswith("-"):
        raise RejectEvent(PROMPT_DASH_ERROR)
    if _folder_trust(kind.name, project_dir) is False:
        raise RejectEvent(
            f"{kind.display_name} on {names.me} has no record of trusting {project_dir}, so "
            f"the new session would stop at the question asking whether to trust that "
            f"folder, with nobody there to answer. Open {kind.display_name} in that folder "
            f"once on {names.me} and accept, or pick a folder that has been used there before")
    _spawn_slot(root, names.src, target)
    try:
        argv = spawn.build_command(kind.spawn_builder, prompt, project_dir)
        env = spawn.build_remote_child_env(_child_base_env(), target, str(project_dir), root, by)
        spawn.spawn_in_terminal(argv, project_dir, env)
    except Exception as e:
        _release_spawn_slot(root, target)
        log_line(root, f"spawn of {target} for {by} failed: {type(e).__name__}: {e}")
        raise RejectEvent(f"could not launch on {names.me}: {e}")
    _await(root, target, by, "spawn")
    log_line(root, f"spawned {target} in {project_dir} for {by}; prompt starts "
                   f"{' '.join(prompt.split())[:80]!r}")


def _apply_shutdown(root, names, payload):
    from . import tools
    _require_control(root, names)
    by = names.actor(payload.get("by"))
    target = names.target(payload.get("agent"))
    record = read_agent_record(root, None, target)
    try:
        tools.check_shutdown_target(record, target)
        write_shutdown_request(root, None, target, by, record.get("instance_id"))
    except CommsError as e:
        raise RejectEvent(f"on {names.me}: {e}")
    except OSError as e:
        raise RejectEvent(f"on {names.me}: could not write the request: {e}")
    _await(root, target, by, "shutdown")
    log_line(root, f"shutdown of {target} requested by {by}")


def cloud_matches(root, short):
    """The agent sessions on this machine whose name a mailbox carries as ``short``."""
    out = []
    for path in sorted(get_agents_dir(root, None).glob(f"{short}*.json")):
        if mailbox_mod.cloud_name(path.stem) == short:
            record = read_agent_record(root, None, path.stem)
            if record and record.get("type") == "full":
                out.append(path.stem)
    return out


def deliver_cloud_message(root, mailbox, me, bots, msg, now=None):
    """Put one message from a cloud agent into a local inbox.

    Only a direct message to a registered agent session is ever accepted, at normal
    priority, from a cloud agent the mailbox itself reported.
    """
    from . import tools
    sender, to = msg.get("from"), msg.get("to")
    if not isinstance(sender, str) or sender not in bots:
        raise RejectEvent("the sender is not a cloud agent of this mailbox")
    try:
        validate_agent_name(to)
        text = tools._clean_message(msg.get("message"))
    except CommsError as e:
        raise RejectEvent(str(e))
    if len(to) == mailbox_mod.CLOUD_NAME_CHARS:
        matches = cloud_matches(root, to)
        if len(matches) > 1:
            raise RejectEvent(f"{to} is the shortened form of more than one teammate on {me}, "
                              f"so it is not clear who this is for")
        to = matches[0] if matches else to
    record = read_agent_record(root, None, to)
    if not record or record.get("type") != "full":
        raise RejectEvent(f"no teammate named {to} is registered on {me}")
    msg_id = _clean_id(msg.get("msg_id"))
    if msg_id != msg_id.strip():
        raise RejectEvent("bad message id")
    now = time.time() if now is None else now
    try:
        stamped = calendar.timegm(time.strptime(msg_id[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        raise RejectEvent(f"bad message id {msg_id!r}")
    if not now - mailbox_mod.ID_PAST_SECONDS <= stamped <= now + mailbox_mod.ID_FUTURE_SECONDS:
        raise RejectEvent("the message's timestamp is too far from this machine's clock")
    out = {"id": msg_id, "from": f"{sender}@{mailbox}", "priority": "normal",
           "message": mailbox_mod.neutralise(text)}
    reply_to = msg.get("reply_to")
    if isinstance(reply_to, str) and reply_to.strip():
        out["reply_to"] = " ".join(reply_to.split())[:128]
    ts = clean_ts(msg.get("ts"))
    out["ts"] = ts if ts is not None else message_ts()
    if _already_here(root, out):
        return
    tools.deliver_dm_record(root, None, out, to, report_live=False)


def deliver_email(root, mailbox, addresses, record, now=None):
    """Put one email a mailbox received into its owner's inbox, from the reserved email sender."""
    from . import tools
    msg_id = _clean_id(record.get("msg_id")) if isinstance(record, dict) else ""
    now = time.time() if now is None else now
    try:
        stamped = calendar.timegm(time.strptime(msg_id[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        raise RejectEvent("bad message id")
    if (msg_id != msg_id.strip() or not _MSG_ID_RE.fullmatch(msg_id)
            or not now - mailbox_mod.ID_PAST_SECONDS <= stamped <= now + mailbox_mod.ID_FUTURE_SECONDS):
        raise RejectEvent("bad message id")

    def put(out, owner):
        if not _already_here(root, out):
            tools.deliver_dm_record(root, None, out, owner, report_live=False)
    try:
        agentmail.deliver(root, mailbox, addresses, record, put)
    except agentmail.BadEmail as e:
        raise RejectEvent(str(e))


_APPLIERS = {
    "spawn": _apply_spawn,
    "shutdown": _apply_shutdown,
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
    return {"agents": agents, "accepts_remote_control": accepts_remote_control(root)}


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
        self._await_checked = float("-inf")
        self._server = None
        self._server_addr = None
        self._listener_error = ""
        self._listener_retry = 0.0
        self._peers = {}
        self._seen = {}
        self._attempts = {}
        self._stop = threading.Event()
        self._mailbox_post = mailbox_mod.post_sync
        self._fetch_piece = mailbox_mod.fetch_piece
        self._email_swept = 0.0
        self._inbound = {}
        self._status_at = 0.0
        self._status_last = None
        self._rejected_at = {}

    def log(self, message):
        log_line(self.root, message)

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
                if ev.get("kind") in CONTROL_KINDS:
                    age = ev.get("age")
                    if (isinstance(age, bool) or not isinstance(age, (int, float))
                            or not 0 <= age <= CONTROL_TTL_SECONDS):
                        raise RejectEvent("the request arrived too late to be carried out safely")
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
            if event["kind"] in CONTROL_KINDS:
                if self._control_expired(ev):
                    self._drop_control(machine, st, path, ev)
                    continue
                event["age"] = max(0.0, self.clock() - float(ev["queued_epoch"]))
            size += len(raw.encode("utf-8"))
            if events and size > MAX_BATCH_BYTES:
                break
            events.append(event)
            st["inflight"][event["id"]] = path
        return events

    def _control_expired(self, ev):
        try:
            age = self.clock() - float(ev.get("queued_epoch") or 0)
        except (TypeError, ValueError):
            return True
        return not -CLOCK_SLACK_SECONDS <= age <= CONTROL_TTL_SECONDS

    def _sweep_control(self, machine, st):
        """Drop expired control requests whether or not the peer can be reached."""
        d = outbox_dir(self.root, machine)
        if not d.exists():
            return
        with self._lock:
            for path in sorted(d.glob("*.json"))[:MAX_EVENTS]:
                try:
                    ev = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if (isinstance(ev, dict) and ev.get("kind") in CONTROL_KINDS
                        and self._control_expired(ev)):
                    self._drop_control(machine, st, path, ev)

    def _control_label(self, ev):
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        name = split_address(str(payload.get("agent") or "the teammate"))[0]
        return name, ("start" if ev.get("kind") == "spawn" else "shut down")

    def _drop_control(self, machine, st, path, ev):
        """A control request is never delivered late: drop it and tell whoever asked."""
        st["inflight"].pop(ev.get("id"), None)
        try:
            path.unlink()
        except OSError:
            return
        name, verb = self._control_label(ev)
        self.log(f"dropped unconfirmed {ev.get('kind')} {ev.get('id')} for {machine}")
        sender = ev.get("sender")
        if isinstance(sender, str) and not is_remote(sender) and sender != RELAY_SENDER:
            reason = ("" if st["state"] == "ok" else " Reason: " + describe_state(
                st["state"], machine, st["detail"], st["port"], self.machine))
            self._notify(
                sender,
                f"⚠️ NOT CONFIRMED: your request to {verb} {name} on {machine} was not "
                f"confirmed within {CONTROL_TTL_SECONDS} seconds. It will not be sent "
                f"again and can no longer be carried out, but it may already have been if "
                f"only the answer was lost.{reason} Check teammate_list before asking again.")

    def _confirm_control(self, machine, path):
        try:
            ev = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        sender = ev.get("sender")
        if (ev.get("kind") not in CONTROL_KINDS or not isinstance(sender, str)
                or is_remote(sender) or sender == RELAY_SENDER):
            return
        name, _verb = self._control_label(ev)
        if ev.get("kind") == "spawn":
            text = (f"✅ {machine} launched a terminal for {name}. This confirms the launch, "
                    f"not registration: check teammate_list in about 20 seconds. If it has "
                    f"not come online after {REGISTER_WAIT_SECONDS} seconds, {machine} "
                    f"messages you to say so.")
        else:
            text = (f"✅ {machine} accepted the request to shut down {name}. Check "
                    f"teammate_list to confirm it is no longer live. If it is still running "
                    f"after {SHUTDOWN_CONFIRM_SECONDS} seconds, {machine} messages you.")
        self._notify(sender, text)

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
                what = "direct message" if ev.get("kind") == "dm" else ev.get("kind")
                self._notify(sender, f"⚠️ REJECTED by {machine}: your {what} was not "
                                     f"applied there. Reason: {error}")
        ids = [i for i in (applied if isinstance(applied, list) else []) if isinstance(i, str)]
        confirmed = set(ids)
        ids += [i.get("id") for i in (failed if isinstance(failed, list) else [])
                if isinstance(i, dict)]
        for event_id in ids:
            path = st["inflight"].pop(event_id, None)
            if path is not None:
                if event_id in confirmed:
                    self._confirm_control(machine, path)
                try:
                    path.unlink()
                except OSError:
                    pass

    def _store_roster(self, machine, roster):
        path = roster_file(self.root, machine)
        path.parent.mkdir(parents=True, exist_ok=True)
        accepts = roster.get("accepts_remote_control")
        write_json_atomic(path, {"machine": machine, "received_epoch": time.time(),
                                 "agents": _clean_roster(roster),
                                 "accepts_remote_control": accepts
                                 if isinstance(accepts, bool) else None})

    @staticmethod
    def _mailbox_setter(raw, mailbox):
        """Who a mailbox says set the status, kept only if it is a well-formed name."""
        by = raw.get("updated_by")
        try:
            if raw.get("cloud") is True:
                validate_agent_name(by)
                return f"{by}@{mailbox}"
            if isinstance(by, str) and "@" in by:
                return validate_address(by)
        except CommsError:
            pass
        return f"someone through the {mailbox} mailbox"

    def _merge_availability(self, raw, me, mailbox=None):
        try:
            if mailbox and isinstance(raw, dict):
                raw = {**raw, "updated_by": self._mailbox_setter(raw, mailbox)}
            state = availability.from_wire(raw, me)
            if state and availability.merge(self.root, state, behaviour=mailbox is None):
                self.log(f"user availability updated from {mailbox or 'a linked machine'}: "
                         f"seq {state['seq']}")
        except Exception as e:
            self.log(f"could not merge user availability: {type(e).__name__}: {e}")

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
        if isinstance(body.get("roster"), dict):
            self._merge_availability(body["roster"].get("availability"), self.machine)
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
            body["roster"]["availability"] = availability.wire(
                availability.read(self.root), self.machine)
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

    def _mailbox_batch(self, name, st):
        """Queued direct messages for a mailbox, in its wire form. Anything else is dropped."""
        d = outbox_dir(self.root, name)
        if not d.exists():
            return []
        events, size = [], 0
        for path in sorted(d.glob("*.json"))[:mailbox_mod.BATCH_EVENTS]:
            try:
                raw = path.read_text(encoding="utf-8")
                ev = json.loads(raw)
                event_id, kind, payload = ev["id"], ev["kind"], ev["payload"]
                record = payload["record"] if kind == "dm" else None
                to = split_address(payload["to"])[0] if kind == "dm" else None
                wire = None if kind != "dm" else {
                    "id": event_id, "to": to, "from": mailbox_mod.cloud_name(record["from"]),
                    "message": record["message"], "priority": record.get("priority"),
                    "msg_id": record["id"], "ts": record.get("ts")}
            except (OSError, ValueError, KeyError, TypeError):
                self._quarantine(path)
                continue
            if wire is None:
                self._drop_not_carried(name, path, ev)
                continue
            if isinstance(record.get("reply_to"), str):
                wire["reply_to"] = record["reply_to"]
            size += len(raw.encode("utf-8"))
            if events and size > mailbox_mod.BATCH_BYTES:
                break
            events.append(wire)
            st["inflight"][event_id] = path
        return events

    def _drop_not_carried(self, name, path, ev):
        try:
            path.unlink()
        except OSError:
            return
        kind = ev.get("kind")
        self.log(f"dropped {kind} {ev.get('id')} for mailbox {name}: only direct messages")
        sender = ev.get("sender")
        if (kind != "reaction" and isinstance(sender, str) and not is_remote(sender)
                and sender != RELAY_SENDER):
            self._notify(sender, f"⚠️ NOT SENT: {name} is a cloud mailbox and carries direct "
                                 f"messages only, so your {kind} did not reach the cloud "
                                 f"agents there.")

    def _mailbox_roster(self):
        by_short = {}
        for entry in build_roster(self.root)["agents"]:
            if entry.get("type") == "full":
                by_short.setdefault(mailbox_mod.cloud_name(entry["name"]), []).append(entry)
        return [{"name": short, "live": bool(found[0].get("live"))}
                for short, found in by_short.items() if len(found) == 1]

    def _inbound_room(self, name, sender):
        """Whether one cloud agent, and its mailbox as a whole, may deliver another message."""
        now = self.clock()
        room = True
        for key, limit in (((name, sender), mailbox_mod.INBOUND_PER_WINDOW),
                           ((name, None), mailbox_mod.INBOUND_MAILBOX_PER_WINDOW)):
            recent = self._inbound.get(key)
            while recent and now - recent[0] > mailbox_mod.INBOUND_WINDOW_SECONDS:
                recent.popleft()
            if recent is not None and not recent:
                del self._inbound[key]
            room = room and len(recent or ()) < limit
        return room

    def _inbound_count(self, name, sender):
        for key in ((name, sender), (name, None)):
            self._inbound.setdefault(key, collections.deque()).append(self.clock())

    def _apply_cloud(self, name, cfg, bots, messages):
        applied, failed = [], []
        ring = self._seen_ring(name)
        known = {b["name"] for b in bots}
        capped = set()
        started = time.monotonic()
        for msg in (messages if isinstance(messages, list) else [])[:MAX_EVENTS]:
            if time.monotonic() - started > mailbox_mod.APPLY_BUDGET_SECONDS:
                break
            event_id = msg.get("id") if isinstance(msg, dict) else None
            if not isinstance(event_id, str) or not _ID_RE.fullmatch(event_id):
                self.log(f"mailbox {name} sent a message with no usable id; ignored")
                continue
            if event_id in ring["ids"]:
                if event_id in ring["failed"]:
                    failed.append({"id": event_id, "error": ring["failed"][event_id]})
                else:
                    applied.append(event_id)
                continue
            sender = msg.get("from") if isinstance(msg.get("from"), str) else ""
            error = ""
            try:
                if sender in known and not self._inbound_room(name, sender):
                    if sender not in capped:
                        capped.add(sender)
                        self.log(f"mailbox {name}: {sender} is over the inbound limit; its "
                                 f"further messages are refused")
                    raise RejectEvent(mailbox_mod.RATE_REFUSAL)
                deliver_cloud_message(self.root, name, cfg["you_are"], known, msg, self.clock())
                applied.append(event_id)
                self._inbound_count(name, sender)
            except RejectEvent as e:
                error = str(e)[:300] or "rejected"
                if error != mailbox_mod.RATE_REFUSAL:
                    self.log(f"rejected message {event_id} from mailbox {name}: {e}")
            except Exception as e:
                tries = self._attempts.get(event_id, 0) + 1
                self._attempts[event_id] = tries
                self.log(f"message {event_id} from mailbox {name} failed (attempt {tries}): "
                         f"{type(e).__name__}: {e}")
                locked = isinstance(e, CommsError) and "Could not acquire lock" in str(e)
                if tries < (MAX_LOCK_ATTEMPTS if locked else MAX_APPLY_ATTEMPTS):
                    break
                error = f"it could not be stored on {cfg['you_are']}; try again later"
            if error:
                failed.append({"id": event_id, "error": error})
            self._attempts.pop(event_id, None)
            self._seen_add(name, event_id, error)
        return applied, failed

    def _apply_emails(self, name, resp):
        addresses = agentmail.clean_addresses(resp.get("addresses"))
        try:
            agentmail.save_addresses(self.root, name, addresses,
                                     agentmail.clean_counts(resp.get("email_counts"), addresses))
        except (OSError, CommsError) as e:
            self.log(f"could not store the email addresses of mailbox {name}: {e}")
        applied, failed = [], []
        ring = self._seen_ring(name)
        emails = resp.get("emails")
        for record in (emails if isinstance(emails, list) else [])[:MAX_EVENTS]:
            email_id = record.get("id") if isinstance(record, dict) else None
            if not isinstance(email_id, str) or not _ID_RE.fullmatch(email_id):
                continue
            if email_id in ring["ids"]:
                if email_id in ring["failed"]:
                    failed.append({"id": email_id, "error": ring["failed"][email_id]})
                else:
                    applied.append(email_id)
                continue
            if not self._inbound_room(name, agentmail.EMAIL_SENDER):
                break
            error = ""
            try:
                deliver_email(self.root, name, addresses, record, self.clock())
                applied.append(email_id)
                self._inbound_count(name, agentmail.EMAIL_SENDER)
            except RejectEvent as e:
                error = str(e)[:300] or "rejected"
                self.log(f"rejected email {email_id} from mailbox {name}: {e}")
                failed.append({"id": email_id, "error": error})
            except Exception as e:
                tries = self._attempts.get(email_id, 0) + 1
                self._attempts[email_id] = tries
                self.log(f"email {email_id} from mailbox {name} failed (attempt {tries}): "
                         f"{type(e).__name__}: {e}")
                if tries < MAX_APPLY_ATTEMPTS:
                    break
                error = "it could not be stored on this machine"
                failed.append({"id": email_id, "error": error})
            self._attempts.pop(email_id, None)
            self._seen_add(name, email_id, error)
        return applied, failed

    def email_tick(self):
        """Download attachments that are wanted and clear old download folders."""
        from . import tools
        boxes = read_mailboxes(self.root)
        fetchers = {name: (lambda i, n, p, cfg=cfg: self._fetch_piece(cfg["url"], cfg["key"], i, n, p))
                    for name, cfg in boxes.items()}

        def put(out, owner):
            tools.deliver_dm_record(self.root, None, out, owner, report_live=False)
        if (agentmail.email_dir(self.root) / "held").is_dir():
            agentmail.download_tick(self.root, fetchers, put, self.log)
        if self.clock() - self._email_swept > 3600:
            self._email_swept = self.clock()
            agentmail.sweep(self.root)

    def _email_loop(self):
        while not self._stop.is_set():
            try:
                self.email_tick()
            except Exception as e:
                self.log(f"email pass failed: {type(e).__name__}: {e}")
            self._stop.wait(LOOP_SECONDS * 2)

    def _mailbox_sync(self, name, cfg, st):
        now = self.clock()
        with self._lock:
            batch = self._mailbox_batch(name, st)
            acks, nacks = list(st["acks"]), list(st["nacks"])
        body = {"v": mailbox_mod.PROTOCOL, "client": __version__, "events": batch,
                "ack": acks, "failed": nacks,
                "availability": availability.wire(availability.read(self.root),
                                                  cfg["you_are"])}
        fetched = agentmail.done_ids(self.root, name)
        if fetched:
            body["attachments_done"] = fetched
        roster = self._mailbox_roster()
        since = now - st["roster_sent"]
        send_roster = since >= mailbox_mod.ROSTER_REFRESH_SECONDS or (
            since >= ROSTER_SECONDS and roster != st.get("roster_last"))
        if send_roster:
            body["roster"] = roster
        try:
            status, resp = self._mailbox_post(cfg["url"], cfg["key"], body)
        except Exception as e:
            timed_out = isinstance(e, (TimeoutError, socket.timeout))
            return self._contact_failed(st, "mailbox_unreachable",
                                        "it did not answer in time" if timed_out
                                        else "the connection failed")
        if status != 200 or resp.get("v") != mailbox_mod.PROTOCOL:
            state, detail = (mailbox_mod.sync_failure(status, resp) if status != 200 else
                             ("mailbox_error", "it did not answer like a teammate-comms mailbox"))
            return self._contact_failed(st, state, detail)
        if _version_key(resp.get("worker")) < _version_key(mailbox_mod.MIN_WORKER):
            return self._contact_failed(
                st, "mailbox_error",
                "the deployed mailbox is older than this plugin. Your user deploys "
                "cloud-mailbox-worker again from the plugin folder (npx wrangler deploy)")
        if resp.get("you_are") != cfg["you_are"]:
            return self._contact_failed(
                st, "mailbox_error",
                f"it now calls this machine "
                f"{mailbox_mod.clean_note(resp.get('you_are'))[:64]!r}; unlink it and link "
                f"it again")
        bots = mailbox_mod.clean_bots(resp.get("bots"))
        self._merge_availability(resp.get("availability"), cfg["you_are"], mailbox=name)
        with self._lock:
            st["acks"] = [a for a in st["acks"] if a not in acks]
            st["nacks"] = [f for f in st["nacks"] if f not in nacks]
            if send_roster:
                st["roster_sent"], st["roster_last"] = now, roster

            def items(key):
                value = resp.get(key)
                return value if isinstance(value, list) else []

            def known_id(value):
                return isinstance(value, str) and value in st["inflight"]
            accepted = [i for i in items("accepted") if known_id(i)]
            rejected = [{"id": i["id"],
                         "error": "the mailbox said: " + (mailbox_mod.clean_note(i.get("error"))
                                                          or "no reason given")}
                        for i in items("rejected") if isinstance(i, dict) and known_id(i.get("id"))]
            self._settle(st, accepted, rejected, name)
            unanswered = [e["id"] for e in batch if e["id"] in st["inflight"]]
            for event_id in unanswered:
                st["inflight"].pop(event_id, None)
            st["no_verdict"] = bool(unanswered)
            incoming = items("messages")
        applied, failed = self._apply_cloud(name, cfg, bots, incoming)
        mail_applied, mail_failed = self._apply_emails(name, resp)
        applied, failed = applied + mail_applied, failed + mail_failed
        agentmail.forget_done(self.root, name, fetched)
        with self._lock:
            st["acks"] += applied
            st["nacks"] += failed
            try:
                path = roster_file(self.root, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                write_json_atomic(path, {"machine": name, "mailbox": True,
                                         "received_epoch": time.time(), "agents": bots})
            except OSError as e:
                self.log(f"could not store the roster of mailbox {name}: {e}")
            st["last_contact"] = self.clock()
            st["fails"] = 0
            self._set_state(st, "ok")
            waiting = queued_count(self.root, name)
        progressed = bool(applied or failed)
        st["synced_at"] = now
        st["next_due"] = now if progressed or (waiting and accepted) else (
            now + mailbox_mod.IDLE_SECONDS)

    def _mailbox_has_new(self, name, st):
        """True when something queued could be sent now, so the idle wait should be cut short."""
        if st["state"] != "ok" or st.get("no_verdict"):
            return False
        if self.clock() - st.get("synced_at", 0.0) < mailbox_mod.BUSY_SECONDS:
            return False
        d = outbox_dir(self.root, name)
        return d.exists() and any(d.glob("*.json"))

    def mailbox_tick(self):
        """One pass over the linked mailboxes: exchange with each one that is due."""
        for name, cfg in read_mailboxes(self.root).items():
            st = self._peer(name)
            if self.clock() >= st["next_due"] or self._mailbox_has_new(name, st):
                self._mailbox_sync(name, cfg, st)
            self._notices(name, st)

    def _mailbox_loop(self):
        while not self._stop.is_set():
            try:
                self.mailbox_tick()
            except Exception as e:
                self.log(f"mailbox pass failed: {type(e).__name__}: {e}")
            self._stop.wait(LOOP_SECONDS * 2)

    def _check_awaited(self):
        """Report a launched session that never came online, or a shutdown never acted on."""
        now = self.clock()
        if now - self._await_checked < AWAIT_CHECK_SECONDS:
            return
        self._await_checked = now
        reports, alive = [], {}
        for target in read_awaiting(self.root):
            record = read_agent_record(self.root, None, target)
            alive[target] = bool(record and is_channel_alive(record))
        if not alive:
            return
        with self._lock:
            data = read_awaiting(self.root)
            keep = {}
            for target, entry in data.items():
                if target not in alive:
                    keep[target] = entry
                    continue
                live = alive[target]
                spawn = entry["kind"] == "spawn"
                if live == spawn:
                    continue
                since = entry.get("since")
                if isinstance(since, bool) or not isinstance(since, (int, float)) or since > now:
                    since = entry["since"] = now
                wait = REGISTER_WAIT_SECONDS if spawn else SHUTDOWN_CONFIRM_SECONDS
                if now - since < wait:
                    keep[target] = entry
                    continue
                reports.append((target, entry["by"], spawn))
            try:
                write_json_atomic(awaiting_file(self.root), keep)
            except OSError as e:
                self.log(f"could not update the watch list: {e}")
                return
        for target, by, spawn in reports:
            if spawn:
                text = (f"⚠️ {target} was launched on {self.machine} about "
                        f"{REGISTER_WAIT_SECONDS} seconds ago and has NOT come online. Its "
                        f"window is most likely waiting for a person at {self.machine} to "
                        f"answer a question (folder trust, sign-in or a first-run prompt). "
                        f"Nothing is retried; someone has to look at that window.")
            else:
                text = (f"⚠️ {target} on {self.machine} is STILL RUNNING "
                        f"{SHUTDOWN_CONFIRM_SECONDS} seconds after its shutdown was accepted. "
                        f"Its session did not act on the request (it may be on an older "
                        f"version or not responding). Nothing was closed.")
            self.log(f"{target}: {'not online after launch' if spawn else 'still running after shutdown'}")
            try:
                _name, machine = split_address(by)
                if machine not in read_peers(self.root):
                    raise CommsError(f"{machine} is no longer an approved peer")
                record = {"id": new_message_id(self.root), "ts": message_ts(),
                          "from": RELAY_SENDER, "priority": "urgent", "message": text}
                enqueue(self.root, machine, "dm", {"record": record, "to": by},
                        sender=RELAY_SENDER)
            except Exception as e:
                self.log(f"could not tell {by}: {type(e).__name__}: {e}")

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
            if ev.get("kind") in CONTROL_KINDS:
                continue
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

    def _write_status(self, links, force=False):
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
                for m, st in list(self._peers.items()) if m in links
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
        links = read_links(self.root)
        info = self.net.self_info()
        self._tailscale = info
        running = info.get("state") == "running"
        if running:
            self.machine, self.ip = info["machine"], info["ip"]
            self._ensure_listener(self.ip)
        else:
            self._close_listener()
        if links or not peers_file(self.root).exists():
            with self._lock:
                for machine in list(self._peers):
                    if machine not in links:
                        del self._peers[machine]
        self._write_status(links)
        for machine, cfg in peers.items():
            st = self._peer(machine)
            if not running:
                self._set_state(st, "tailscale_down", info.get("detail") or "")
            elif machine == self.machine:
                self._set_state(st, "error", "that is this machine's own name")
            elif self.clock() >= st["next_due"]:
                self._exchange(machine, cfg, st)
                self._write_status(links)
            self._sweep_control(machine, st)
            self._notices(machine, st)
        self._check_awaited()
        self._write_status(links)
        return links

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
        self._stop.set()
        self._close_listener()
        try:
            st = read_status(self.root)
            if st.get("pid") == os.getpid():
                write_json_atomic(status_file(self.root), {**st, "updated_epoch": 0})
        except OSError:
            pass

    def run_forever(self):
        self.log(f"relay {__version__} starting (pid {os.getpid()}, root {self.root})")
        try:
            mailbox_mod.protect_stored(self.root)
        except Exception as e:
            self.log(f"could not protect stored mailbox keys: {type(e).__name__}")
        threading.Thread(target=self._mailbox_loop, daemon=True).start()
        threading.Thread(target=self._email_loop, daemon=True).start()
        empty_since = None
        while not self._stop.is_set():
            if self._stop_requested():
                self.log("stop requested")
                break
            try:
                peers = self.tick()
            except Exception as e:
                self.log(f"tick failed: {type(e).__name__}: {e}")
                peers = read_links(self.root)
            if peers:
                empty_since = None
            else:
                empty_since = empty_since or time.monotonic()
                if time.monotonic() - empty_since > 10:
                    self.log("no approved peers or mailboxes; exiting")
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
