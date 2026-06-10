"""Isolation test for the teammate-comms MCP server.

Drives `python -m teammate_comms.server` over a pipe with a temp comms root and
NO TEAMMATE_AGENT (so identity comes from an explicit teammate_register call, the
primary path). Asserts both halves of the unified server:

  Registration + tool gating:
    - tools/list returns 13 tools (register + 12), each with a valid object inputSchema
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
    # Keep the reincarnate gate OFF in the child regardless of the ambient env — otherwise a
    # box with TEAMMATE_REINCARNATE_ENABLED exported fails the gate-off assertion AND spawns a
    # real terminal window. CI has a clean env so it passes; this makes local runs match it
    # (no more manual `unset` before every run).
    env.pop("TEAMMATE_REINCARNATE_ENABLED", None)
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
        # the handshake surfaces the standing instructions, incl. the status rule (v0.7.0)
        instr = init.get("result", {}).get("instructions", "")
        if "teammate_register" not in instr or "status as you work" not in instr:
            failures.append(f"initialize instructions missing/incomplete: {instr[:80]!r}")

    # tools/list: 13 tools, each with an object inputSchema
    tl = result(2).get("tools")
    expected_names = {"teammate_register", "teammate_send", "teammate_inbox",
                      "teammate_ack", "teammate_list", "teammate_whoami",
                      "teammate_update", "teammate_profile", "teammate_group",
                      "teammate_react", "teammate_reincarnate", "teammate_dashboard",
                      "teammate_delete"}
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
        # F-1 (hostile direction): a parent that HAS the reincarnate gate set must NOT pass it
        # to the child — else a spawned child could itself re-spawn (gate is opt-in-default-off).
        _genv = _spawn.build_child_env({"TEAMMATE_REINCARNATE_ENABLED": "1"}, "Echo", "/proj")
        if "TEAMMATE_REINCARNATE_ENABLED" in _genv:
            failures.append("F-1: build_child_env carried the reincarnate gate into the child env")
        # F-1 corollary: TEAMMATE_LAUNCH_ARGS (a launch override, NOT the gate) IS inherited.
        _lenv = _spawn.build_child_env({"TEAMMATE_LAUNCH_ARGS": "claude --foo"}, "Echo", "/proj")
        if _lenv.get("TEAMMATE_LAUNCH_ARGS") != "claude --foo":
            failures.append("F-1: build_child_env wrongly stripped TEAMMATE_LAUNCH_ARGS")
        # F-6: a REAL raise (not a no-op assert) when a handoff var is empty.
        try:
            _spawn.build_child_env({}, "", "/proj")
            failures.append("F-6: build_child_env did not raise on an empty agent")
        except _CE:
            pass
        try:
            _vpd("/no/such/dir/xyz123")
            failures.append("validate_project_dir accepted a missing directory")
        except _CE:
            pass
    except Exception as e:
        failures.append(f"spawn unit checks errored: {e}")

    # ── teammate_delete (v0.7.0) — hermetic unit checks on the cores (own temp root) ──
    try:
        import socket as _socket

        from teammate_comms import comms as _c
        from teammate_comms import tools as _t
        droot = tempfile.mkdtemp(prefix="tc-del-")
        dteam = None
        A, B, OP = "alice", "bob", "Operator"

        def _inbox(member):
            return _c.read_json_safe(_c.get_inboxes_dir(droot, dteam) / f"{member}_unread.json")

        # group with both members; alice posts (canonical messages.json + fan-out + transcript)
        _c.write_group_meta(droot, dteam, "g", {"name": "g", "members": [A, B],
                                                "creator": A, "createdAt": _c.now_timestamp()})
        gmid = _t.send_group(droot, dteam, A, "#g", "hello team")["id"]
        _t.send_group(droot, dteam, B, "#g", "re: hello", reply_to=gmid)   # a reply citing it

        # (1) group-post tombstone: AUTHOR deletes → messages.json + every member inbox copy
        _t.delete_message(droot, dteam, A, gmid, is_operator=False)
        gm = _c.read_group_messages(droot, dteam, "g")
        grec = next((m for m in gm if m.get("id") == gmid), None)
        if not (grec and grec.get("deleted") and grec.get("from") == A
                and grec.get("message") == _c.DELETED_MARKER):
            failures.append(f"group tombstone not applied to messages.json: {grec}")
        bcopy = next((m for m in _inbox(B) if m.get("id") == gmid), None)
        if not (bcopy and bcopy.get("deleted")):
            failures.append(f"group tombstone not propagated to member inbox: {bcopy}")
        if not any(m.get("reply_to") == gmid for m in gm):   # reply thread still resolves
            failures.append("reply citing the deleted message no longer resolves")
        if not any(d.get("target") == gmid and d.get("kind") == "message"
                   for d in _c.read_deletions(droot, dteam)):
            failures.append("no message deletion event emitted")

        # (2) permission: unknown id raises; non-author refused; operator can delete any
        try:
            _t.delete_message(droot, dteam, B, "no-such-id", is_operator=False)
            failures.append("delete of unknown id did not raise")
        except _c.CommsError:
            pass
        mid2 = _t.send_group(droot, dteam, A, "#g", "second")["id"]
        try:
            _t.delete_message(droot, dteam, B, mid2, is_operator=False)
            failures.append("non-author delete not refused")
        except _c.CommsError:
            pass
        _t.delete_message(droot, dteam, OP, mid2, is_operator=True)   # operator override OK

        # (3) DM tombstone hits the recipient inbox
        dmid = _t.send_dm(droot, dteam, A, B, "secret")["id"]
        _t.delete_message(droot, dteam, A, dmid, is_operator=False)
        if not any(m.get("id") == dmid and m.get("deleted") for m in _inbox(B)):
            failures.append("DM tombstone not applied to recipient inbox")

        # (4) TEAMMATE_TRANSCRIPT=0 → resolve via the inbox-scan fallback, still tombstones
        _prevT = os.environ.get("TEAMMATE_TRANSCRIPT")
        try:
            os.environ["TEAMMATE_TRANSCRIPT"] = "0"
            dmid2 = _t.send_dm(droot, dteam, A, B, "no-firehose")["id"]
            _t.delete_message(droot, dteam, A, dmid2, is_operator=False)
            if not any(m.get("id") == dmid2 and m.get("deleted") for m in _inbox(B)):
                failures.append("delete with TEAMMATE_TRANSCRIPT=0 did not tombstone via fallback")
        finally:
            if _prevT is None:
                os.environ.pop("TEAMMATE_TRANSCRIPT", None)
            else:
                os.environ["TEAMMATE_TRANSCRIPT"] = _prevT

        # (5) XOR guard in the handler (neither / both → CommsError)
        class _IdA:
            def snapshot(self):
                return (A, dteam, droot, None)
        _ctxA = {"identity": _IdA()}
        for bad in ({}, {"message": "x", "teammate": "y"}):
            try:
                _t._handle_delete(bad, _ctxA)
                failures.append(f"XOR guard did not raise for {bad}")
            except _c.CommsError:
                pass

        # (6) teammate removal: offline teammate gone (record + inbox + memberships) + event
        _c.write_agent_record(droot, dteam, "carol", type="full", channel=False)
        _c.ensure_inbox(_c.get_inboxes_dir(droot, dteam), "carol")
        _c.write_group_meta(droot, dteam, "g2", {"name": "g2", "members": [A, "carol"],
                                                 "creator": A, "createdAt": _c.now_timestamp()})
        _t.remove_teammate(droot, dteam, A, "carol", is_operator=False)
        if (_c.get_agents_dir(droot, dteam) / "carol.json").exists():
            failures.append("remove_teammate left the agent record")
        if (_c.get_inboxes_dir(droot, dteam) / "carol_unread.json").exists():
            failures.append("remove_teammate left the inbox")
        g2 = _c.read_group_meta(droot, dteam, "g2")
        if "carol" in (g2.get("members") if g2 else []):
            failures.append("remove_teammate did not strip group membership")
        if not any(d.get("target") == "@carol" and d.get("kind") == "teammate"
                   for d in _c.read_deletions(droot, dteam)):
            failures.append("no teammate deletion event emitted")

        # (7) removal guards: self refused; LIVE teammate refused (fresh heartbeat)
        try:
            _t.remove_teammate(droot, dteam, A, A, is_operator=False)
            failures.append("self-removal not refused")
        except _c.CommsError:
            pass
        _c.write_agent_record(droot, dteam, "dave", type="full", channel=True,
                              host=_socket.gethostname(), pid=999999,
                              lastHeartbeat=_c.now_timestamp())
        try:
            _t.remove_teammate(droot, dteam, A, "dave", is_operator=False)
            failures.append("live-teammate removal not refused")
        except _c.CommsError:
            pass

        # (8) whole-group delete purges fan-out copies + emits a group deletion event (B4)
        _c.write_group_meta(droot, dteam, "g3", {"name": "g3", "members": [A, B],
                                                 "creator": A, "createdAt": _c.now_timestamp()})
        g3id = _t.send_group(droot, dteam, A, "#g3", "doomed")["id"]
        _t._handle_group({"action": "delete", "group": "#g3"}, _ctxA)
        if any(m.get("id") == g3id for m in _inbox(B)):
            failures.append("whole-group delete left fan-out copies in member inbox")
        if not any(d.get("target") == "#g3" and d.get("kind") == "group"
                   for d in _c.read_deletions(droot, dteam)):
            failures.append("whole-group delete did not emit a group deletion event")
    except Exception as e:
        failures.append(f"delete unit checks errored: {e}")

    # ── compact re-injection (v0.7.0): instructions.py single-sources the text + emits
    #    valid SessionStart additionalContext JSON for the matcher:"compact" hook ──
    try:
        import io as _io
        from contextlib import redirect_stdout as _redirect

        from teammate_comms import instructions as _ins
        from teammate_comms.server import INSTRUCTIONS as _srv_instr
        if _ins.INSTRUCTIONS is not _srv_instr:
            failures.append("server INSTRUCTIONS is not single-sourced from instructions.py")
        if "status as you work" not in _ins.INSTRUCTIONS:
            failures.append("INSTRUCTIONS missing the 'update your status as you work' standing rule")
        _buf = _io.StringIO()
        with _redirect(_buf):
            _ins.main()
        _emitted = json.loads(_buf.getvalue())  # must be valid JSON
        _hso = _emitted.get("hookSpecificOutput", {})
        if _hso.get("hookEventName") != "SessionStart":
            failures.append(f"reinject hookEventName wrong: {_hso.get('hookEventName')}")
        if "status as you work" not in _hso.get("additionalContext", ""):
            failures.append("reinject additionalContext missing the standing rule")
    except Exception as e:
        failures.append(f"instructions/reinject checks errored: {e}")

    # ── WP-1 (v0.7.1) — missed-event correctness: poll-cursor burst (A-1), reaction-wake
    #    high-water cursor (A-2), blocking reaction/deletion appends (A-3). Hermetic, own roots.
    try:
        from teammate_comms import channel as _ch
        from teammate_comms import comms as _c
        tm = None

        def _rx(i, target_from="alice", frm="bob", op="add"):
            return {"id": f"2026-06-10T01:00:00.{i:06d}", "target": "msg", "from": frm,
                    "emoji": "fire", "op": op, "target_from": target_from}
        ME = "alice"

        def _seed_jsonl(path, records):
            # Write records with their ids INTACT — append_reaction/append_deletion now stamp
            # their OWN id under the lock (CR-1), so tests that need controlled ids to drive
            # the read/cursor logic must write the file directly instead of via the appenders.
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                for r in records:
                    fh.write(json.dumps(r) + "\n")

        # ---- A-1: forward-pagination window never skips a burst, tail view unchanged ----
        # (i) _window unit: oldest_first swallows an id-collision group at the boundary so a
        #     cursor set to the last returned id strictly advances (no stall); tail unaffected.
        w = _c._window
        coll = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "c"}, {"id": "c"}, {"id": "d"}]
        if [r["id"] for r in w(coll, 3, True)] != ["a", "b", "c", "c", "c"]:
            failures.append("A-1 _window did not swallow the boundary collision group")
        if [r["id"] for r in w(coll, 3, False)] != ["c", "c", "d"]:
            failures.append("A-1 _window tail (oldest_first=False) regressed from newest-N")
        if w(coll, 6, True) is not coll:   # len == limit → return as-is, no copy
            failures.append("A-1 _window len==limit should return the list unchanged")

        # (ii) integration on the transcript: a >limit burst drains EVERY id with no skip
        #      and the cursor walk terminates; first load (no cursor) is byte-identical tail.
        wroot = tempfile.mkdtemp(prefix="tc-wp1a1-")
        N = 450  # spans 3 pages of the 200 transcript window
        all_ids = [f"2026-06-10T00:00:00.{i:06d}" for i in range(N)]
        for i in range(N):
            _c.append_transcript(wroot, tm, {"id": all_ids[i], "from": "a", "to": "b",
                                             "kind": "dm", "message": f"m{i}"})
        first = _c.read_transcript(wroot, tm, since=None, limit=200, oldest_first=False)
        if [r["id"] for r in first] != all_ids[-200:]:
            failures.append("A-1 first-load (no cursor) is not the newest-200 tail (legacy parity)")
        seen, cursor, hops = set(), all_ids[0], 0
        while hops < 100:
            hops += 1
            page = _c.read_transcript(wroot, tm, since=cursor, limit=200, oldest_first=True)
            ids = [r["id"] for r in page]
            seen.update(ids)
            nxt = ids[-1] if ids else cursor
            if nxt == cursor:
                break
            cursor = nxt
        if sorted(seen) != all_ids:
            failures.append(f"A-1 burst walk skipped ids (drained {len(seen)}/{N})")
        if hops >= 100:
            failures.append("A-1 burst walk failed to terminate (cursor stall)")

        # (iii) missing file → [] for all three readers (no crash; dashboard keeps its cursor)
        eroot = tempfile.mkdtemp(prefix="tc-wp1empty-")
        if (_c.read_transcript(eroot, tm, since="x", limit=200, oldest_first=True) != []
                or _c.read_reactions(eroot, tm, since="x", limit=500, oldest_first=True) != []
                or _c.read_deletions(eroot, tm, since="x", limit=1000, oldest_first=True) != []):
            failures.append("A-1 reader on a missing file did not return []")

        # ---- A-2: reaction-wake high-water cursor — pure helper + the real >500 burst hole ----
        cw = _ch.compute_reaction_wakes
        # seed read never wakes, sets the cursor to the max id seen
        f0, k0, c0 = cw([_rx(0), _rx(1)], None, ME)
        if f0 != [] or c0 != _rx(1)["id"]:
            failures.append(f"A-2 seed read woke or set a bad cursor: fresh={f0} cursor={c0}")
        # next tick: only the genuinely-new add targeting ME wakes (boundary _rx(1) re-read
        # but in the previous batch → not re-woken); cursor advances to the max id.
        f1, k1, c1 = cw([_rx(1), _rx(2)], k0, ME)
        if [r["id"] for r in f1] != [_rx(2)["id"]] or c1 != _rx(2)["id"]:
            failures.append(f"A-2 fresh wake/cursor wrong: fresh={[r['id'] for r in f1]} cursor={c1}")
        # filters: a reaction BY me, one targeting someone else, and a remove → none wake
        ff, _, _ = cw([_rx(3, frm=ME), _rx(4, target_from="carol"), _rx(5, op="remove")], k1, ME)
        if ff != []:
            failures.append(f"A-2 wake filter leaked: {[r['id'] for r in ff]}")
        # empty batch leaves the cursor untouched (None return → caller keeps its prior cursor)
        if cw([], k1, ME)[2] is not None:
            failures.append("A-2 empty batch returned a non-None cursor (would rewind)")

        # The actual hole: the watcher is ALREADY seeded, then >500 reactions land in one
        # window with a target-ME reaction OLDER than the post-burst newest-500 tail. The
        # legacy newest-500 read scrolls past it; the cursor-driven read pages forward and
        # still delivers it — exactly once.
        rroot = tempfile.mkdtemp(prefix="tc-wp1rx-")
        rx_file = _c.get_reactions_file(rroot, tm)
        _seed_jsonl(rx_file, [_rx(0, target_from="carol"), _rx(1, target_from="carol")])
        _, known, rcur = cw(_c.read_reactions(rroot, tm, since=None, limit=500), None, ME)
        BURST, HIT = 600, 70
        _seed_jsonl(rx_file, [_rx(i, target_from=(ME if i == HIT else "carol"),
                                  frm=("bob" if i == HIT else "dave"))
                              for i in range(10, 10 + BURST)])
        hit_id = _rx(HIT)["id"]
        if any(r["id"] == hit_id for r in _c.read_reactions(rroot, tm, limit=500)):
            failures.append("A-2 precondition broken: the hit fell inside the newest-500 tail")
        woke, guard = [], 0
        while guard < 50:
            guard += 1
            b = _c.read_reactions(rroot, tm, since=rcur, limit=500, oldest_first=True)
            fr, known, nc = cw(b, known, ME)
            woke.extend(x["id"] for x in fr)
            if nc is None or nc == rcur:
                break
            rcur = nc
        if hit_id not in woke:
            failures.append("A-2 target-author reaction beyond the 500-tail was never delivered")
        elif woke.count(hit_id) != 1:
            failures.append(f"A-2 hit delivered {woke.count(hit_id)}x (want exactly 1)")

        # ---- A-3: append_reaction/append_deletion BLOCK then succeed under contention ----
        # Hold the file's lock for ~2.5s in a background thread; the blocking append must WAIT
        # then write. The hold MUST exceed the legacy file_lock_optional(timeout=2) give-up —
        # at 0.4s the legacy code would also wait it out and write, so the test wouldn't
        # discriminate (CR-2). At 2.5s legacy drops unwritten at 2s while the new blocking
        # append (timeout=5) acquires on release and writes. (file_lock steals only AFTER its
        # timeout; we release at 2.5s < 5s, so it acquires cleanly without stealing.)
        def _hold_then_release(lock_dir, secs, done):
            lock_dir.mkdir(parents=True, exist_ok=True)
            time.sleep(secs)
            try:
                lock_dir.rmdir()
            finally:
                done.set()

        aroot = tempfile.mkdtemp(prefix="tc-wp1a3-")
        for path_fn, call, ident in (
            (_c.get_reactions_file,
             lambda: _c.append_reaction(aroot, tm, _rx(0), timeout=5), "append_reaction"),
            (_c.get_deletions_file,
             lambda: _c.append_deletion(aroot, tm, {"target": "m", "kind": "message",
                                                    "by": "a", "op": "delete"}, timeout=5),
             "append_deletion"),
        ):
            p = path_fn(aroot, tm)
            p.parent.mkdir(parents=True, exist_ok=True)
            done = threading.Event()
            ld = Path(str(p) + ".lock")
            threading.Thread(target=_hold_then_release, args=(ld, 2.5, done), daemon=True).start()
            time.sleep(0.05)  # ensure the holder grabbed the lock first
            t0 = time.monotonic()
            call()
            waited = time.monotonic() - t0
            if not done.is_set():
                failures.append(f"A-3 {ident} returned before the lock was released")
            if waited < 2.0:  # blocked PAST legacy's 2s give-up (so the test discriminates)
                failures.append(f"A-3 {ident} did not block past 2s under contention ({waited:.2f}s)")
            if p.stat().st_size == 0:
                failures.append(f"A-3 {ident} blocked but wrote nothing (lost the record)")

        # best-effort path (block=False, used by group-delete + teammate-removal) must NEVER
        # raise under contention — a destructive caller depends on it being safe to drop.
        broot = tempfile.mkdtemp(prefix="tc-wp1be-")
        bp = _c.get_deletions_file(broot, tm)
        bp.parent.mkdir(parents=True, exist_ok=True)
        bld = Path(str(bp) + ".lock")
        bld.mkdir()
        try:
            _c.append_deletion(broot, tm, {"target": "#g", "kind": "group",
                                           "by": "a", "op": "delete"}, block=False)
        except Exception as e:
            failures.append(f"A-3 append_deletion(block=False) raised under contention: {e}")
        finally:
            bld.rmdir()

        # ---- CR-1: event ids are stamped UNDER the lock, so file order == id order even
        # under contention. A pre-stamped id + the now-blocking lock could otherwise commit a
        # LOWER id after a cursor advanced past it (since=cursor excludes it forever → silent
        # missed wake). Concurrent appenders must yield a monotonically non-decreasing id
        # sequence in the file, with zero drops. (Fails against a pre-lock-stamp implementation.)
        croot = tempfile.mkdtemp(prefix="tc-wp1cr1-")
        # Generous timeout AND explicit worker-exception capture: 80-way synthetic contention can
        # drive file_lock past its timeout into the steal path, which can RAISE CommsError (this
        # flake was the first empirical sighting of that path — audit A-7). timeout=30 keeps the
        # synthetic burst from reaching it (real callers catch the raise anyway); the try/except
        # surfaces any FUTURE lock-path raise as a LABELED failure rather than a confusing
        # drop-count (a silently-dying worker is how the original 61/80 mystery read). Net:
        # the ordering invariant is isolated AND any regression is diagnosable.
        worker_errors = []

        def _spam_reactions(n):
            for _ in range(n):
                try:
                    _c.append_reaction(croot, tm, {"target": "m", "from": "bob", "emoji": "fire",
                                                   "op": "add", "target_from": "alice"}, timeout=30)
                except Exception as e:
                    worker_errors.append(f"append_reaction raised: {e!r}")

        def _spam_deletions(n):
            for _ in range(n):
                try:
                    _c.append_deletion(croot, tm, {"target": "m", "kind": "message",
                                                   "by": "bob", "op": "delete"}, timeout=30)
                except Exception as e:
                    worker_errors.append(f"append_deletion raised: {e!r}")

        for spam, read_fn, label, total in ((_spam_reactions, _c.read_reactions, "reaction", 80),
                                            (_spam_deletions, _c.read_deletions, "deletion", 80)):
            ths = [threading.Thread(target=spam, args=(20,)) for _ in range(4)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            ids = [r["id"] for r in read_fn(croot, tm)]
            if ids != sorted(ids):
                failures.append(f"CR-1 {label} ids not monotonic under contention (out-of-order race)")
            if len(ids) != total:
                failures.append(f"CR-1 {label} dropped events under contention: {len(ids)}/{total}")
        if worker_errors:  # any lock-path raise → labeled, not a silent drop-count mystery
            failures.append(f"CR-1 worker raised under lock contention (steal-path A-7): {worker_errors[:3]}")
    except Exception as e:
        failures.append(f"WP-1 missed-event unit checks errored: {e}")

    # ── WP-9 — wake reliability: re-nudge still-unseen unread with capped exponential
    #    backoff after a dropped channel push. compute_reemit is pure → hermetic. ──
    try:
        from teammate_comms.channel import REEMIT_BASE_SECONDS as _RB
        from teammate_comms.channel import REEMIT_MAX_ATTEMPTS as _RMAX
        from teammate_comms.channel import compute_reemit as _cre
        U = {"m1", "m2"}  # a non-empty unseen set

        # (1) first-emit guard: with no prior emit (last_emit None) re-nudge NEVER fires,
        #     even with unseen unread — the seed/pre-first-emit window stays nudge-silent.
        if _cre(U, 1000.0, None, 0) != (False, 0, None):
            failures.append("WP-9 first-emit guard: re-nudged with no prior emit")
        # (2) before the first threshold → no fire, clock + attempts unchanged.
        if _cre(U, 1000.0 + _RB - 1, 1000.0, 0) != (False, 0, 1000.0):
            failures.append("WP-9 fired before the base threshold")
        # (3) at the threshold → fire, attempt+1, clock advances to now.
        t1 = 1000.0 + _RB
        if _cre(U, t1, 1000.0, 0) != (True, 1, t1):
            failures.append("WP-9 did not fire at the base threshold")
        # (4) exponential backoff: attempt k waits BASE * 2**k (120, 240, 480 …).
        for k in range(_RMAX):
            wait = _RB * (2 ** k)
            if _cre(U, 5000.0 + wait - 1, 5000.0, k)[0]:
                failures.append(f"WP-9 attempt {k}: fired before its {wait}s backoff")
            if _cre(U, 5000.0 + wait, 5000.0, k) != (True, k + 1, 5000.0 + wait):
                failures.append(f"WP-9 attempt {k}: did not fire at {wait}s")
        # (5) cap: at REEMIT_MAX_ATTEMPTS, never fire again no matter how long it's been.
        if _cre(U, 1e9, 0.0, _RMAX)[0]:
            failures.append("WP-9 re-nudged past the attempt cap")
        # (6) caught up (unseen empty) → reset attempts AND DISARM (last_emit → None), so a
        #     never-emitted batch can't later be re-nudged (CR fix). NOT (False, 0, now).
        if _cre(set(), 9000.0, 1000.0, _RMAX) != (False, 0, None):
            failures.append("WP-9 empty-unseen did not disarm (must return last_emit=None)")
        # (7) read-position (v0.4.2) suppression: a fully-read batch reduces unseen to empty
        #     upstream, so even after an eternity nothing re-nudges (no waking a read message).
        unread, muted, seen = {"a", "b"}, set(), {"a", "b"}
        if _cre((unread - muted) - seen, 1e9, 0.0, 0)[0]:
            failures.append("WP-9 re-nudged for fully-read (last_seen) messages")
        # (8) cap-reset round-trip: a fresh emit (caller sets attempts=0, last_emit=now)
        #     re-arms the backoff — the next batch fires again after BASE.
        if not _cre(U, 100.0 + _RB, 100.0, 0)[0]:
            failures.append("WP-9 did not re-arm after a fresh-emit reset")
        # (9) BLOCKER regression — arrived-while-muted then unmuted must NEVER re-nudge (the
        #     v0.4.2 retro-nudge sin). While muted, unseen is empty every tick → the caught-up
        #     branch DISARMS (last_emit=None), so no non-emit ever arms the clock. On unmute
        #     the message is revealed into unseen but the clock is still None → first-emit
        #     guard → silent forever, until a genuinely new message fresh-emits (re-arms).
        le = 500.0                                   # clock as if some earlier emit existed
        for _t in range(0, 2000, 5):                 # 400 muted ticks: unseen stays empty
            _, _a, le = _cre(set(), float(_t), le, 0)
        if le is not None:
            failures.append("WP-9 muted-window left the clock armed (would retro-nudge)")
        revealed = {"m_muted"}                       # unmute reveals the absorbed message
        if _cre(revealed, 1e9, le, 0)[0]:
            failures.append("WP-9 retro-nudged an arrived-muted message after unmute (B1)")
        # (10) mute-flap: rapidly toggling muted/unmuted (empty ↔ {m}) across many ticks must
        #      never fire — the clock stays disarmed the whole time (never armed by a non-emit).
        le2, fired = None, 0
        for i in range(2000):
            unseen_tick = set() if (i % 2 == 0) else {"m_flap"}
            do, _a2, le2 = _cre(unseen_tick, float(i) * 1000.0, le2, 0)
            fired += int(do)
        if fired:
            failures.append(f"WP-9 mute-flap re-nudged {fired}x (must be 0)")
    except Exception as e:
        failures.append(f"WP-9 re-nudge unit checks errored: {e}")

    # ── WP-3 (G-1) — dashboard HTTP layer: start the real loopback server in-process and hit
    #    every endpoint with stdlib http.client (zero deps). Token/Host guards + status codes
    #    + response shapes. try/finally shutdown — _STATE is process-global, a leak would
    #    poison later blocks (and the harness never else-imports dashboard, so the port's free).
    try:
        import http.client as _hc

        from teammate_comms import dashboard as _dash

        ddroot = tempfile.mkdtemp(prefix="tc-wp3-dash-")
        # port=0 → OS-assigned free port. The default 7842 must NOT be used: the server sets
        # allow_reuse_address, so on Windows the test would bind the SAME port as a real
        # dashboard already running on 7842 and client requests would race to the wrong server
        # (a different token → spurious 401/403). An ephemeral port keeps the block hermetic.
        _dres = _dash.start_dashboard(ddroot, None, "Operator", port=0, open_browser=False)
        _dport, _dtok = _dres["port"], _dash._STATE.token

        def _dreq(method, path, token=None, host="127.0.0.1", body=None):
            conn = _hc.HTTPConnection("127.0.0.1", _dport, timeout=5)
            raw = json.dumps(body).encode("utf-8") if body is not None else None
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host)
            if token is not None:
                conn.putheader("X-Dashboard-Token", token)
            if raw is not None:
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(raw)))
            conn.endheaders(message_body=raw)
            resp = conn.getresponse()
            status, data = resp.status, resp.read()
            conn.close()
            return status, data

        try:
            # index: good token → 200 AND the served HTML actually carries the injected token.
            st, body = _dreq("GET", f"/?token={_dtok}")
            if st != 200 or _dtok.encode() not in body:
                failures.append(f"WP-3 GET / (token) -> {st}; token injected into HTML: {_dtok.encode() in body}")
            if _dreq("GET", "/?token=BAD")[0] != 403:
                failures.append("WP-3 GET / bad token did not 403")
            if _dreq("GET", f"/?token={_dtok}", host="evil.com")[0] != 403:
                failures.append("WP-3 GET / bad Host did not 403")
            # /api/conversations: token → 200 + keys; no token → 401.
            st, body = _dreq("GET", "/api/conversations", token=_dtok)
            conv = json.loads(body) if st == 200 else {}
            if st != 200 or not all(k in conv for k in ("me", "groups", "roster", "dms")):
                failures.append(f"WP-3 /api/conversations -> {st} or missing keys")
            if _dreq("GET", "/api/conversations")[0] != 401:
                failures.append("WP-3 /api/conversations no-token did not 401")
            # /api/poll: 200 + all six sub-stream keys (values empty on a fresh root).
            st, body = _dreq("GET", "/api/poll?cursor=&rcursor=&dcursor=", token=_dtok)
            poll = json.loads(body) if st == 200 else {}
            if st != 200 or not all(k in poll for k in
                                    ("records", "cursor", "reactions", "rcursor", "deletions", "dcursor")):
                failures.append(f"WP-3 /api/poll -> {st} or missing keys")
            # /api/send: valid (to a non-self name) → 200 {id}; missing 'to' → 400.
            st, body = _dreq("POST", "/api/send", token=_dtok, body={"to": "Bob", "message": "hi"})
            if st != 200 or "id" not in (json.loads(body) if st == 200 else {}):
                failures.append(f"WP-3 /api/send valid -> {st} or no id")
            if _dreq("POST", "/api/send", token=_dtok, body={})[0] != 400:
                failures.append("WP-3 /api/send missing 'to' did not 400")
            # ── WP-4 (B-1) — _api_send passes post_type/reply_to/priority through to both cores.
            #    Assert against the RECIPIENT INBOX (authoritative, always written) — NOT
            #    read_transcript (TEAMMATE_TRANSCRIPT-gated → could pass vacuously). DM + # branches.
            from teammate_comms import comms as _dc
            # DM branch: typed + threaded + urgent reach the recipient's stored record.
            st, body = _dreq("POST", "/api/send", token=_dtok,
                             body={"to": "Carol", "message": "m1", "post_type": "decision",
                                   "reply_to": "ref-123", "priority": "urgent"})
            _mid = json.loads(body).get("id") if st == 200 else None
            _cinbox = _dc.read_json_safe(_dc.get_inboxes_dir(ddroot, None) / "Carol_unread.json")
            _crec = next((m for m in _cinbox if m.get("id") == _mid), None) if _mid else None
            if not (_crec and _crec.get("post_type") == "decision"
                    and _crec.get("reply_to") == "ref-123" and _crec.get("priority") == "urgent"):
                failures.append(f"WP-4 DM send did not pass post_type/reply_to/priority through: {_crec}")
            # Group (#) branch: same pass-through into the group transcript.
            _dc.write_group_meta(ddroot, None, "g", {"name": "g", "members": ["Operator", "Dave"],
                                                     "creator": "Operator", "createdAt": _dc.now_timestamp()})
            st, body = _dreq("POST", "/api/send", token=_dtok,
                             body={"to": "#g", "message": "gm", "post_type": "blocker", "reply_to": "ref-g"})
            _gmid = json.loads(body).get("id") if st == 200 else None
            _grec = next((m for m in _dc.read_group_messages(ddroot, None, "g")
                          if m.get("id") == _gmid), None) if _gmid else None
            if not (_grec and _grec.get("post_type") == "blocker" and _grec.get("reply_to") == "ref-g"):
                failures.append(f"WP-4 group send did not pass post_type/reply_to through: {_grec}")
            # A bogus post_type is rejected by the core (_clean_post_type → CommsError → 400).
            if _dreq("POST", "/api/send", token=_dtok,
                     body={"to": "Carol", "message": "x", "post_type": "bogus"})[0] != 400:
                failures.append("WP-4 send with bogus post_type did not 400")
            # /api/react: a valid emoji always 200 (records best-effort, no transcript setup);
            # a bogus emoji → 400 (the whitelist gate).
            if _dreq("POST", "/api/react", token=_dtok, body={"target": "x", "emoji": "fire"})[0] != 200:
                failures.append("WP-3 /api/react valid emoji did not 200")
            if _dreq("POST", "/api/react", token=_dtok, body={"target": "x", "emoji": "bogus"})[0] != 400:
                failures.append("WP-3 /api/react bogus emoji did not 400")
            # /api/delete: XOR guard (neither message nor teammate) → 400. "Operator" is
            # registered so we reach the 400 guard, NOT the 409 no-identity branch.
            if _dreq("POST", "/api/delete", token=_dtok, body={})[0] != 400:
                failures.append("WP-3 /api/delete XOR guard did not 400")
            # unknown path → 404.
            if _dreq("GET", "/api/bogus", token=_dtok)[0] != 404:
                failures.append("WP-3 unknown /api path did not 404")
            # ── WP-5 (D-2) — the token must NOT leak into the dashboard's stderr logs. Capture
            #    stderr around a token-bearing GET (its request line is logged) and assert the
            #    token is ABSENT (hostile direction: prove the secret doesn't leak).
            import io as _io2
            from contextlib import redirect_stderr as _rse
            _sbuf = _io2.StringIO()
            with _rse(_sbuf):
                _dreq("GET", f"/?token={_dtok}")
                time.sleep(0.2)                      # let the server thread's access-log line flush
            if _dtok in _sbuf.getvalue():
                failures.append("WP-5 D-2: token leaked into the dashboard log (query not redacted)")
            # ── WP-5 (D-3) — a POST whose Content-Length exceeds the 1 MB cap is rejected with
            #    413 BEFORE the body is read (claim the size; the server never reads the bytes).
            from teammate_comms.dashboard import MAX_BODY_BYTES as _MBB
            _bconn = _hc.HTTPConnection("127.0.0.1", _dport, timeout=5)
            _bconn.putrequest("POST", "/api/send", skip_host=True, skip_accept_encoding=True)
            _bconn.putheader("Host", "127.0.0.1")
            _bconn.putheader("X-Dashboard-Token", _dtok)
            _bconn.putheader("Content-Type", "application/json")
            _bconn.putheader("Content-Length", str(_MBB + 1))   # claim oversize → reject pre-read
            _bconn.endheaders(message_body=b"{}")
            if _bconn.getresponse().status != 413:
                failures.append("WP-5 D-3: oversized POST body was not rejected with 413")
            _bconn.close()
        finally:
            _dash.shutdown_dashboard()
    except Exception as e:
        failures.append(f"WP-3 dashboard HTTP unit checks errored: {e}")

    # ── WP-3 (G-4) — hooks fail-closed-but-VISIBLE: with CLAUDE_PLUGIN_ROOT unset each hook
    #    must still emit valid '{}' (not die silently under set -u); session-start.sh must
    #    fast-exit '{}' on a `compact` source (the matcherless double-fire self-filter). Needs
    #    bash — SKIP (don't fail) where absent so the harness keeps no hard bash dependency. ──
    try:
        import shutil as _sh
        _bash = _sh.which("bash")
        if _bash:
            _hooks = REPO / "hooks"
            _noroot = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
            for _script in ("session-start.sh", "reinject-instructions.sh"):
                _p = subprocess.run([_bash, str(_hooks / _script)], env=_noroot,
                                    input=b"", capture_output=True, timeout=30)
                if _p.returncode != 0 or _p.stdout.decode("utf-8", "replace").strip() != "{}":
                    failures.append(f"WP-3 hook {_script} (no PLUGIN_ROOT) → rc={_p.returncode} "
                                    f"out={_p.stdout.decode('utf-8', 'replace').strip()!r}")
            # source=compact on stdin → fast '{}' (skips the venv build), regardless of env.
            _pc = subprocess.run([_bash, str(_hooks / "session-start.sh")], env=dict(os.environ),
                                 input=b'{"source":"compact"}', capture_output=True, timeout=30)
            if _pc.returncode != 0 or _pc.stdout.decode("utf-8", "replace").strip() != "{}":
                failures.append(f"WP-3 session-start.sh source=compact did not fast-exit '{{}}': "
                                f"rc={_pc.returncode}")
    except Exception as e:
        failures.append(f"WP-3 hooks unit checks errored: {e}")

    # ── WP-5 — hardening units: _redact_query (D-2), message-length cap (D-3), dispatch
    #    stderr-trace for unexpected (non-CommsError) bugs (A-9). All pure/in-process. ──
    try:
        from teammate_comms import comms as _c5
        from teammate_comms import tools as _t5
        from teammate_comms.dashboard import _redact_query as _rq
        # D-2: the helper scrubs a token-bearing query but leaves a query-less line intact.
        if "SECRET123" in _rq('"GET /?token=SECRET123 HTTP/1.1" 200 -'):
            failures.append("WP-5 D-2: _redact_query left the token in the request line")
        if "<redacted>" not in _rq("GET /?token=x HTTP/1.1"):
            failures.append("WP-5 D-2: _redact_query did not redact the query")
        if _rq('"GET /api/poll HTTP/1.1" 200 -') != '"GET /api/poll HTTP/1.1" 200 -':
            failures.append("WP-5 D-2: _redact_query mangled a query-less line")
        # D-3: message-length cap — exactly the limit passes, one over raises CommsError.
        _t5._clean_message("a" * _t5.MAX_MESSAGE_CHARS)
        try:
            _t5._clean_message("a" * (_t5.MAX_MESSAGE_CHARS + 1))
            failures.append("WP-5 D-3: _clean_message accepted an over-limit message")
        except _c5.CommsError:
            pass
        # A-9: a non-CommsError handler bug traces to STDERR (never stdout) and the dispatch
        # still returns an isError (the loop survives). _require_registered calls
        # ctx["identity"].snapshot(); a snapshot that raises KeyError is the unexpected path.
        import io as _io5
        from contextlib import redirect_stderr as _rse5

        class _BoomId:
            def snapshot(self):
                raise KeyError("boom")
        _ebuf = _io5.StringIO()
        with _rse5(_ebuf):
            _txt, _err = _t5.dispatch("teammate_send", {"to": "x", "message": "y"},
                                      {"identity": _BoomId()})
        if not _err or "Traceback" not in _ebuf.getvalue():
            failures.append(f"WP-5 A-9: unexpected error not traced to stderr / not isError (err={_err})")
    except Exception as e:
        failures.append(f"WP-5 hardening unit checks errored: {e}")

    # ── WP-6 — race + scale: A-7 dead-only atomic steal (exactly-one-winner), A-5 predicate
    #    purge, N-1 characterization of the accepted out-of-order-tee firehose limit. ──
    try:
        import shutil as _sh6
        import threading as _th6

        from teammate_comms import comms as _c6
        from teammate_comms import tools as _t6
        tm6 = None
        wroot6 = tempfile.mkdtemp(prefix="tc-wp6-")

        def _mklock(name, pid):
            ld = Path(wroot6) / f"{name}.lock"
            ld.mkdir(parents=True, exist_ok=True)
            (ld / "pid").write_text(str(pid))
            return ld

        # ---- A-7 (a): an ALIVE holder (this very process) is NEVER stolen — file_lock RAISES.
        _alive = _mklock("alive", os.getpid())
        try:
            with _c6.file_lock(Path(wroot6) / "alive", timeout=0.2):
                failures.append("A-7: stole a lock from an ALIVE holder")
        except _c6.CommsError:
            pass
        if not (_alive / "pid").exists():
            failures.append("A-7: the alive holder's lock dir was disturbed")
        _sh6.rmtree(_alive, ignore_errors=True)

        # ---- A-7 (b)+(c): force _pid_alive→False (verified dead) to test the steal path
        #      deterministically (no real-dead-pid reuse flake).
        _orig_pa = _c6._pid_alive
        _c6._pid_alive = lambda pid: False
        try:
            _mklock("dead", 99999)
            try:
                with _c6.file_lock(Path(wroot6) / "dead", timeout=0.3):
                    pass                              # acquired by stealing the dead holder
            except _c6.CommsError:
                failures.append("A-7: did NOT steal a lock from a verified-dead holder")
            # exactly-one-winner: 8 concurrent stealers on one dead lock → exactly one claims
            # (os.replace renames the stale dir to a UNIQUE name; only one wins, Windows-safe).
            _two = _mklock("two", 99999)
            _wins = []

            def _steal():
                if _c6._claim_if_dead(_two):
                    _wins.append(1)
            _ts = [_th6.Thread(target=_steal) for _ in range(8)]
            for t in _ts:
                t.start()
            for t in _ts:
                t.join()
            if len(_wins) != 1:
                failures.append(f"A-7: {len(_wins)} stealers won the atomic claim (must be EXACTLY 1)")
        finally:
            _c6._pid_alive = _orig_pa
            for _p in Path(wroot6).glob("*.claim"):     # clean any leftover claim marker
                _sh6.rmtree(_p, ignore_errors=True)

        # ---- A-7 (d): UNDETERMINED liveness (_pid_alive→None) must NOT be stolen (absence of a
        #      death proof is not a death proof — else a tasklist failure re-opens the bug).
        _c6._pid_alive = lambda pid: None
        try:
            _unk = _mklock("unk", 424242)
            if _c6._claim_if_dead(_unk):
                failures.append("A-7: stole a lock whose holder liveness is UNDETERMINED (None)")
        finally:
            _c6._pid_alive = _orig_pa
            _sh6.rmtree(Path(wroot6) / "unk.lock", ignore_errors=True)

        # ---- A-5: the predicate purge removes a member's group copies (group==sigil) ONLY —
        #      including a copy injected AFTER an id snapshot (the race-window-shrink) — and
        #      leaves non-group (DM) messages untouched.
        _c6.write_group_meta(wroot6, tm6, "g", {"name": "g", "members": ["alice", "bob"],
                                                "creator": "alice", "createdAt": _c6.now_timestamp()})
        _t6.send_group(wroot6, tm6, "alice", "#g", "g-msg")
        _t6.send_dm(wroot6, tm6, "alice", "bob", "dm-msg")
        _binbox = _c6.get_inboxes_dir(wroot6, tm6) / "bob_unread.json"
        _bm = _c6.read_json_safe(_binbox)
        _bm.append({"id": "race-1", "from": "carol", "group": "#g", "message": "raced-in"})
        _c6.write_json_atomic(_binbox, _bm)             # a copy that "landed" before the purge
        _c6.remove_group_messages_from_inbox(wroot6, tm6, "bob", "#g")
        _after = _c6.read_json_safe(_binbox)
        if any(isinstance(m, dict) and m.get("group") == "#g" for m in _after):
            failures.append("A-5: predicate purge left a group copy (incl. the raced-in one)")
        if not any(isinstance(m, dict) and not m.get("group") for m in _after):
            failures.append("A-5: predicate purge wrongly removed bob's DM")

        # ---- N-1 (characterization of the ACCEPTED firehose limit): a record teed out of order
        #      (id < the cursor) is excluded by a since=cursor read. Encodes the known behavior
        #      so the WP-7 byte-cursor implementer knows exactly what they're fixing.
        _nroot = tempfile.mkdtemp(prefix="tc-wp6-n1-")
        _c6.append_transcript(_nroot, tm6, {"id": "2026-01-01T00:00:00.000200", "message": "later"})
        _c6.append_transcript(_nroot, tm6, {"id": "2026-01-01T00:00:00.000100", "message": "raced-earlier"})
        _seen = [r.get("id") for r in _c6.read_transcript(_nroot, tm6, since="2026-01-01T00:00:00.000200")]
        if "2026-01-01T00:00:00.000100" in _seen:
            failures.append("N-1 characterization changed: the out-of-order earlier id is now included")
    except Exception as e:
        failures.append(f"WP-6 race+scale unit checks errored: {e}")

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
