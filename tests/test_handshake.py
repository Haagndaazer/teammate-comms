"""Isolation test for the teammate-comms MCP server.

Drives `python -m teammate_comms.server` over a pipe with a temp comms root and
NO TEAMMATE_AGENT (so identity comes from an explicit teammate_register call, the
primary path). Asserts both halves of the unified server:

  Registration + tool gating:
    - tools/list returns 8 tools (register + 7), each with a valid object inputSchema
    - before registration, messaging tools return isError ("register first")
    - teammate_register (with a profile) establishes identity; teammate_whoami flips
      to registered and echoes the profile

  Profile fields:
    - teammate_register echoes the profile back (personality reminder at start)
    - `project` is auto-filled from CLAUDE_PROJECT_DIR's basename and shows in
      whoami / teammate_list / teammate_profile
    - teammate_update changes status; teammate_list always shows project/status/
      authority and includes personality; teammate_profile returns the full profile;
      a profile field SURVIVES a heartbeat cycle
    - the channel wake event leads with the personality reminder

  Channel half:
    - initialize echoes the id and advertises BOTH experimental['claude/channel']
      AND tools capabilities
    - a new inbox message (after registration) triggers notifications/claude/channel
    - the registry record is written, and type:"full" SURVIVES a heartbeat cycle

  Tool half + error paths (all stay alive):
    - send / inbox / ack round-trip; self-send, unknown tool, missing arg, bad name
      all return isError; an unknown JSON-RPC method returns -32601

  Version sync: __init__.py, plugin.json, pyproject.toml all agree.

Run:  uv run --no-dev python tests/test_handshake.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
AGENT = "test-chan"
PEER = "test-peer"
TEAM = "chtest"
ROLE = "test runner"
PERSONALITY = "methodical and dry"
STATUS_INIT = "booting up"
STATUS_NEW = "running checks"
AUTHORITY = "tests/**"
PROJECT = "MyTestProject"  # basename auto-filled from CLAUDE_PROJECT_DIR

stdout_lines = []
stderr_lines = []


def reader(stream, sink):
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            sink.append(line)


def send(proc, obj):
    proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
    proc.stdin.flush()


def inboxes_dir(root):
    return Path(root) / "TeammateComms" / TEAM / "inboxes"


def append_external_message(root, to, frm, message):
    """Simulate a peer's send by appending to <to>'s unread inbox directly."""
    d = inboxes_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{to}_unread.json"
    msgs = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    msgs.append({"id": f"ext-{time.time()}", "from": frm, "priority": "normal", "message": message})
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(msgs), encoding="utf-8")
    os.replace(tmp, f)


def main():
    root = tempfile.mkdtemp(prefix="teammate-comms-test-")
    record = Path(root) / "TeammateComms" / TEAM / "agents" / f"{AGENT}.json"

    env = dict(os.environ)
    env["TEAMMATE_COMMS_DIR"] = root
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("TEAMMATE_AGENT", None)   # force explicit registration (no auto-register)
    env.pop("TEAMMATE_TEAM", None)
    # Comms root comes from TEAMMATE_COMMS_DIR above; CLAUDE_PROJECT_DIR now only
    # feeds the auto-filled `project` profile field (no longer the comms root).
    env["CLAUDE_PROJECT_DIR"] = f"C:/some/path/{PROJECT}"
    env.pop("CLAUDE_CONFIG_DIR", None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "teammate_comms.server"],
        cwd=str(REPO), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env,
    )
    threading.Thread(target=reader, args=(proc.stdout, stdout_lines), daemon=True).start()
    threading.Thread(target=reader, args=(proc.stderr, stderr_lines), daemon=True).start()

    # Handshake
    send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    time.sleep(0.4)
    send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    time.sleep(0.4)

    # Tool surface + gating BEFORE registration
    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teammate_inbox", "arguments": {}}})       # not registered -> isError
    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "teammate_whoami", "arguments": {}}})       # registered:false
    time.sleep(0.3)

    # Register (WITH a profile), then verify identity + channel arming
    send(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "teammate_register",
                           "arguments": {"agent": AGENT, "team": TEAM, "role": ROLE,
                                         "personality": PERSONALITY, "status": STATUS_INIT,
                                         "authority": AUTHORITY}}})
    time.sleep(1.0)  # let the watcher seed its baseline (count 0)
    send(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "teammate_whoami", "arguments": {}}})       # registered:true + profile

    # Tool round-trips + error paths
    send(proc, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "teammate_send",
                           "arguments": {"to": PEER, "message": "hi peer", "priority": "urgent"}}})
    send(proc, {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "teammate_send", "arguments": {"to": AGENT, "message": "self"}}})   # self -> isError
    send(proc, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": "no_such_tool", "arguments": {}}})                                   # unknown -> isError
    send(proc, {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                "params": {"name": "teammate_send", "arguments": {"message": "no recipient"}}})         # missing 'to'
    send(proc, {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                "params": {"name": "teammate_send", "arguments": {"to": "../evil", "message": "x"}}})   # bad name
    send(proc, {"jsonrpc": "2.0", "id": 12, "method": "totally/unknown"})                               # -> -32601
    time.sleep(0.6)

    # New external message -> channel notification
    append_external_message(root, AGENT, "tester", "hello via channel")
    time.sleep(1.5)

    send(proc, {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                "params": {"name": "teammate_inbox", "arguments": {}}})
    send(proc, {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
                "params": {"name": "teammate_ack", "arguments": {"id": "all"}}})
    send(proc, {"jsonrpc": "2.0", "id": 15, "method": "ping"})
    time.sleep(0.6)

    # Profile: update status, then read it back via list + profile.
    send(proc, {"jsonrpc": "2.0", "id": 16, "method": "tools/call",
                "params": {"name": "teammate_update", "arguments": {"status": STATUS_NEW}}})
    send(proc, {"jsonrpc": "2.0", "id": 17, "method": "tools/call",
                "params": {"name": "teammate_list", "arguments": {}}})
    send(proc, {"jsonrpc": "2.0", "id": 18, "method": "tools/call",
                "params": {"name": "teammate_profile", "arguments": {}}})           # self full profile
    send(proc, {"jsonrpc": "2.0", "id": 19, "method": "tools/call",
                "params": {"name": "teammate_profile", "arguments": {"agent": PEER}}})  # no record -> isError
    time.sleep(0.6)

    # Heartbeat cycle (5s) -> confirm type:"full" AND a profile field survive the merge.
    time.sleep(5.5)
    type_after_heartbeat = None
    status_after_heartbeat = None
    if record.exists():
        rec_hb = json.loads(record.read_text(encoding="utf-8"))
        type_after_heartbeat = rec_hb.get("type")
        status_after_heartbeat = rec_hb.get("status")

    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    # ── assertions ──
    msgs = [json.loads(l) for l in stdout_lines]
    by_id = {m.get("id"): m for m in msgs if "id" in m}
    notifications = [m for m in msgs if m.get("method") == "notifications/claude/channel"]
    failures = []

    def result(i):
        return by_id.get(i, {}).get("result", {})

    def is_error(i):
        return result(i).get("isError") is True

    def text(i):
        return (result(i).get("content") or [{}])[0].get("text", "")

    # initialize: id echo + both capabilities
    init = by_id.get(1)
    if not init:
        failures.append("no initialize response")
    else:
        caps = init.get("result", {}).get("capabilities", {})
        if "claude/channel" not in caps.get("experimental", {}):
            failures.append(f"initialize missing claude/channel capability: {caps}")
        if "tools" not in caps:
            failures.append(f"initialize missing tools capability: {caps}")
        if init.get("result", {}).get("protocolVersion") != "2025-06-18":
            failures.append("initialize did not echo protocolVersion")

    # tools/list: 8 tools, each with an object inputSchema
    tl = result(2).get("tools")
    expected_names = {"teammate_register", "teammate_send", "teammate_inbox",
                      "teammate_ack", "teammate_list", "teammate_whoami",
                      "teammate_update", "teammate_profile"}
    if not isinstance(tl, list) or {t.get("name") for t in tl} != expected_names:
        failures.append(f"tools/list names mismatch: {tl}")
    else:
        for t in tl:
            sch = t.get("inputSchema")
            if not isinstance(sch, dict) or sch.get("type") != "object" or "properties" not in sch:
                failures.append(f"tool {t.get('name')} invalid inputSchema: {sch}")

    # gating before registration
    if not is_error(3):
        failures.append(f"teammate_inbox before register not isError: {by_id.get(3)}")
    if '"registered": false' not in text(4).lower():
        failures.append(f"whoami before register not unregistered: {text(4)}")

    # registration succeeded + whoami flips
    if is_error(5) or "Registered as" not in text(5):
        failures.append(f"teammate_register failed: {text(5)}")
    # register return echoes the profile back (personality reminder at session start)
    if PERSONALITY not in text(5) or ROLE not in text(5):
        failures.append(f"teammate_register did not echo profile: {text(5)}")
    if '"registered": true' not in text(6).lower() or AGENT not in text(6):
        failures.append(f"whoami after register wrong: {text(6)}")
    # whoami echoes the profile set at registration
    if STATUS_INIT not in text(6) or ROLE not in text(6):
        failures.append(f"whoami missing profile fields: {text(6)}")
    # project was auto-filled from CLAUDE_PROJECT_DIR's basename (not passed explicitly)
    if PROJECT not in text(6):
        failures.append(f"whoami missing auto-filled project {PROJECT!r}: {text(6)}")

    # send to peer wrote peer's inbox
    if is_error(7):
        failures.append(f"teammate_send to peer errored: {text(7)}")
    peer_inbox = inboxes_dir(root) / f"{PEER}_unread.json"
    if not peer_inbox.exists() or "hi peer" not in peer_inbox.read_text(encoding="utf-8"):
        failures.append("teammate_send did not write peer inbox")

    # error paths -> isError
    for i, label in [(8, "self-send"), (9, "unknown tool"), (10, "missing 'to'"), (11, "bad agent name")]:
        if not is_error(i):
            failures.append(f"{label} did not return isError: {by_id.get(i)}")

    # unknown method -> -32601
    if by_id.get(12, {}).get("error", {}).get("code") != -32601:
        failures.append(f"unknown method not -32601: {by_id.get(12)}")

    # channel notification fired
    if not notifications:
        failures.append("no notifications/claude/channel emitted for new message")
    elif notifications[0]["params"]["meta"].get("agent") != AGENT:
        failures.append(f"channel notification meta wrong: {notifications[0]['params']['meta']}")
    # wake event leads with the personality reminder
    elif PERSONALITY not in notifications[0]["params"].get("content", ""):
        failures.append(f"channel notification missing personality reminder: {notifications[0]['params'].get('content')}")

    # inbox shows message, ack clears, ping ok
    if "hello via channel" not in text(13):
        failures.append(f"teammate_inbox missing message: {text(13)}")
    if is_error(14) or "Acknowledged" not in text(14):
        failures.append(f"teammate_ack failed: {text(14)}")
    if by_id.get(15, {}).get("result") != {}:
        failures.append(f"ping result not empty: {by_id.get(15)}")

    # profile: update succeeded; list + profile reflect it
    if is_error(16) or "Profile updated" not in text(16):
        failures.append(f"teammate_update failed: {text(16)}")
    # teammate_list always surfaces project + status + authority, and the updated status
    if "project:" not in text(17) or "status:" not in text(17) or "authority:" not in text(17):
        failures.append(f"teammate_list missing project/status/authority labels: {text(17)}")
    if PROJECT not in text(17):
        failures.append(f"teammate_list missing project value {PROJECT!r}: {text(17)}")
    if STATUS_NEW not in text(17) or AUTHORITY not in text(17):
        failures.append(f"teammate_list missing updated status/authority values: {text(17)}")
    if PERSONALITY not in text(17):
        failures.append(f"teammate_list missing personality: {text(17)}")
    # teammate_profile (self) returns the full profile incl. personality
    for needle in (PROJECT, PERSONALITY, ROLE, STATUS_NEW, AUTHORITY):
        if needle not in text(18):
            failures.append(f"teammate_profile missing {needle!r}: {text(18)}")
    # teammate_profile for an agent with no registry record -> isError
    if not is_error(19):
        failures.append(f"teammate_profile for unregistered peer not isError: {by_id.get(19)}")

    # registry: written + type/profile survive heartbeat merge
    if not record.exists():
        failures.append(f"registry record not written at {record}")
    else:
        rec = json.loads(record.read_text(encoding="utf-8"))
        if rec.get("type") != "full" or "lastHeartbeat" not in rec:
            failures.append(f"registry record incomplete: {rec}")
    if type_after_heartbeat != "full":
        failures.append(f"type:'full' did not survive heartbeat (got {type_after_heartbeat!r})")
    if status_after_heartbeat != STATUS_NEW:
        failures.append(f"profile status did not survive heartbeat (got {status_after_heartbeat!r})")

    # version sync
    pkg = re.search(r'__version__\s*=\s*"([^"]+)"',
                    (SRC / "teammate_comms" / "__init__.py").read_text(encoding="utf-8")).group(1)
    plug = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    pyp = re.search(r'^version\s*=\s*"([^"]+)"',
                    (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE).group(1)
    if not (pkg == plug == pyp):
        failures.append(f"version drift: pkg={pkg} plugin={plug} pyproject={pyp}")

    # ── report ──
    print("=== STDOUT messages ===")
    for m in msgs:
        print(" ", json.dumps(m)[:200])
    print("=== STDERR (server diagnostics) ===")
    for l in stderr_lines:
        print(" ", l)

    if failures:
        print("\nFAIL:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
