"""Isolation test for the teammate-comms MCP server.

Drives `python -m teammate_comms.server` over a pipe with scripted JSON-RPC and a
temp comms root, asserting both halves of the unified server:

  Channel half (ported from the proven prototype test):
    - initialize echoes the request id and advertises BOTH
      capabilities.experimental['claude/channel'] AND capabilities.tools
    - a new inbox message (post-initialized) triggers notifications/claude/channel
    - the registry record agents/<agent>.json is written, and type:"full" SURVIVES
      a heartbeat cycle (the merge must not clobber it)

  Tool half (new):
    - tools/list returns the 5 tools, each with a valid object inputSchema
    - tools/call teammate_whoami / teammate_send / teammate_inbox / teammate_ack round-trip
    - error paths return isError:true AND the process stays alive:
        self-send, unknown tool, missing required arg, bad agent name
    - an unknown JSON-RPC method returns -32601 (and the process stays alive)

  Version sync:
    - plugin.json, pyproject.toml, and teammate_comms.__version__ all agree

Run:  uv run --no-dev python tests/test_handshake.py   (or: python tests/test_handshake.py)
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
    env["TEAMMATE_AGENT"] = AGENT
    env["TEAMMATE_TEAM"] = TEAM
    env["TEAMMATE_COMMS_DIR"] = root
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("CLAUDE_PROJECT_DIR", None)  # ensure TEAMMATE_COMMS_DIR is the path used

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
    time.sleep(0.5)
    send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    time.sleep(1.2)  # let watcher seed baseline (count 0) + first heartbeat

    # Tool surface + error paths
    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "teammate_whoami", "arguments": {}}})
    send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "teammate_send",
                           "arguments": {"to": PEER, "message": "hi peer", "priority": "urgent"}}})
    send(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "teammate_send",
                           "arguments": {"to": AGENT, "message": "to self"}}})  # self -> isError
    send(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "no_such_tool", "arguments": {}}})  # unknown -> isError
    send(proc, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "teammate_send", "arguments": {"message": "no recipient"}}})  # missing 'to'
    send(proc, {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "teammate_send",
                           "arguments": {"to": "../evil", "message": "x"}}})  # bad name -> isError
    send(proc, {"jsonrpc": "2.0", "id": 9, "method": "totally/unknown"})  # -> -32601
    time.sleep(0.8)

    # New external message -> should trigger a channel notification
    append_external_message(root, AGENT, "tester", "hello via channel")
    time.sleep(1.5)

    send(proc, {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                "params": {"name": "teammate_inbox", "arguments": {}}})
    send(proc, {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                "params": {"name": "teammate_ack", "arguments": {"id": "all"}}})
    send(proc, {"jsonrpc": "2.0", "id": 12, "method": "ping"})
    time.sleep(0.6)

    # Wait out a heartbeat cycle (5s) and confirm type:"full" survives the merge.
    time.sleep(5.5)
    type_after_heartbeat = None
    if record.exists():
        type_after_heartbeat = json.loads(record.read_text(encoding="utf-8")).get("type")

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

    def call_result(i):
        return by_id.get(i, {}).get("result", {})

    def is_error(i):
        return call_result(i).get("isError") is True

    def call_text(i):
        content = call_result(i).get("content") or [{}]
        return content[0].get("text", "")

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

    # tools/list: 5 tools, each with an object inputSchema
    tools_list = by_id.get(2, {}).get("result", {}).get("tools")
    if not isinstance(tools_list, list) or len(tools_list) != 5:
        failures.append(f"tools/list did not return 5 tools: {tools_list}")
    else:
        names = {t.get("name") for t in tools_list}
        expected = {"teammate_send", "teammate_inbox", "teammate_ack", "teammate_list", "teammate_whoami"}
        if names != expected:
            failures.append(f"tool names mismatch: {names}")
        for t in tools_list:
            sch = t.get("inputSchema")
            if not isinstance(sch, dict) or sch.get("type") != "object" or "properties" not in sch:
                failures.append(f"tool {t.get('name')} has invalid inputSchema: {sch}")

    # whoami reports the resolved root + source
    who = call_text(3)
    if '"agent": "test-chan"' not in who or "TEAMMATE_COMMS_DIR" not in who:
        failures.append(f"teammate_whoami unexpected: {who}")

    # send to peer succeeded and wrote peer's inbox
    if is_error(4):
        failures.append(f"teammate_send to peer reported error: {call_text(4)}")
    peer_inbox = inboxes_dir(root) / f"{PEER}_unread.json"
    if not peer_inbox.exists() or "hi peer" not in peer_inbox.read_text(encoding="utf-8"):
        failures.append("teammate_send did not write peer inbox")

    # error paths -> isError true
    for i, label in [(5, "self-send"), (6, "unknown tool"), (7, "missing 'to'"), (8, "bad agent name")]:
        if not is_error(i):
            failures.append(f"{label} did not return isError: {by_id.get(i)}")

    # unknown method -> -32601
    if by_id.get(9, {}).get("error", {}).get("code") != -32601:
        failures.append(f"unknown method not -32601: {by_id.get(9)}")

    # channel notification fired for the external message
    if not notifications:
        failures.append("no notifications/claude/channel emitted for new message")
    elif notifications[0]["params"]["meta"].get("agent") != AGENT:
        failures.append(f"channel notification meta wrong: {notifications[0]['params']['meta']}")

    # inbox shows the message, ack clears it, ping ok
    if "hello via channel" not in call_text(10):
        failures.append(f"teammate_inbox missing the message: {call_text(10)}")
    if is_error(11) or "Acknowledged" not in call_text(11):
        failures.append(f"teammate_ack failed: {call_text(11)}")
    if by_id.get(12, {}).get("result") != {}:
        failures.append(f"ping result not empty: {by_id.get(12)}")

    # registry: written, and type survives the heartbeat merge
    if not record.exists():
        failures.append(f"registry record not written at {record}")
    else:
        rec = json.loads(record.read_text(encoding="utf-8"))
        if rec.get("type") != "full" or "lastHeartbeat" not in rec:
            failures.append(f"registry record incomplete: {rec}")
    if type_after_heartbeat != "full":
        failures.append(f"type:'full' did not survive heartbeat (got {type_after_heartbeat!r})")

    # version sync across manifests
    pkg_version = re.search(r'__version__\s*=\s*"([^"]+)"',
                            (SRC / "teammate_comms" / "__init__.py").read_text(encoding="utf-8")).group(1)
    plugin_version = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    pyproject_version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE).group(1)
    if not (pkg_version == plugin_version == pyproject_version):
        failures.append(f"version drift: pkg={pkg_version} plugin={plugin_version} pyproject={pyproject_version}")

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
