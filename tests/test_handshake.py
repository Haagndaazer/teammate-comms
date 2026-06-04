"""Isolation test for the teammate-comms MCP server.

Drives `python -m teammate_comms.server` over a pipe with a temp comms root and
NO TEAMMATE_AGENT (so identity comes from an explicit teammate_register call, the
primary path). Asserts both halves of the unified server:

  Registration + tool gating:
    - tools/list returns 10 tools (register + 9), each with a valid object inputSchema
    - before registration, messaging tools return isError ("register first")
    - teammate_register (with a profile) establishes identity; teammate_whoami flips
      to registered and echoes the profile

  Group chat:
    - teammate_group create; teammate_send to="#grp" fans out to members' inboxes
      (group-tagged 👥) but NOT the sender; the shared transcript records it;
      teammate_group history returns it (and filters by sender); unknown action +
      duplicate create are isError
    - ack("all") only clears messages SEEN as of the last teammate_inbox read; an
      arrival that lands after the read is preserved (v0.4.1)

  Profile fields:
    - teammate_register echoes the profile back (personality reminder at start)
    - `project` is auto-filled from CLAUDE_PROJECT_DIR's basename and shows in
      whoami / teammate_list / teammate_profile
    - teammate_update changes status; teammate_list always shows project/status/
      authority and includes personality; teammate_profile returns the full profile;
      a profile field SURVIVES a heartbeat cycle
    - the channel wake names the message source (sender/group); the personality reminder
      is every ~10 msgs (registration echoes it), NOT every wake

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
GROUP = "brainstorm"
GROUP_SIGIL = "#brainstorm"
HUMAN = "Operator"  # the human operator registered by teammate_dashboard

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


def find_response(rid, timeout=4.0):
    """Poll stdout_lines mid-run for a response with the given id (so a later request can
    reference a dynamic message id)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for line in list(stdout_lines):
            try:
                m = json.loads(line)
            except (ValueError, TypeError):
                continue
            if m.get("id") == rid:
                return ((m.get("result") or {}).get("content") or [{}])[0].get("text", "")
        time.sleep(0.1)
    return ""


def inboxes_dir(root):
    return Path(root) / "TeammateComms" / TEAM / "inboxes"


def groups_dir(root):
    return Path(root) / "TeammateComms" / TEAM / "groups"


def append_external_message(root, to, frm, message, group=None, mentions=None):
    """Simulate a peer's send by appending to <to>'s unread inbox directly.

    Pass ``group`` to simulate a peer posting to a group (a group-tagged record);
    ``mentions`` to simulate an @mention list on the shared record.
    """
    d = inboxes_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{to}_unread.json"
    msgs = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    rec = {"id": f"ext-{time.time()}", "from": frm, "priority": "normal", "message": message}
    if group:
        rec["group"] = group
    if mentions:
        rec["mentions"] = mentions
    msgs.append(rec)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(msgs), encoding="utf-8")
    os.replace(tmp, f)


def append_external_reaction(root, target, target_from, frm, emoji, op="add"):
    """Simulate a reaction by appending an event to reactions.jsonl directly."""
    d = Path(root) / "TeammateComms" / TEAM
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": f"rxext-{time.time()}", "target": target, "target_from": target_from,
           "from": frm, "emoji": emoji, "op": op}
    with open(d / "reactions.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


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
    time.sleep(1.0)  # let the watcher seed known_ids (empty inbox at register)
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

    # Group chat: create, duplicate-create (isError), send (fan-out), history, bad action.
    send(proc, {"jsonrpc": "2.0", "id": 20, "method": "tools/call",
                "params": {"name": "teammate_group",
                           "arguments": {"action": "create", "group": GROUP_SIGIL, "members": [PEER]}}})
    send(proc, {"jsonrpc": "2.0", "id": 21, "method": "tools/call",
                "params": {"name": "teammate_group",
                           "arguments": {"action": "create", "group": GROUP_SIGIL}}})   # dup -> isError
    send(proc, {"jsonrpc": "2.0", "id": 22, "method": "tools/call",
                "params": {"name": "teammate_send",
                           "arguments": {"to": GROUP_SIGIL, "message": "group hello"}}})
    send(proc, {"jsonrpc": "2.0", "id": 23, "method": "tools/call",
                "params": {"name": "teammate_group", "arguments": {"action": "history", "group": GROUP_SIGIL}}})
    send(proc, {"jsonrpc": "2.0", "id": 24, "method": "tools/call",
                "params": {"name": "teammate_group", "arguments": {"action": "bogus", "group": GROUP_SIGIL}}})  # isError
    time.sleep(0.6)

    # A peer posts to the group -> group-tagged record in AGENT's inbox; inbox shows the tag.
    append_external_message(root, AGENT, PEER, "from the group", group=GROUP_SIGIL)
    time.sleep(0.6)
    send(proc, {"jsonrpc": "2.0", "id": 25, "method": "tools/call",
                "params": {"name": "teammate_inbox", "arguments": {}}})   # last_seen = {this msg}
    time.sleep(0.4)

    # A1: ack("all") must PRESERVE a message that arrives AFTER the last read.
    append_external_message(root, AGENT, PEER, "arrived-after-read")
    time.sleep(0.3)
    send(proc, {"jsonrpc": "2.0", "id": 26, "method": "tools/call",
                "params": {"name": "teammate_ack", "arguments": {"id": "all"}}})   # acks SEEN only
    send(proc, {"jsonrpc": "2.0", "id": 27, "method": "tools/call",
                "params": {"name": "teammate_inbox", "arguments": {}}})            # still shows the new one
    # A2: history sender filter (only AGENT posted "group hello" to the transcript).
    send(proc, {"jsonrpc": "2.0", "id": 28, "method": "tools/call",
                "params": {"name": "teammate_group",
                           "arguments": {"action": "history", "group": GROUP_SIGIL, "sender": AGENT}}})
    send(proc, {"jsonrpc": "2.0", "id": 29, "method": "tools/call",
                "params": {"name": "teammate_group",
                           "arguments": {"action": "history", "group": GROUP_SIGIL, "sender": PEER}}})  # none
    time.sleep(0.5)

    # v0.4.4 mixed-batch: a DM-triggered wake must still name a PENDING unseen group
    # thread. Drain to a clean slate, then inject an (unread) group msg + a later DM in
    # SEPARATE polls. The DM wake fires at count=2 (group+DM both unseen) and must name
    # the group target — under v0.4.3 it would not (DM alone in `fresh`).
    send(proc, {"jsonrpc": "2.0", "id": 30, "method": "tools/call",
                "params": {"name": "teammate_ack", "arguments": {"id": "all"}}})   # clear leftover (seen)
    time.sleep(0.6)
    # carries an @mention of AGENT → the wake(s) for it must include the 🔔 line
    append_external_message(root, AGENT, PEER, "mixed-group @test-chan", group=GROUP_SIGIL,
                            mentions=[AGENT])  # unread group msg + mention
    time.sleep(1.3)   # nudged in its own poll -> known_ids, still unseen
    append_external_message(root, AGENT, PEER, "mixed-dm")                         # 1:1 DM
    time.sleep(1.3)   # DM wake: fresh={dm}, unseen={group,dm} -> count 2, names group

    # teammate_dashboard: launch the stdlib console (no browser), which registers the
    # human as a first-class teammate; then confirm the human shows in list/profile.
    # (Tool calls only — these touch the human's record/inbox, NOT AGENT's, so they add
    # no channel nudges and don't perturb the mixed-batch count assertion above.)
    send(proc, {"jsonrpc": "2.0", "id": 31, "method": "tools/call",
                "params": {"name": "teammate_dashboard",
                           "arguments": {"human_name": HUMAN, "open_browser": False}}})
    time.sleep(0.8)
    send(proc, {"jsonrpc": "2.0", "id": 32, "method": "tools/call",
                "params": {"name": "teammate_list", "arguments": {}}})
    send(proc, {"jsonrpc": "2.0", "id": 33, "method": "tools/call",
                "params": {"name": "teammate_profile", "arguments": {"agent": HUMAN}}})
    time.sleep(0.5)

    # Typed posts + history filters (v0.6.0): a typed group post, then history filtered by
    # post_type (decision trail) and by a future `since` cursor (should be empty).
    send(proc, {"jsonrpc": "2.0", "id": 34, "method": "tools/call",
                "params": {"name": "teammate_send",
                           "arguments": {"to": GROUP_SIGIL, "message": "ship v0.6.0",
                                         "post_type": "decision"}}})
    time.sleep(0.5)
    # Reactions (v0.6.0): capture the decision post's id, react to it (ambient — no wake),
    # then re-read history to confirm the reaction renders. add fire + thumbsup, then
    # remove thumbsup → only fire remains; a bogus emoji is rejected.
    decision_id = ""
    m = re.search(r"\(id: (.+?)\)", find_response(34))
    if m:
        decision_id = m.group(1)
    for rid, emoji, rm in [(47, "fire", False), (48, "thumbsup", False), (49, "thumbsup", True)]:
        send(proc, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                    "params": {"name": "teammate_react",
                               "arguments": {"to_message": decision_id, "emoji": emoji, "remove": rm}}})
    send(proc, {"jsonrpc": "2.0", "id": 50, "method": "tools/call",
                "params": {"name": "teammate_react",
                           "arguments": {"to_message": decision_id, "emoji": "bogus"}}})
    time.sleep(0.4)
    send(proc, {"jsonrpc": "2.0", "id": 35, "method": "tools/call",
                "params": {"name": "teammate_group",
                           "arguments": {"action": "history", "group": GROUP_SIGIL, "post_type": "decision"}}})
    send(proc, {"jsonrpc": "2.0", "id": 36, "method": "tools/call",
                "params": {"name": "teammate_group",
                           "arguments": {"action": "history", "group": GROUP_SIGIL, "since": "9999"}}})
    send(proc, {"jsonrpc": "2.0", "id": 37, "method": "tools/call",
                "params": {"name": "teammate_send",
                           "arguments": {"to": GROUP_SIGIL, "message": "bad type", "post_type": "bogus"}}})
    time.sleep(0.4)

    # @mentions (record level): add the human to the group, then a post mentioning a real
    # member (PEER), the human (Operator), and a non-member (stranger). Only real members
    # are recorded — stranger is excluded (no phantom mentions).
    send(proc, {"jsonrpc": "2.0", "id": 38, "method": "tools/call",
                "params": {"name": "teammate_group",
                           "arguments": {"action": "add", "group": GROUP_SIGIL, "members": [HUMAN]}}})
    send(proc, {"jsonrpc": "2.0", "id": 39, "method": "tools/call",
                "params": {"name": "teammate_send",
                           "arguments": {"to": GROUP_SIGIL,
                                         "message": f"review @{PEER} @stranger @{HUMAN}",
                                         "reply_to": "2026-01-01T00:00:00.000000"}}})
    # history filtered to that thread (reply_to) returns this message
    send(proc, {"jsonrpc": "2.0", "id": 45, "method": "tools/call",
                "params": {"name": "teammate_group",
                           "arguments": {"action": "history", "group": GROUP_SIGIL,
                                         "reply_to": "2026-01-01T00:00:00.000000"}}})
    time.sleep(0.5)

    # ── Mute (v0.6.0) ── drain to a clean slate, mute #brainstorm, then inject a muted
    # group message + a 1:1 DM, then unmute + inject a new group message. The watcher's
    # muted-cache refreshes on the 5s heartbeat, so each mute/unmute is followed by a
    # heartbeat-length wait. Invariants proven by the LAST TWO nudges (both count "1"):
    #   - DM woke WHILE the group was muted (never-mute-a-DM) — count 1, names no group;
    #   - the new group woke AFTER unmute (restore) — count 1, names #brainstorm;
    #   - count 1 (not 2) on each → the muted message neither padded the count nor
    #     retro-nudged on unmute. (The global non_one==[one "2"] assertion guards this too.)
    send(proc, {"jsonrpc": "2.0", "id": 40, "method": "tools/call",
                "params": {"name": "teammate_inbox", "arguments": {}}})       # drain-read
    send(proc, {"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                "params": {"name": "teammate_ack", "arguments": {"id": "all"}}})  # clear inbox
    send(proc, {"jsonrpc": "2.0", "id": 42, "method": "tools/call",
                "params": {"name": "teammate_group", "arguments": {"action": "mute", "group": GROUP_SIGIL}}})
    time.sleep(0.5)

    # Reaction WAKES (v0.6.1): a reaction wakes ONLY the author of the reacted-to message.
    # R1 (target_from=AGENT, from=PEER, add) → should wake AGENT; R2 (target_from=PEER) and
    # R3 (op=remove) → must NOT wake. Processed on the next heartbeat tick (the 5.5s below).
    append_external_reaction(root, decision_id or "x", AGENT, PEER, "fire", "add")    # → wake
    append_external_reaction(root, "someones-msg", PEER, "Nancy", "smile", "add")     # not my msg
    append_external_reaction(root, decision_id or "x", AGENT, PEER, "thumbsup", "remove")  # remove

    # Heartbeat cycle (5s): refreshes the watcher's muted cache, processes reaction wakes,
    # AND confirms type:"full" + a profile field survive the registry merge.
    time.sleep(5.5)
    type_after_heartbeat = None
    status_after_heartbeat = None
    if record.exists():
        rec_hb = json.loads(record.read_text(encoding="utf-8"))
        type_after_heartbeat = rec_hb.get("type")
        status_after_heartbeat = rec_hb.get("status")

    append_external_message(root, AGENT, PEER, "muted-grp-msg", group=GROUP_SIGIL)  # muted → NO wake
    time.sleep(0.6)
    append_external_message(root, AGENT, PEER, "dm-while-muted")                    # DM → wakes (count 1)
    time.sleep(1.3)
    send(proc, {"jsonrpc": "2.0", "id": 43, "method": "tools/call",
                "params": {"name": "teammate_inbox", "arguments": {}}})   # both present (mute keeps them)
    send(proc, {"jsonrpc": "2.0", "id": 44, "method": "tools/call",
                "params": {"name": "teammate_group", "arguments": {"action": "unmute", "group": GROUP_SIGIL}}})
    time.sleep(5.5)   # heartbeat clears the muted cache
    append_external_message(root, AGENT, PEER, "after-unmute-grp", group=GROUP_SIGIL)  # wakes (count 1, names group)
    time.sleep(1.3)

    # Read receipts (read-only inference): AGENT has acked group messages (ack-all above),
    # so its position is a real id; PEER never acked → (none acked).
    send(proc, {"jsonrpc": "2.0", "id": 46, "method": "tools/call",
                "params": {"name": "teammate_group", "arguments": {"action": "reads", "group": GROUP_SIGIL}}})
    # reincarnate is GATED off by default (env has no TEAMMATE_REINCARNATE_ENABLED) → isError,
    # and must NOT spawn anything.
    send(proc, {"jsonrpc": "2.0", "id": 52, "method": "tools/call",
                "params": {"name": "teammate_reincarnate",
                           "arguments": {"agent": "Echo", "project_dir": str(REPO)}}})
    time.sleep(0.5)

    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    # ── assertions ──
    # stdout-purity: EVERY stdout line must parse as JSON-RPC. A stray print (e.g. from
    # the dashboard HTTP server) would corrupt the JSON-RPC stream — catch it as a clean
    # failure rather than crashing the parse.
    msgs, bad_stdout = [], []
    for l in stdout_lines:
        try:
            msgs.append(json.loads(l))
        except (json.JSONDecodeError, ValueError):
            bad_stdout.append(l)
    by_id = {m.get("id"): m for m in msgs if "id" in m}
    notifications = [m for m in msgs if m.get("method") == "notifications/claude/channel"]
    # MESSAGE wakes only — v0.6.1 reaction wakes carry meta.kind=="reaction" and must NOT
    # perturb any message-count/position assertion below (they use mwakes, not notifications).
    mwakes = [n for n in notifications if n["params"]["meta"].get("kind") != "reaction"]
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

    # tools/list: 12 tools, each with an object inputSchema
    tl = result(2).get("tools")
    expected_names = {"teammate_register", "teammate_send", "teammate_inbox",
                      "teammate_ack", "teammate_list", "teammate_whoami",
                      "teammate_update", "teammate_profile", "teammate_group",
                      "teammate_react", "teammate_reincarnate", "teammate_dashboard"}
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
    if not mwakes:
        failures.append("no notifications/claude/channel emitted for new message")
    elif mwakes[0]["params"]["meta"].get("agent") != AGENT:
        failures.append(f"channel notification meta wrong: {mwakes[0]['params']['meta']}")
    # v0.6.0: the wake names WHERE the message came from (the DM sender), and does NOT
    # lead with the personality reminder (that's now every ~10 msgs + at registration, not
    # every wake — registration echo is asserted via text(5) above).
    elif "tester" not in mwakes[0]["params"].get("content", ""):
        failures.append(f"channel wake did not name the source/sender: {mwakes[0]['params'].get('content')}")
    elif PERSONALITY in mwakes[0]["params"].get("content", ""):
        failures.append(f"early wake wrongly included the personality reminder (should be every ~10): {mwakes[0]['params'].get('content')}")
    # v0.4.2: nudge count is the UNSEEN count and is never padded by read-but-unacked
    # messages. Every message in this run is read/acked before the next arrives, so the
    # unseen count at each nudge is exactly 1 — a notification with count>1 would mean a
    # read-but-unacked message padded it (the old baseline=len(unread) behavior).
    # Counts are unseen-only (never padded). Every nudge in this flow is count "1"
    # EXCEPT the v0.4.4 mixed-batch DM wake — a legit count "2" (group + DM both unseen)
    # that must name the group reply target (proves the mixed-batch fix).
    non_one = [n for n in mwakes if n["params"]["meta"].get("count") != "1"]
    if len(non_one) != 1 or non_one[0]["params"]["meta"].get("count") != "2":
        failures.append(f"unexpected nudge counts (want all '1' except one '2'): {[n['params']['meta'].get('count') for n in mwakes]}")
    elif f"to:'{GROUP_SIGIL}'" not in non_one[0]["params"].get("content", ""):
        failures.append(f"mixed-batch DM nudge (count 2) did not name group target: {non_one[0]['params']['content']}")
    # v0.4.3: a group-message wake names the group reply target; a 1:1 wake does not.
    group_nudges = [n for n in mwakes if GROUP_SIGIL in n["params"].get("content", "")]
    if not group_nudges:
        failures.append("no channel nudge named the group reply target")
    elif f"to:'{GROUP_SIGIL}'" not in group_nudges[0]["params"]["content"]:
        failures.append(f"group nudge missing reply target to:'{GROUP_SIGIL}': {group_nudges[0]['params']['content']}")
    # mwakes[0] is the 1:1 "hello via channel" wake — must NOT name a group target
    if mwakes and "to:'#" in mwakes[0]["params"].get("content", ""):
        failures.append(f"1:1 nudge wrongly named a group target: {mwakes[0]['params']['content']}")
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

    # group chat: create / duplicate / send fan-out / transcript / history / bad action
    if is_error(20) or "Created group" not in text(20):
        failures.append(f"teammate_group create failed: {text(20)}")
    if not is_error(21):
        failures.append(f"duplicate group create not isError: {by_id.get(21)}")
    if is_error(22) or "Posted to" not in text(22):
        failures.append(f"teammate_send to group failed: {text(22)}")
    # fan-out reached PEER's inbox (group-tagged) but NOT the sender's own inbox
    peer_unread = inboxes_dir(root) / f"{PEER}_unread.json"
    peer_txt = peer_unread.read_text(encoding="utf-8") if peer_unread.exists() else ""
    if "group hello" not in peer_txt or GROUP_SIGIL not in peer_txt:
        failures.append("group send did not fan out to peer inbox (tagged)")
    agent_unread = inboxes_dir(root) / f"{AGENT}_unread.json"
    agent_txt = agent_unread.read_text(encoding="utf-8") if agent_unread.exists() else ""
    if "group hello" in agent_txt:
        failures.append("group send echoed to the sender's own inbox (should skip sender)")
    # transcript recorded the message
    transcript = groups_dir(root) / GROUP / "messages.json"
    if not transcript.exists() or "group hello" not in transcript.read_text(encoding="utf-8"):
        failures.append(f"group transcript missing message at {transcript}")
    # history returns it
    if is_error(23) or "group hello" not in text(23):
        failures.append(f"teammate_group history missing message: {text(23)}")
    # unknown action -> isError
    if not is_error(24):
        failures.append(f"unknown group action not isError: {by_id.get(24)}")
    # inbox shows the [👥 group: #grp] tag for a group-tagged message
    if "group:" not in text(25) or GROUP_SIGIL not in text(25) or "from the group" not in text(25):
        failures.append(f"teammate_inbox missing group tag: {text(25)}")
    if "👥" not in text(25):
        failures.append(f"teammate_inbox group tag missing glyph: {text(25)}")

    # A1: ack("all") preserved the post-read arrival; the seen group msg was acked
    if is_error(26) or "Acknowledged" not in text(26) or "Kept 1" not in text(26):
        failures.append(f"ack-all did not preserve post-read arrival: {text(26)}")
    if "arrived-after-read" not in text(27):
        failures.append(f"post-read arrival was wrongly acked (not in inbox): {text(27)}")
    if "from the group" in text(27):
        failures.append(f"seen message was not acked by ack-all: {text(27)}")
    # A2: history sender filter
    if is_error(28) or "group hello" not in text(28):
        failures.append(f"history sender={AGENT} missing AGENT's message: {text(28)}")
    if "group hello" in text(29):
        failures.append(f"history sender={PEER} wrongly included AGENT's message: {text(29)}")

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

    # ── dashboard + human-as-teammate + durable observability transcript ──
    # stdout stayed pure JSON-RPC even after the HTTP server launched (no stray prints)
    if bad_stdout:
        failures.append(f"non-JSON-RPC line(s) on stdout (stdout-purity): {bad_stdout[:3]}")
    # teammate_dashboard launched and returned a localhost URL
    if is_error(31) or "http://127.0.0.1:" not in text(31) or "Dashboard" not in text(31):
        failures.append(f"teammate_dashboard did not return a localhost URL: {text(31)}")
    # the human is a first-class teammate: type=human, an inbox, NO pid/channel, online
    human_record = Path(root) / "TeammateComms" / TEAM / "agents" / f"{HUMAN}.json"
    if not human_record.exists():
        failures.append(f"human record not written at {human_record}")
    else:
        hr = json.loads(human_record.read_text(encoding="utf-8"))
        if hr.get("type") != "human":
            failures.append(f"human record type not 'human': {hr}")
        if "pid" in hr or hr.get("channel"):
            failures.append(f"human record should have no pid/channel: {hr}")
        # presence is read AFTER the process exits, so the clean shutdown has marked it
        # "away" — assert it's a valid presence value; "online while running" is checked
        # via the live teammate_list below.
        if hr.get("presence") not in ("online", "away"):
            failures.append(f"human presence not a valid state: {hr}")
    if not (inboxes_dir(root) / f"{HUMAN}_unread.json").exists():
        failures.append("human has no inbox")
    # teammate_list (live, while the dashboard runs) marks the human as an online operator
    if HUMAN not in text(32) or "operator" not in text(32) or "presence=online" not in text(32):
        failures.append(f"teammate_list did not mark the human as online operator: {text(32)}")
    # teammate_profile of the human renders type: human + presence
    if "human" not in text(33) or "presence:" not in text(33):
        failures.append(f"teammate_profile(human) missing type/presence: {text(33)}")
    # durable transcript: the DM (id 7) and the group post (id 22) were both teed
    transcript_log = Path(root) / "TeammateComms" / TEAM / "transcript.jsonl"
    if not transcript_log.exists():
        failures.append(f"transcript.jsonl not written at {transcript_log}")
    else:
        tlines = [json.loads(l) for l in transcript_log.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not any(r.get("kind") == "dm" and r.get("to") == PEER and "hi peer" in r.get("message", "") for r in tlines):
            failures.append("transcript missing the DM tee (kind=dm, to=peer)")
        if not any(r.get("kind") == "group" and r.get("group") == GROUP_SIGIL and "group hello" in r.get("message", "") for r in tlines):
            failures.append("transcript missing the group tee (kind=group)")

    # ── typed posts + history filters (v0.6.0) ──
    # the typed group post was teed with post_type into the durable transcript
    transcript_log = Path(root) / "TeammateComms" / TEAM / "transcript.jsonl"
    if transcript_log.exists():
        tl2 = [json.loads(l) for l in transcript_log.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not any(r.get("post_type") == "decision" and "ship v0.6.0" in r.get("message", "") for r in tl2):
            failures.append("typed post not teed with post_type=decision to transcript")
    # ── reactions (v0.6.0) ──
    if is_error(47) or "Reacted" not in text(47):
        failures.append(f"teammate_react add failed: {text(47)}")
    if not is_error(50):
        failures.append(f"bogus emoji not isError: {by_id.get(50)}")
    # reactions.jsonl recorded add/remove events; aggregate → only fire remains on the decision
    reactions_log = Path(root) / "TeammateComms" / TEAM / "reactions.jsonl"
    if not reactions_log.exists():
        failures.append("reactions.jsonl not written")
    elif decision_id:
        revs = [json.loads(l) for l in reactions_log.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not any(r.get("target") == decision_id and r.get("emoji") == "fire" and r.get("op") == "add" for r in revs):
            failures.append("reactions.jsonl missing the fire add event")
        if not any(r.get("emoji") == "thumbsup" and r.get("op") == "remove" for r in revs):
            failures.append("reactions.jsonl missing the thumbsup remove event")
    # history renders the surviving reaction (🔥) on the decision post, and NOT 👍 (removed)
    if "🔥" not in text(35):
        failures.append(f"history did not render the fire reaction: {text(35)}")
    if "👍" in text(35):
        failures.append(f"history rendered a removed reaction (👍 should be gone): {text(35)}")

    # history post_type filter returns the decision, renders the [DECISION] tag, and EXCLUDES untyped
    if is_error(35) or "ship v0.6.0" not in text(35) or "DECISION" not in text(35):
        failures.append(f"history post_type=decision missing the typed post: {text(35)}")
    if "group hello" in text(35):
        failures.append(f"history post_type=decision wrongly included an untyped post: {text(35)}")
    # history since=<future cursor> excludes everything
    if "has no messages" not in text(36):
        failures.append(f"history since=future not empty: {text(36)}")
    # a bogus post_type is rejected
    if not is_error(37):
        failures.append(f"bogus post_type not isError: {by_id.get(37)}")

    # ── @mentions (v0.6.0) ──
    # the wake for the mention-carrying mixed-batch message included the 🔔 line
    if not any("🔔" in n["params"].get("content", "") for n in mwakes):
        failures.append("no channel wake carried the @mention 🔔 line")
    # record-level: only real members are recorded (stranger excluded; human included)
    if transcript_log.exists():
        tl3 = [json.loads(l) for l in transcript_log.read_text(encoding="utf-8").splitlines() if l.strip()]
        mrec = next((r for r in tl3 if "review @" in r.get("message", "")), None)
        if not mrec:
            failures.append("mention post not found in transcript")
        elif sorted(mrec.get("mentions", [])) != sorted([PEER, HUMAN]):
            failures.append(f"mentions wrong (want {PEER}+{HUMAN}, no stranger): {mrec.get('mentions')}")
        # threading: the reply_to hint was stored on the same record
        elif mrec.get("reply_to") != "2026-01-01T00:00:00.000000":
            failures.append(f"reply_to hint not stored: {mrec.get('reply_to')}")
    # history reply_to filter returns the threaded message + renders the ↳ note
    if is_error(45) or "review @" not in text(45) or "↳ re 2026-01-01" not in text(45):
        failures.append(f"history reply_to filter/render wrong: {text(45)}")

    # ── read receipts (v0.6.0, read-only inference) ──
    if is_error(46) or "read positions" not in text(46):
        failures.append(f"reads action failed: {text(46)}")
    # AGENT acked group messages → a real position (not "(none acked)"); PEER never acked
    if f"{AGENT}: (none acked)" in text(46) or f"{AGENT}: " not in text(46):
        failures.append(f"reads: AGENT should have an acked group position: {text(46)}")
    if f"{PEER}: (none acked)" not in text(46):
        failures.append(f"reads: PEER never acked, should be (none acked): {text(46)}")

    # ── mute (v0.6.0) ──
    if is_error(42) or "Muted" not in text(42):
        failures.append(f"mute action failed: {text(42)}")
    if is_error(44) or "Unmuted" not in text(44):
        failures.append(f"unmute action failed: {text(44)}")
    # mute keeps messages in the inbox (it removes the WAKE, not the message)
    if "muted-grp-msg" not in text(43) or "dm-while-muted" not in text(43):
        failures.append(f"mute dropped a message from the inbox (should keep both): {text(43)}")
    # the last two MESSAGE wakes prove the invariants (reaction wakes excluded via mwakes):
    if len(mwakes) < 2:
        failures.append("expected DM + post-unmute wakes for the mute test")
    else:
        dm_wake, grp_wake = mwakes[-2], mwakes[-1]
        # [-2] = the DM that woke WHILE the group was muted: count 1, names NO group
        if dm_wake["params"]["meta"].get("count") != "1" or "to:'#" in dm_wake["params"].get("content", ""):
            failures.append(f"never-mute-a-DM: DM wake wrong (want count 1, no group): {dm_wake['params']}")
        # [-1] = the new group post AFTER unmute: count 1 (no retro-nudge of the muted msg), names group
        if grp_wake["params"]["meta"].get("count") != "1" or f"to:'{GROUP_SIGIL}'" not in grp_wake["params"].get("content", ""):
            failures.append(f"unmute restore wrong (want count 1 naming {GROUP_SIGIL}): {grp_wake['params']}")

    # ── reaction wakes (v0.6.1) — only the AUTHOR is woken, add-only ──
    reaction_wakes = [n for n in notifications if n["params"]["meta"].get("kind") == "reaction"]
    if len(reaction_wakes) != 1:
        # exactly R1 should have woken (R2 target_from!=AGENT, R3 op=remove, self-reacts from==agent)
        failures.append(f"want exactly 1 reaction wake (author-only/add-only): {[r['params']['meta'] for r in reaction_wakes]}")
    else:
        rw = reaction_wakes[0]["params"]
        if rw["meta"].get("agent") != AGENT:
            failures.append(f"reaction wake not addressed to the author: {rw['meta']}")
        if PEER not in rw.get("content", ""):
            failures.append(f"reaction wake doesn't name the reactor: {rw.get('content')}")
    # the count assertion (over mwakes) must be unperturbed by the reaction wake
    # (reaction wakes carry kind:reaction → excluded) — already checked via non_one above.
    # react() stamped target_from on the self-reactions (id 47-49 → decision post by AGENT)
    if reactions_log.exists():
        revs2 = [json.loads(l) for l in reactions_log.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not any(r.get("from") == AGENT and r.get("target_from") == AGENT and r.get("op") == "add" for r in revs2):
            failures.append("react() did not stamp target_from on a self-reaction")

    # ── teammate_reincarnate (v0.6.0) ──
    # gate-off path (no TEAMMATE_REINCARNATE_ENABLED) is isError + spawns nothing
    if not is_error(52) or "disabled" not in text(52):
        failures.append(f"reincarnate gate-off not isError/disabled: {by_id.get(52)}")
    # spawn.py pure builders (NO real spawn): list-form safety + handoff env + dir validation
    try:
        sys.path.insert(0, str(SRC))
        from teammate_comms import spawn as _spawn
        from teammate_comms.comms import validate_project_dir as _vpd, CommsError as _CE
        hostile = "evil; rm -rf / & $(whoami) `id` | nc"
        # Pass an absent settings path so the channel-flag branch is DETERMINISTIC regardless
        # of the test machine (a real managed-settings file would otherwise flip it to --channels).
        no_settings = ["/no/such/managed-settings.json"]
        argv = _spawn.build_claude_command(hostile, settings_paths=no_settings)
        if "--permission-mode" not in argv or "bypassPermissions" not in argv:
            failures.append(f"build_claude_command missing bypassPermissions: {argv}")
        if argv[-1] != hostile:   # hostile prompt stays ONE argv element → no shell injection
            failures.append(f"prompt not a single trailing argv element (injection risk): {argv}")
        if "--name" in argv:
            failures.append("build_claude_command should not pass --name (print-only)")
        # ── channel-flag auto-detect (v0.6.5) — ALWAYS pass explicit settings_paths (hermetic) ──
        # (a) no allowlist file → dangerous flag, never plain --channels
        if "--dangerously-load-development-channels" not in argv or "--channels" in argv:
            failures.append(f"no-allowlist should use dangerous flag, not --channels: {argv}")
        # (b) a managed-settings file trusting this plugin → plain --channels, never the dangerous flag
        _d = tempfile.mkdtemp()   # mkdtemp (NOT NamedTemporaryFile): Windows can't reopen an open handle
        _ms = os.path.join(_d, "managed-settings.json")
        with open(_ms, "w", encoding="utf-8") as _f:
            json.dump({"channelsEnabled": True,
                       "allowedChannelPlugins": [{"marketplace": "coltondyck", "plugin": "teammate-comms"}]}, _f)
        argv_allow = _spawn.build_claude_command("p", settings_paths=[_ms])
        if "--channels" not in argv_allow or "--dangerously-load-development-channels" in argv_allow:
            failures.append(f"allowlisted should use --channels, not the dangerous flag: {argv_allow}")
        # (c) channelsEnabled false → NOT trusted → fall back to the dangerous flag
        with open(_ms, "w", encoding="utf-8") as _f:
            json.dump({"channelsEnabled": False,
                       "allowedChannelPlugins": [{"marketplace": "coltondyck", "plugin": "teammate-comms"}]}, _f)
        if _spawn.channel_allowlisted(settings_paths=[_ms]):
            failures.append("channel_allowlisted true despite channelsEnabled=false")
        # (d) the real default-path resolver must not throw and returns a bool (covers managed_settings_paths)
        if not isinstance(_spawn.channel_allowlisted(), bool):
            failures.append("channel_allowlisted() default-path call did not return a bool")
        # (e) $TEAMMATE_LAUNCH_ARGS overrides BOTH branches — try/finally so a failure can't leak the env
        _prev = os.environ.get("TEAMMATE_LAUNCH_ARGS")
        try:
            os.environ["TEAMMATE_LAUNCH_ARGS"] = "claude --foo"
            argv_ovr = _spawn.build_claude_command("p", settings_paths=[_ms])
            if "--foo" not in argv_ovr or "--channels" in argv_ovr or "--dangerously-load-development-channels" in argv_ovr:
                failures.append(f"TEAMMATE_LAUNCH_ARGS did not override auto-detect: {argv_ovr}")
        finally:
            if _prev is None:
                os.environ.pop("TEAMMATE_LAUNCH_ARGS", None)
            else:
                os.environ["TEAMMATE_LAUNCH_ARGS"] = _prev
        cenv = _spawn.build_child_env({}, "Echo", "/proj", team="t", comms_dir="/c")
        if cenv.get("TEAMMATE_AGENT") != "Echo" or cenv.get("CLAUDE_PROJECT_DIR") != "/proj":
            failures.append(f"build_child_env missing handoff vars: {cenv}")
        if cenv.get("TEAMMATE_TEAM") != "t" or cenv.get("TEAMMATE_COMMS_DIR") != "/c":
            failures.append(f"build_child_env missing team/comms_dir: {cenv}")
        try:
            _vpd("/no/such/dir/xyz123")
            failures.append("validate_project_dir accepted a missing directory")
        except _CE:
            pass
    except Exception as e:
        failures.append(f"spawn unit checks errored: {e}")

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
