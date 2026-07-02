"""Isolation test for the teammate-comms MCP server.

Drives `python -m teammate_comms.server` over a pipe with a temp comms root and
NO TEAMMATE_AGENT (so identity comes from an explicit teammate_register call, the
primary path). Asserts both halves of the unified server:

  Registration + tool gating:
    - tools/list returns 17 tools (register + 16), each with a valid object inputSchema
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
    - teammate_register echoes the profile back (role + personality shown on register)
    - `project` is auto-filled from CLAUDE_PROJECT_DIR's basename and shows in
      whoami / teammate_list / teammate_profile
    - teammate_update changes status; teammate_list always shows project/status/
      authority (WP-11b: personality dropped from list, use teammate_profile);
      teammate_profile returns the full profile;
      a profile field SURVIVES a heartbeat cycle
    - the channel wake names the message source (sender/group); personality never
      appears in wakes (WP-11a dropped the owner-reminder — registration echo suffices)

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
from datetime import datetime, timedelta
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
PROJECT = "MyTestProject"  # auto-filled from CLAUDE_PROJECT_DIR (F-4: as two-component parent/name)
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


def send_raw(proc, raw_line):
    """Write a raw line to stdin verbatim — for frames that aren't a JSON object (WP-16 AC-1)."""
    proc.stdin.write((raw_line + "\n").encode("utf-8"))
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


def wait_until(predicate, timeout=12.0, interval=0.1):
    """Poll until ``predicate()`` is truthy or ``timeout`` elapses; return its last value (G-3).

    Deadline-polling beats a fixed ``time.sleep`` against the real 5s heartbeat: robust on slow CI
    (it waits longer when needed) and quicker on fast machines (returns as soon as the signal
    lands). On timeout it returns the falsy value and the caller proceeds, so a genuinely missing
    signal still surfaces as the downstream assertion's failure (never a hang)."""
    deadline = time.time() + timeout
    val = predicate()
    while not val and time.time() < deadline:
        time.sleep(interval)
        val = predicate()
    return val


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

    # Heartbeat cycle (5s): processes reaction wakes + confirms type:"full" + a profile field
    # survive the registry merge. G-3: deadline-poll on the reaction-wake SIGNAL instead of a fixed
    # 5.5s sleep — the tick that emits the wake is the tick that does the merge, so the wake's
    # arrival proves both ran; the atomic record write means a post-wake read can't see a torn merge.
    def _reaction_wake_seen():
        for _l in list(stdout_lines):
            try:
                _m = json.loads(_l)
            except (ValueError, TypeError):
                continue
            if (_m.get("method") == "notifications/claude/channel"
                    and (_m.get("params") or {}).get("meta", {}).get("kind") == "reaction"):
                return True
        return False
    wait_until(_reaction_wake_seen, timeout=12.0)   # > 2 heartbeats so a slow first tick still lands
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
    # Kept as a fixed heartbeat-length sleep (NOT deadline-polled, unlike the reaction wait above):
    # this waits for the watcher to REFRESH its in-memory muted cache from the record — an INTERNAL
    # state change with no observable pre-signal (the only proof is the post-unmute wake that
    # follows, which we can't poll for before it exists). Forcing a poll here would be guesswork.
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

    # WP-11b: teammate_list all=True — smoke test that the param is accepted (no shared-inbox side-effects)
    send(proc, {"jsonrpc": "2.0", "id": 62, "method": "tools/call",
                "params": {"name": "teammate_list", "arguments": {"all": True}}})
    time.sleep(0.5)

    # ── WP-16: envelope guard + version negotiation + notification discipline ──
    # AC-1: malformed non-dict frames (null, a bare scalar, a JSON-RPC batch array — batching
    # is unsupported here) must not kill the server; a subsequent request (id 64) still answers.
    # ids 63/64/65 are free (out-of-sequence ids are fine; by_id is order-independent).
    # Crash-tolerant (gate micro-CR): if server.py regresses on S1, the server actually dies and
    # writing to its closed stdin raises OSError — uncaught, that would abort this WHOLE script
    # before any FAIL prints, so a real S1 regression would read as harness flakiness instead of
    # a named failure. Catch it here, record the specific reason, and let the run finish so every
    # other id's assertion still reports.
    wp16_frame_error = None
    try:
        send_raw(proc, "null")
        send_raw(proc, '"scalar"')
        send_raw(proc, json.dumps([{"jsonrpc": "2.0", "id": 63, "method": "ping"}]))
        time.sleep(0.4)
        send(proc, {"jsonrpc": "2.0", "id": 64, "method": "ping"})
        time.sleep(0.4)
        # AC-2: a mid-session re-initialize with a bogus protocolVersion must be answered with
        # OUR version (non-echo) — the FIRST initialize (id 1) can't prove this since the
        # harness sent the same version we'd answer regardless.
        send(proc, {"jsonrpc": "2.0", "id": 65, "method": "initialize",
                    "params": {"protocolVersion": "1999-01-01", "capabilities": {}}})
        time.sleep(0.4)
        # AC-3: a notification-form ping (no id) must produce NO response frame.
        send(proc, {"jsonrpc": "2.0", "method": "ping"})
        time.sleep(0.4)
    except OSError as e:
        wp16_frame_error = str(e)

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
    # Responses are matched by request id, so the SEND order and the gaps in the id space don't
    # matter: ids are allocated per logical step, NOT strictly monotonically — some are reserved
    # for a later cluster (e.g. the group/mute/unmute steps send 43/44/46 then the gated-reincarnate
    # probe reuses 52) and a few group-history reads (35/36/45) resolve out of numeric order. by_id
    # makes lookup order-independent; the gaps are intentional, not missing requests.
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
        # WP-10: the authority-coordination standing rule reaches the handshake too. Bind its
        # DISTINCTIVE reason-phrase (who-owns + coordinate-before) — NOT the bare token "authority",
        # which already appears in the profile-fields list (asserting that would be a tautology).
        _instr_l = instr.lower()
        if "authority over the areas" not in _instr_l or "before you modify" not in _instr_l:
            failures.append(f"initialize instructions missing the authority-coordination rule (WP-10): {instr[:120]!r}")

    # WP-16 gate micro-CR: the pipe block itself hit an OSError (server died mid-block) —
    # report this FIRST and specifically, since it explains why the AC-1/2/3 checks below fail.
    if wp16_frame_error:
        failures.append(f"WP-16: server died on a malformed frame — S1 envelope-guard regression "
                         f"(write failed: {wp16_frame_error})")
    # WP-16 AC-1: a malformed non-dict frame (null / bare scalar / batch array) must not kill
    # the server — assert the SPECIFIC reason (a -32600 frame emitted, id 64 still answered),
    # not just "something didn't throw" (a dead server would silently fail both checks below).
    bad_frame_errors = [m for m in msgs if m.get("id") is None and (m.get("error") or {}).get("code") == -32600]
    if not bad_frame_errors:
        failures.append("no -32600 error frame emitted for a malformed non-dict request (WP-16 AC-1)")
    if by_id.get(64, {}).get("result") != {}:
        failures.append(f"ping (id 64) after malformed frames unanswered — server likely died (WP-16 AC-1): {by_id.get(64)}")
    # WP-16 AC-2: mid-session re-initialize with a bogus protocolVersion answers with OUR
    # version, proving non-echo (the id-1 initialize alone can't prove this).
    if by_id.get(65, {}).get("result", {}).get("protocolVersion") != "2025-06-18":
        failures.append(f"re-initialize (id 65) did not answer with our protocol version (WP-16 AC-2): {by_id.get(65)}")
    # WP-16 AC-3: a notification-form ping (no id) produces NO response frame. -32600 error
    # frames legitimately carry id null — only exclude those, don't over-assert.
    if any("id" in m and m["id"] is None and "result" in m for m in msgs):
        failures.append("notification-form ping produced a response frame (WP-16 AC-3, should be silent)")

    # tools/list: 18 tools (13 original + 4 project-profile tools + 1 avatar tool), each with an object inputSchema
    tl = result(2).get("tools")
    expected_names = {"teammate_register", "teammate_send", "teammate_inbox",
                      "teammate_ack", "teammate_list", "teammate_whoami",
                      "teammate_update", "teammate_profile", "teammate_group",
                      "teammate_react", "teammate_reincarnate", "teammate_dashboard",
                      "teammate_delete",
                      "project_register", "list_projects", "project_profile", "project_delete",
                      "teammate_set_avatar"}
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
    # WP-19 AC-5: the normal single-owner register path must stay silent — no collision warning.
    if "WARNING" in text(5):
        failures.append(f"WP-19 AC-5: normal single-owner register wrongly printed a "
                         f"collision warning: {text(5)}")
    if '"registered": true' not in text(6).lower() or AGENT not in text(6):
        failures.append(f"whoami after register wrong: {text(6)}")
    # whoami echoes the profile set at registration
    if STATUS_INIT not in text(6) or ROLE not in text(6):
        failures.append(f"whoami missing profile fields: {text(6)}")
    # project auto-filled as TWO-COMPONENT parent/name from CLAUDE_PROJECT_DIR (F-4):
    # "C:/some/path/MyTestProject" -> "path/MyTestProject" (not the bare basename), not passed explicitly
    if "path/" + PROJECT not in text(6):
        failures.append(f"whoami project not two-component 'path/{PROJECT}' (F-4): {text(6)}")

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
    # v0.6.0: the wake names WHERE the message came from (the DM sender).
    # WP-11a: the v0.6 owner-reminder ("You are <name>: <personality>") was dropped —
    # the persona is durable in the agent's session; PERSONALITY must not appear in any wake.
    elif "tester" not in mwakes[0]["params"].get("content", ""):
        failures.append(f"channel wake did not name the source/sender: {mwakes[0]['params'].get('content')}")
    elif any(PERSONALITY in n["params"].get("content", "") for n in mwakes):
        failures.append(f"WP-11a: personality reminder appeared in a wake (should be gone)")
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
    # WP-11b: personality is dropped from teammate_list output (use teammate_profile for full details)
    if PERSONALITY in text(17):
        failures.append(f"WP-11b: teammate_list should not include personality (use teammate_profile): {text(17)}")
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
    # (stdout-purity is reported FIRST in the report section below — a break there cascades into
    #  many false downstream by_id-miss failures, so it's surfaced as the probable ROOT CAUSE.)
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

    # ── WP-11b — lean surface & outputs ──
    # teammate_list all=True accepted without error and returns roster
    if is_error(62) or "Registered teammates" not in text(62):
        failures.append(f"WP-11b: teammate_list(all=True) failed: {text(62)}")

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
        _ins_l = _ins.INSTRUCTIONS.lower()   # WP-10: authority-coordination rule, distinctive phrase
        if "authority over the areas" not in _ins_l or "before you modify" not in _ins_l:
            failures.append("INSTRUCTIONS missing the WP-10 authority-coordination standing rule")
        _buf = _io.StringIO()
        with _redirect(_buf):
            _ins.main()
        _emitted = json.loads(_buf.getvalue())  # must be valid JSON
        _hso = _emitted.get("hookSpecificOutput", {})
        if _hso.get("hookEventName") != "SessionStart":
            failures.append(f"reinject hookEventName wrong: {_hso.get('hookEventName')}")
        if "status as you work" not in _hso.get("additionalContext", ""):
            failures.append("reinject additionalContext missing the standing rule")
        _ac_l = _hso.get("additionalContext", "").lower()   # WP-10: the reinject path carries it too
        if "authority over the areas" not in _ac_l or "before you modify" not in _ac_l:
            failures.append("reinject additionalContext missing the WP-10 authority-coordination rule")
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

    # ── WP-11b — inbox body suppression (hermetic, isolated temp root — no shared inbox) ──
    # Tests _handle_inbox's _prev_seen/new_msgs/seen_msgs split directly. A second read in the
    # same session must suppress already-shown bodies; show_all=True restores them.
    # Isolated from the channel harness to prevent polluting the timing-sensitive watcher tests.
    try:
        from teammate_comms.tools import _handle_inbox as _hi11b
        from teammate_comms import comms as _c11b

        _b11b_root = tempfile.mkdtemp(prefix="tc-11b-")
        _b11b_agent = "probe-agent"
        _b11b_team = None

        _inboxes11b = _c11b.get_inboxes_dir(_b11b_root, _b11b_team)
        _c11b.ensure_inbox(_inboxes11b, _b11b_agent)
        _probe_msg = {"id": "probe-msg-1", "from": "probe-peer", "priority": "normal",
                      "message": "suppression-probe-body"}
        _c11b.write_json_atomic(_inboxes11b / f"{_b11b_agent}_unread.json", [_probe_msg])

        class _Id11b:
            def __init__(self):
                self._ls = None
            def snapshot(self):
                return (_b11b_agent, _b11b_team, _b11b_root, None)
            def get_last_seen(self):
                return self._ls
            def set_last_seen(self, v):
                self._ls = v

        _ctx11b = {"identity": _Id11b()}

        # First read: body must appear (message is new this session)
        _r1 = _hi11b({}, _ctx11b)
        if "suppression-probe-body" not in _r1:
            failures.append(f"WP-11b suppression: first read must show the body: {_r1}")

        # Second read: body must be suppressed (id now in last_seen); note must appear
        _r2 = _hi11b({}, _ctx11b)
        if "suppression-probe-body" in _r2:
            failures.append(f"WP-11b suppression: second read re-dumped already-seen body (should suppress): {_r2}")
        if "already delivered" not in _r2 and "No new messages" not in _r2:
            failures.append(f"WP-11b suppression: second read missing suppression note: {_r2}")

        # show_all=True: body must reappear (escape hatch for post-compaction re-read)
        _r3 = _hi11b({"show_all": True}, _ctx11b)
        if "suppression-probe-body" not in _r3:
            failures.append(f"WP-11b suppression show_all=True: body should reappear: {_r3}")

    except Exception as e:
        failures.append(f"WP-11b inbox suppression unit checks errored: {e}")

    # ── WP-12 — live-watcher re-nudge + re-register-reset (the regression guards that were
    #    missing from WP-11a and let the watcher-crash bug ship green). Drive run_watcher on a
    #    daemon thread with a fake send_message; monkeypatch REEMIT_BASE_SECONDS small; use
    #    deadline-polling (not fixed sleeps); own temp root per block (hermetic). ──
    try:
        import threading as _th12
        from teammate_comms import channel as _ch12
        from teammate_comms import comms as _c12

        _TINY_BASE = 0.05  # 50 ms → re-nudge fires in <200 ms even on a loaded CI runner

        def _wait_until12(cond, timeout=5.0, interval=0.05):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cond():
                    return True
                time.sleep(interval)
            return False

        # ── WP-12 (a): group re-nudge recovers — watcher must still be alive after the re-nudge
        #    (pre-fix it dies on the TypeError from the stray positional None). ──
        try:
            _wroot12a = tempfile.mkdtemp(prefix="tc-wp12a-")
            _agent12a = "wp12a-agent"
            _inboxes12a = _c12.get_inboxes_dir(_wroot12a, None)
            _c12.ensure_inbox(_inboxes12a, _agent12a)
            _unread12a = _inboxes12a / f"{_agent12a}_unread.json"

            _emits12a = []

            def _send12a(obj):
                m = obj.get("method", "")
                if m == "notifications/claude/channel":
                    _emits12a.append(obj)

            # Generation-aware mock identity
            class _Id12a:
                def __init__(self):
                    self._lock = _th12.Lock()
                    self._gen = 0
                    self._ls = None
                def set(self, agent, team, root, uf):
                    with self._lock:
                        self._gen += 1
                        self._agent, self._team, self._root, self._uf = agent, team, root, uf
                def snapshot(self):
                    with self._lock:
                        return (self._agent, self._team, self._root, self._uf)
                def get_generation(self):
                    with self._lock:
                        return self._gen
                def get_last_seen(self):
                    with self._lock:
                        return self._ls
                def set_last_seen(self, v):
                    with self._lock:
                        self._ls = v
                def get_instance_id(self):  # WP-19: run_watcher's heartbeat branch needs this
                    return "test-instance-id"
                def get_epoch(self):
                    return 1

            _id12a = _Id12a()
            _id12a.set(_agent12a, None, _wroot12a, _unread12a)

            _init12a = _th12.Event()
            _reg12a = _th12.Event()
            _stop12a = _th12.Event()
            _init12a.set()
            _reg12a.set()

            _orig_base = _ch12.REEMIT_BASE_SECONDS
            _ch12.REEMIT_BASE_SECONDS = _TINY_BASE
            try:
                _wt12a = _th12.Thread(
                    target=_ch12.run_watcher,
                    args=(_send12a, _id12a, _init12a, _reg12a, _stop12a),
                    daemon=True,
                )
                _wt12a.start()
                time.sleep(1.0)  # let the watcher seed known_ids on the empty inbox

                # Inject a group message into the inbox
                _gmsg = {"id": "g-msg-12a", "from": "peer", "group": "#team",
                         "message": "group-content"}
                _c12.write_json_atomic(_unread12a, [_gmsg])

                # Wait for fresh emit
                if not _wait_until12(lambda: len(_emits12a) >= 1, timeout=5.0):
                    failures.append("WP-12a: no fresh emit for group message within 5s")
                else:
                    # Don't ack — wait for re-nudge (backoff = TINY_BASE)
                    if not _wait_until12(lambda: len(_emits12a) >= 2,
                                         timeout=_TINY_BASE * 20 + 3.0):
                        failures.append("WP-12a: no re-nudge emit for group message (watcher may have crashed)")
                    else:
                        # Watcher must still be alive after the re-nudge (pre-fix it dies on TypeError)
                        if not _wt12a.is_alive():
                            failures.append("WP-12a: watcher thread DIED after re-nudge (TypeError crash not fixed)")
                        # Re-nudge emit must name the group target ("to:'#team'")
                        _renudge_content = _emits12a[1].get("params", {}).get("content", "")
                        if "to:'#team'" not in _renudge_content:
                            failures.append(f"WP-12a: re-nudge emit did not name group target: {_renudge_content!r}")
                        # Cap check: ≤ 1 + REEMIT_MAX_ATTEMPTS total emits
                        _max_exp = 1 + _ch12.REEMIT_MAX_ATTEMPTS
                        if len(_emits12a) > _max_exp + 1:  # +1 grace for timing overlap
                            failures.append(f"WP-12a: too many emits ({len(_emits12a)} > cap {_max_exp})")
            finally:
                _stop12a.set()
                _ch12.REEMIT_BASE_SECONDS = _orig_base
                _wt12a.join(timeout=2.0)
        except Exception as _e12a:
            failures.append(f"WP-12a live-watcher group-renudge errored: {_e12a}")

        # ── WP-12 (b): no-noise live guard — a read message must not re-nudge. ──
        try:
            _wroot12b = tempfile.mkdtemp(prefix="tc-wp12b-")
            _agent12b = "wp12b-agent"
            _inboxes12b = _c12.get_inboxes_dir(_wroot12b, None)
            _c12.ensure_inbox(_inboxes12b, _agent12b)
            _unread12b = _inboxes12b / f"{_agent12b}_unread.json"

            _emits12b = []

            def _send12b(obj):
                if obj.get("method") == "notifications/claude/channel":
                    _emits12b.append(obj)

            class _Id12b:
                def __init__(self):
                    self._lock = _th12.Lock()
                    self._gen = 1
                    self._ls = None
                def snapshot(self):
                    return (_agent12b, None, _wroot12b, _unread12b)
                def get_generation(self):
                    with self._lock:
                        return self._gen
                def get_last_seen(self):
                    with self._lock:
                        return self._ls
                def set_last_seen(self, v):
                    with self._lock:
                        self._ls = v
                def get_instance_id(self):  # WP-19: run_watcher's heartbeat branch needs this
                    return "test-instance-id"
                def get_epoch(self):
                    return 1

            _id12b = _Id12b()
            _init12b = _th12.Event()
            _reg12b = _th12.Event()
            _stop12b = _th12.Event()
            _init12b.set()
            _reg12b.set()

            _orig_base2 = _ch12.REEMIT_BASE_SECONDS
            _ch12.REEMIT_BASE_SECONDS = _TINY_BASE
            try:
                _wt12b = _th12.Thread(
                    target=_ch12.run_watcher,
                    args=(_send12b, _id12b, _init12b, _reg12b, _stop12b),
                    daemon=True,
                )
                _wt12b.start()
                time.sleep(1.0)  # let the watcher seed known_ids on the empty inbox

                _dmsg = {"id": "dm-12b", "from": "peer", "message": "dm-content"}
                _c12.write_json_atomic(_unread12b, [_dmsg])

                # Wait for fresh emit
                if not _wait_until12(lambda: len(_emits12b) >= 1, timeout=5.0):
                    failures.append("WP-12b no-noise: no fresh DM emit")
                else:
                    # Mark as read (set_last_seen with the message id)
                    _id12b.set_last_seen({"dm-12b"})
                    # Wait well past the re-nudge window — no second emit should arrive
                    time.sleep(_TINY_BASE * 6)
                    if len(_emits12b) > 1:
                        failures.append(f"WP-12b no-noise: re-nudged a read message ({len(_emits12b)} emits)")
            finally:
                _stop12b.set()
                _ch12.REEMIT_BASE_SECONDS = _orig_base2
                _wt12b.join(timeout=2.0)
        except Exception as _e12b:
            failures.append(f"WP-12b no-noise live guard errored: {_e12b}")

        # ── WP-12 (c): re-registration reset — same-name re-register bumps generation, watcher
        #    resets known_ids; pre-existing message absorbed at re-seed does NOT nudge; a new
        #    arrival after re-seed DOES nudge. ──
        try:
            _wroot12c = tempfile.mkdtemp(prefix="tc-wp12c-")
            _agent12c = "wp12c-agent"
            _inboxes12c = _c12.get_inboxes_dir(_wroot12c, None)
            _c12.ensure_inbox(_inboxes12c, _agent12c)
            _unread12c = _inboxes12c / f"{_agent12c}_unread.json"

            # Pre-seed inbox with a message BEFORE watcher starts
            _old_msg = {"id": "old-12c", "from": "peer", "message": "pre-existing"}
            _c12.write_json_atomic(_unread12c, [_old_msg])

            _emits12c = []

            def _send12c(obj):
                if obj.get("method") == "notifications/claude/channel":
                    _emits12c.append(obj)

            class _Id12c:
                def __init__(self):
                    self._lock = _th12.Lock()
                    self._gen = 1
                    self._ls = None
                def snapshot(self):
                    with self._lock:
                        return (_agent12c, None, _wroot12c, _unread12c)
                def get_generation(self):
                    with self._lock:
                        return self._gen
                def bump(self):
                    with self._lock:
                        self._gen += 1
                def get_last_seen(self):
                    with self._lock:
                        return self._ls
                def set_last_seen(self, v):
                    with self._lock:
                        self._ls = v
                def get_instance_id(self):  # WP-19: run_watcher's heartbeat branch needs this
                    return "test-instance-id"
                def get_epoch(self):
                    return 1

            _id12c = _Id12c()
            _init12c = _th12.Event()
            _reg12c = _th12.Event()
            _stop12c = _th12.Event()
            _init12c.set()
            _reg12c.set()

            _orig_base3 = _ch12.REEMIT_BASE_SECONDS
            _ch12.REEMIT_BASE_SECONDS = _TINY_BASE
            try:
                _wt12c = _th12.Thread(
                    target=_ch12.run_watcher,
                    args=(_send12c, _id12c, _init12c, _reg12c, _stop12c),
                    daemon=True,
                )
                _wt12c.start()

                # Let watcher seed (old_msg absorbed into known_ids — no emit)
                time.sleep(1.0)
                if _emits12c:
                    failures.append(f"WP-12c: pre-existing message caused a spurious emit at seed ({_emits12c})")

                # Simulate same-name re-register: bump generation (old_msg still in inbox)
                _id12c.bump()

                # Wait for the watcher to detect the new generation and re-seed — still no emit
                # Needs ≥2 poll cycles: one to detect + reset, one to re-seed.
                time.sleep(1.2)
                if _emits12c:
                    failures.append(f"WP-12c: re-seed after re-register caused spurious emit ({_emits12c})")

                # Now inject a NEW message — must trigger a fresh nudge
                _new_msg = {"id": "new-12c", "from": "peer", "message": "new-after-rereg"}
                _c12.write_json_atomic(_unread12c, [_old_msg, _new_msg])
                if not _wait_until12(lambda: len(_emits12c) >= 1, timeout=5.0):
                    failures.append("WP-12c: no fresh nudge for new message after re-register reset")
            finally:
                _stop12c.set()
                _ch12.REEMIT_BASE_SECONDS = _orig_base3
                _wt12c.join(timeout=2.0)
        except Exception as _e12c:
            failures.append(f"WP-12c re-register-reset errored: {_e12c}")

    except Exception as e:
        failures.append(f"WP-12 live-watcher unit checks errored: {e}")

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

        import socket as _sock6

        def _mklock(name, pid, host=None):
            ld = Path(wroot6) / f"{name}.lock"
            ld.mkdir(parents=True, exist_ok=True)
            (ld / "pid").write_text(f"{pid}\n{_sock6.gethostname() if host is None else host}")
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
        finally:
            _c6._pid_alive = _orig_pa
            for _p in Path(wroot6).glob("*.claim"):     # clean any leftover claim marker
                _sh6.rmtree(_p, ignore_errors=True)

        # ---- A-7 (g) [release-gate de-flake]: contention EXCLUSION through the PRODUCTION path.
        #      The old test ("exactly one of 8 _claim_if_dead returns True") raced shutil.rmtree on
        #      Windows — a lingering dir (a peer thread's open pid-read handle) read as a SECOND
        #      'win'. That's a TEST artifact, NOT a prod two-writers: a steal is followed by an
        #      EXCLUSIVE re-mkdir (the true arbiter), and a lingered rmtree routes through file_lock's
        #      FileExistsError-retry, which recovers. Re-aim at what matters: N contenders go through
        #      REAL file_lock on a stealable dead lock; NO TWO may hold at once, and the dead lock
        #      must be recovered (>=1 acquires). Asserting serial hand-off of ALL N would itself be
        #      timing-dependent (a thread crossing its 0.3s cliff while a peer holds raises, legitimately)
        #      — "no simultaneous holders + >=1 acquirer" is the right, flake-immune invariant.
        #      DO NOT mock _pid_alive here: after the steal a LIVE holder (a real peer thread, real
        #      pid) must stay UN-stealable, so the real liveness check is load-bearing — an always-dead
        #      mock would defeat the steal's re-verify by construction (and make the live peer stealable
        #      → a false red AND a miniature of the very two-writers this guards).
        _ex = Path(wroot6) / "excl"
        _exld = Path(str(_ex) + ".lock")
        _exld.mkdir()
        (_exld / "pid").write_text(f"2000000000\n{_sock6.gethostname()}")   # > pid_max / no such PID → really dead
        _held = [False]
        _overlap = []
        _exacq = [0]
        _exerr = []
        try:
            def _contend():
                try:
                    with _c6.file_lock(_ex, timeout=0.3):
                        if _held[0]:
                            _overlap.append(1)           # two holders at once = exclusion broken
                        _held[0] = True
                        time.sleep(0.01)                 # widen the overlap window
                        _held[0] = False
                    _exacq[0] += 1                        # completed a full EXCLUSIVE hold
                except _c6.CommsError:
                    pass                                 # a live peer held through our timeout — not a violation
                except Exception as _e:
                    _exerr.append(repr(_e))              # record, never mask
            _ets = [_th6.Thread(target=_contend) for _ in range(5)]
            for t in _ets:
                t.start()
            for t in _ets:
                t.join()
            if _overlap:
                failures.append(f"A-7 exclusion BROKEN: {len(_overlap)} simultaneous-holder event(s) (two-writers)")
            if _exacq[0] < 1:
                failures.append("A-7 exclusion: NO thread acquired the dead lock (steal path not exercised)")
            if _exerr:
                failures.append(f"A-7 exclusion worker error(s): {_exerr[:2]}")
        finally:
            for _p in Path(wroot6).glob("excl*"):
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

        # ---- A-7 (e) CR-1: a FRESH .claim marker is respected (not stolen past), but an
        #      ORPHANED one (older than CLAIM_STALE_SECONDS — a stealer killed mid-claim) is
        #      reclaimed so a dead holder's lock can't get permanently stuck.
        _c6._pid_alive = lambda pid: False
        try:
            _af = _mklock("aged", 99999)
            _fresh = Path(str(_af) + ".claim")
            _fresh.mkdir()
            if _c6._claim_if_dead(_af):
                failures.append("A-7 CR-1: stole past a FRESH claim marker")
            _fresh.rmdir()
            _ag2 = _mklock("aged2", 99999)
            _stale = Path(str(_ag2) + ".claim")
            _stale.mkdir()
            _old = time.time() - (_c6.CLAIM_STALE_SECONDS + 60)
            os.utime(_stale, (_old, _old))
            if not _c6._claim_if_dead(_ag2):
                failures.append("A-7 CR-1: did NOT reclaim an ORPHANED (aged) claim → lock would stick")
        finally:
            _c6._pid_alive = _orig_pa
            for _p in Path(wroot6).glob("aged*"):
                _sh6.rmtree(_p, ignore_errors=True)

        # ---- A-7 (f) CR-2: a holder on a DIFFERENT host with a locally-dead pid is NOT stolen
        #      (host-gated liveness — local _pid_alive can't judge a remote pid; A-7 must not
        #      relocate cross-host).
        _c6._pid_alive = lambda pid: False
        try:
            _rem = _mklock("remote", 99999, host="some-other-host-xyz")
            if _c6._claim_if_dead(_rem):
                failures.append("A-7 CR-2: stole a lock held on a DIFFERENT host (cross-host steal)")
        finally:
            _c6._pid_alive = _orig_pa
            _sh6.rmtree(Path(wroot6) / "remote.lock", ignore_errors=True)

        # ---- A-7 (g) CR-3 [release-gate]: STALE death-evidence in the steal path. An earlier
        #      stealer can remove the dead lock and re-acquire a LIVE one BETWEEN our top-of-function
        #      pid read and our winning the .claim; the post-claim guard must RE-READ + RE-VERIFY the
        #      pid (not just exists(), which can't tell the live re-acquire from the old dead lock),
        #      or it rmtrees the live holder's lock = the two-writers A-7 corruption reintroduced by
        #      the defense. Deterministic: the FIRST _pid_alive call (top read) rewrites the lock's
        #      pid to THIS live process (simulating the re-acquire) and reports the ORIGINAL pid dead
        #      so we proceed into the claim; the re-verify then re-reads the now-LIVE pid and must
        #      DECLINE. Unfixed (exists()-only) code STEALS it — so this fails hard without the fix.
        _calls = []
        lock_cr = _mklock("cr3", 99999)

        def _fake_pa(pid):
            _calls.append(pid)
            if len(_calls) == 1:                       # top read: an earlier stealer re-acquired LIVE
                (lock_cr / "pid").write_text(f"{os.getpid()}\n{_sock6.gethostname()}")
                return False                           # the ORIGINAL dead pid still reads dead → proceed
            return True if pid == os.getpid() else _orig_pa(pid)   # re-verify sees the LIVE re-acquirer
        _c6._pid_alive = _fake_pa
        try:
            _stole = _c6._claim_if_dead(lock_cr)
        finally:
            _c6._pid_alive = _orig_pa
        if _stole is not False:
            failures.append("A-7 CR-3: STOLE a lock re-acquired LIVE between top-read and claim (stale evidence)")
        if len(_calls) < 2:
            failures.append(f"A-7 CR-3: the post-claim re-verify did NOT run ({len(_calls)} _pid_alive call(s) — fix absent?)")
        elif _calls[1] != os.getpid():
            failures.append(f"A-7 CR-3: re-verify did not re-read the fresh (live) pid: {_calls[1]}")
        if not lock_cr.exists():
            failures.append("A-7 CR-3: the re-acquired LIVE lock was DESTROYED (two-writers reintroduced)")
        if Path(str(lock_cr) + ".claim").exists():
            failures.append("A-7 CR-3: the .claim marker leaked (finally cleanup regressed)")
        _sh6.rmtree(lock_cr, ignore_errors=True)

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

    # ── WP-7 P4 (C-3) — teammate_inbox since/limit windowing; the SILENT-LOSS guard: ack("all")
    #    after a LIMITED read clears only what was SHOWN, never unshown messages. ──
    try:
        from teammate_comms import comms as _c7
        from teammate_comms import tools as _t7

        class _Id7:
            def __init__(self, agent, root):
                self.agent, self.root, self._seen = agent, root, None

            def snapshot(self):
                return (self.agent, None, self.root, None)

            def set_last_seen(self, ids):
                self._seen = set(ids)

            def get_last_seen(self):
                return self._seen
        proot7 = tempfile.mkdtemp(prefix="tc-wp7-p4-")
        for i in range(5):
            _t7.send_dm(proot7, None, "alice", "bob", f"m{i}")
        _ctx7 = {"identity": _Id7("bob", proot7)}
        _binb = _c7.get_inboxes_dir(proot7, None) / "bob_unread.json"
        # limited read → most recent 2; header notes it; set_last_seen reflects ONLY those 2.
        _out = _t7._handle_inbox({"limit": 2}, _ctx7)
        if "showing 2 of 5" not in _out:
            failures.append(f"P4: limited inbox didn't note 'showing 2 of 5': {_out[:90]!r}")
        _seen = _ctx7["identity"].get_last_seen()
        if _seen is None or len(_seen) != 2:
            failures.append(f"P4: set_last_seen did not reflect ONLY the 2 shown ids ({_seen})")
        # ack-all now clears ONLY the 2 shown — the other 3 remain unread (NO silent loss).
        _t7._handle_ack({"id": "all"}, _ctx7)
        _rem = _c7.read_json_safe(_binb)
        if len(_rem) != 3:
            failures.append(f"P4 SILENT-LOSS: ack('all') after limit=2 left {len(_rem)} unread (want 3)")
        # since filter: only ids >= the cursor (page forward). The 3 remaining are the oldest 3.
        _ids = sorted(m["id"] for m in _rem)
        _sout = _t7._handle_inbox({"since": _ids[-1]}, {"identity": _Id7("bob", proot7)})
        if "1 unread message(s)" not in _sout:
            failures.append(f"P4 since filter: expected 1 message at/after the newest remaining: {_sout[:90]!r}")
        # _read.json cap is module-level (_READ_CAP) and trims on ack — assert the constant is
        # sane (a heavy 1000-ack test isn't worth the runtime; the trim is a plain slice).
        if not (isinstance(_t7._READ_CAP, int) and _t7._READ_CAP > 0):
            failures.append("P4: _READ_CAP is not a positive int")
        # P4 CR (v0.4.2 composition): an id SHOWN on an earlier page must STAY in last_seen when
        # a later windowed read shows a DIFFERENT page — else it re-counts as unseen and the
        # watcher would re-nudge a message the agent already read. last_seen is union-with-prune.
        uroot = tempfile.mkdtemp(prefix="tc-wp7-p4u-")
        for i in range(5):
            _t7.send_dm(uroot, None, "alice", "bob", f"u{i}")
        _uctx = {"identity": _Id7("bob", uroot)}
        _uids = sorted(m["id"] for m in _c7.read_json_safe(_c7.get_inboxes_dir(uroot, None) / "bob_unread.json"))
        _t7._handle_inbox({"since": _uids[1]}, _uctx)     # page A: shows ids >= id1 (incl _uids[1])
        _t7._handle_inbox({"limit": 1}, _uctx)            # page B: shows only the newest id
        if _uids[1] not in (_uctx["identity"].get_last_seen() or set()):
            failures.append("P4 CR: an earlier-page id fell out of last_seen on a later windowed "
                            "read (would re-nudge an already-read message)")
    except Exception as e:
        failures.append(f"WP-7 P4 inbox-windowing unit checks errored: {e}")

    # ── WP-7 P1 (C-1) — read_jsonl_tail: seek-from-tail NDJSON reader. Reads only the file's
    #    tail, byte-identical to read_transcript(...)[-N:]; the react/resolve scans use it
    #    tail-first with a full-scan fallback. Edge cases stress chunk-boundary straddles. ──
    try:
        from teammate_comms import comms as _cp1
        from teammate_comms import tools as _tp1

        def _wb(_path, _data):
            with open(_path, "wb") as _f:
                _f.write(_data)

        _d1 = tempfile.mkdtemp(prefix="tc-wp7-p1-")
        _p = os.path.join(_d1, "t.jsonl")

        # (1) no trailing newline — the last (unterminated) record must still be returned.
        _wb(_p, b'{"id":"a","n":1}\n{"id":"b","n":2}')
        if [x.get("id") for x in _cp1.read_jsonl_tail(_p, 2)] != ["a", "b"]:
            failures.append("P1 no-trailing-newline: last record dropped")

        # (2) trailing newline (window ends ON a newline) — no phantom empty record.
        _wb(_p, b'{"id":"a"}\n{"id":"b"}\n')
        if [x.get("id") for x in _cp1.read_jsonl_tail(_p, 5)] != ["a", "b"]:
            failures.append("P1 trailing-newline: miscounted")

        # (3) single line at BOF, no newline — the leading line at pos==0 is complete.
        _wb(_p, b'{"id":"solo"}')
        if [x.get("id") for x in _cp1.read_jsonl_tail(_p, 3)] != ["solo"]:
            failures.append("P1 single-line-BOF: not returned")

        # (4) n > count — clamp to all available, newest-last order preserved.
        _wb(_p, b'{"id":"1"}\n{"id":"2"}\n{"id":"3"}\n')
        if [x.get("id") for x in _cp1.read_jsonl_tail(_p, 99)] != ["1", "2", "3"]:
            failures.append("P1 n>count clamp wrong")

        # (5)+(6) records AND a multibyte UTF-8 char straddling back-chunks: force a tiny
        #         chunk_size so the 3-byte → and record boundaries fall mid-chunk and must be
        #         rejoined before decode (else replacement chars / dropped records).
        _recs = [{"id": f"{i:02d}", "msg": "cafe→unicode"} for i in range(20)]
        _wb(_p, ("\n".join(json.dumps(r, ensure_ascii=False) for r in _recs) + "\n").encode("utf-8"))
        _r = _cp1.read_jsonl_tail(_p, 6, chunk_size=4)
        if [x.get("id") for x in _r] != ["14", "15", "16", "17", "18", "19"]:
            failures.append(f"P1 chunk-straddle ids wrong: {[x.get('id') for x in _r]}")
        if any(x.get("msg") != "cafe→unicode" for x in _r):
            failures.append("P1 multibyte-straddle: a record decoded with replacement chars")

        # (7) CRLF line endings — .strip() drops the \r; clean parse (matches the line readers).
        _wb(_p, b'{"id":"x"}\r\n{"id":"y"}\r\n')
        if [x.get("id") for x in _cp1.read_jsonl_tail(_p, 2)] != ["x", "y"]:
            failures.append("P1 CRLF not stripped")

        # (8) garbage / blank lines skipped — 'newest N' is N PARSEABLE records, not raw lines.
        _wb(_p, b'{"id":"g1"}\n\nnot json\n{"id":"g2"}\n   \n{"id":"g3"}\n')
        if [x.get("id") for x in _cp1.read_jsonl_tail(_p, 2)] != ["g2", "g3"]:
            failures.append("P1 garbage/blank lines not skipped to N parseable")

        # (9) byte-identical to the full read's tail across realistic data x chunk sizes.
        _troot = tempfile.mkdtemp(prefix="tc-wp7-p1bi-")
        for i in range(50):
            _cp1.append_transcript(_troot, None, {"id": f"2026-02-01T00:00:00.{i:06d}", "message": f"m{i}"})
        _path = _cp1.get_transcript_file(_troot, None)
        _whole = _cp1.read_transcript(_troot, None, limit=None)  # limit=None bypasses the fast path
        for _n in (1, 7, 50, 200):
            for _cs in (8, 64, 8192):
                _tail = _cp1.read_jsonl_tail(_path, _n, chunk_size=_cs)
                _want = _whole[-_n:] if _n <= len(_whole) else _whole
                if [r.get("id") for r in _tail] != [r.get("id") for r in _want]:
                    failures.append(f"P1 NOT byte-identical to full read: n={_n} cs={_cs}")
                    break

        # (10) since filter — only ids >= the cursor survive.
        _r = _cp1.read_jsonl_tail(_path, 100, since="2026-02-01T00:00:00.000045")
        if [r.get("id") for r in _r] != [f"2026-02-01T00:00:00.{i:06d}" for i in range(45, 50)]:
            failures.append(f"P1 since filter wrong: {[r.get('id') for r in _r]}")

        # ---- _scan_transcript_for_id: in-window hit, beyond-window full-scan fallback, true miss.
        if (_tp1._scan_transcript_for_id(_troot, None, "2026-02-01T00:00:00.000049") or {}).get("message") != "m49":
            failures.append("P1 _scan: recent (in-window) id did not resolve")
        _save = _tp1._RESOLVE_TAIL
        try:
            _tp1._RESOLVE_TAIL = 5  # shrink so id 0 predates the tail → must full-scan
            if (_tp1._scan_transcript_for_id(_troot, None, "2026-02-01T00:00:00.000000") or {}).get("message") != "m0":
                failures.append("P1 _scan: older-than-window id did not resolve via full-scan fallback")
            if _tp1._scan_transcript_for_id(_troot, None, "no-such-id") is not None:
                failures.append("P1 _scan: a true miss past a saturated window did not return None")
        finally:
            _tp1._RESOLVE_TAIL = _save
    except Exception as e:
        failures.append(f"WP-7 P1 seek-tail unit checks errored: {e}")

    # ── WP-7 P2 (C-2) — deletions.jsonl compaction: fold tail-evicted events into a target-keyed
    #    set-file so a FRESH dashboard load (baseline-set UNION the FULL live jsonl) reflects EVERY
    #    deletion ever — a tombstone can't reappear once it ages past the jsonl tail. ──
    try:
        import http.client as _hc2
        from urllib.parse import quote as _q2

        from teammate_comms import comms as _cp2
        from teammate_comms import dashboard as _dash2

        _save_retain, _save_bytes = _cp2.DELETIONS_RETAIN, _cp2.DELETIONS_COMPACT_BYTES
        try:
            _cp2.DELETIONS_RETAIN = 3          # keep only the newest 3 events in the live jsonl
            _cp2.DELETIONS_COMPACT_BYTES = 1   # trip the gate on every append (any line > 1 byte)
            p2root = tempfile.mkdtemp(prefix="tc-wp7-p2-")
            # 8 message-deletes; RETAIN=3 → the oldest 5 fold into the set-file, jsonl keeps 3.
            for i in range(8):
                _cp2.append_deletion(p2root, None,
                                     {"target": f"msg-{i}", "kind": "message", "by": "op", "op": "delete"})
            _djsonl = _cp2.get_deletions_file(p2root, None)
            _dset = _cp2.read_deletions_set(p2root, None)
            _live_targets = [e["target"] for e in _cp2.read_deletions(p2root, None, limit=None)]
            # (a) jsonl trimmed to exactly the newest RETAIN (file order); (b) the rest are folded.
            if _live_targets != ["msg-5", "msg-6", "msg-7"]:
                failures.append(f"P2 jsonl not trimmed to newest RETAIN: {_live_targets}")
            if set(_dset) != {"msg-0", "msg-1", "msg-2", "msg-3", "msg-4"}:
                failures.append(f"P2 set-file missing folded targets: {sorted(_dset)}")
            # (c) COMPLETENESS / C-2 close: baseline UNION full jsonl = ALL 8 incl the oldest.
            if (set(_dset) | set(_live_targets)) != {f"msg-{i}" for i in range(8)}:
                failures.append("P2 fresh-load union is not the complete deleted-set")
            # (d) idempotent: re-compacting (lock held) loses nothing and dups nothing.
            with _cp2.file_lock(_djsonl, timeout=10):
                _cp2._compact_deletions_locked(p2root, None)
            _live2 = [e["target"] for e in _cp2.read_deletions(p2root, None, limit=None)]
            if len(_live2) != 3 or (set(_cp2.read_deletions_set(p2root, None)) | set(_live2)) != {f"msg-{i}" for i in range(8)}:
                failures.append(f"P2 re-compaction not idempotent: live={_live2}")
            # (e) THE GAP CASE — un-compacted oversized steady state (RETAIN < count, gate OFF):
            #     the fresh read is FULL-FILE, so the oldest is STILL present (the bug a bounded
            #     newest-1000 tail read would have reintroduced).
            _cp2.DELETIONS_COMPACT_BYTES = 10**9        # never trip
            gaproot = tempfile.mkdtemp(prefix="tc-wp7-p2gap-")
            for i in range(6):                          # 6 > RETAIN(3) but NO compaction fires
                _cp2.append_deletion(gaproot, None,
                                     {"target": f"g-{i}", "kind": "message", "by": "op", "op": "delete"})
            if _cp2.read_deletions_set(gaproot, None) != {}:
                failures.append("P2 gap-case: compaction fired with the gate disabled")
            if {e["target"] for e in _cp2.read_deletions(gaproot, None, limit=None)} != {f"g-{i}" for i in range(6)}:
                failures.append("P2 gap-case: full-file fresh read dropped the oldest un-folded event")
            _cp2.DELETIONS_COMPACT_BYTES = 1            # re-arm
            # (f) block=False (whole-group/teammate delete) never raises, records the event, and
            #     compaction still runs under the optional lock.
            bfroot = tempfile.mkdtemp(prefix="tc-wp7-p2bf-")
            for i in range(8):
                _cp2.append_deletion(bfroot, None,
                                     {"target": f"grp-{i}", "kind": "group", "by": "op", "op": "delete"},
                                     block=False)
            _bf_union = set(_cp2.read_deletions_set(bfroot, None)) | {
                e["target"] for e in _cp2.read_deletions(bfroot, None, limit=None)}
            if _bf_union != {f"grp-{i}" for i in range(8)}:
                failures.append(f"P2 block=False path lost a deletion: {sorted(_bf_union)}")
            # (g) corrupt set-file → read_deletions_set coerces to {} (no crash; fresh load still works).
            with open(_cp2.get_deletions_set_file(bfroot, None), "w", encoding="utf-8") as _f:
                _f.write("{ this is not json")
            if _cp2.read_deletions_set(bfroot, None) != {}:
                failures.append("P2 corrupt set-file did not coerce to {}")

            # (h) _api_poll fresh-vs-cursored over a REAL loopback server (ephemeral port) — pins the
            #     C-2 close AND the documented cursored-lag self-heal. try/finally shutdown.
            _dres2 = _dash2.start_dashboard(p2root, None, "Operator", port=0, open_browser=False)
            _dport2, _dtok2 = _dres2["port"], _dash2._STATE.token
            try:
                def _poll(dc):
                    c = _hc2.HTTPConnection("127.0.0.1", _dport2, timeout=5)
                    c.putrequest("GET", f"/api/poll?cursor=&rcursor=&dcursor={_q2(dc)}",
                                 skip_host=True, skip_accept_encoding=True)
                    c.putheader("Host", "127.0.0.1")
                    c.putheader("X-Dashboard-Token", _dtok2)
                    c.endheaders()
                    r = c.getresponse()
                    d = json.loads(r.read())
                    c.close()
                    return d
                _pf = _poll("")                                     # FRESH load
                if {e["target"] for e in _pf.get("deletions", [])} != {f"msg-{i}" for i in range(8)}:
                    failures.append("P2 _api_poll FRESH load did not return the complete deleted-set")
                # CAUGHT-UP cursored poll: NO baseline (folded msg-0..4 absent); only live tail ids.
                _pc = {e["target"] for e in _poll(_pf.get("dcursor", "")).get("deletions", [])}
                if _pc & {"msg-0", "msg-1", "msg-2", "msg-3", "msg-4"} or not _pc <= {"msg-5", "msg-6", "msg-7"}:
                    failures.append(f"P2 cursored poll leaked the baseline or non-tail ids: {sorted(_pc)}")
                # LAGGED cursor (older than the jsonl floor) — the RESCUE (wire-the-fallback CR):
                # the compacted-away tombstones must ARRIVE IN THIS poll, NOT after a reload. A
                # message deleted while the tab was suspended can't silently render undeleted.
                _pl = {e["target"] for e in _poll("0000").get("deletions", [])}
                if _pl != {f"msg-{i}" for i in range(8)}:
                    failures.append(f"P2 lagged cursored did NOT rescue the folded tombstones in-poll: {sorted(_pl)}")
            finally:
                _dash2.shutdown_dashboard()
        finally:
            _cp2.DELETIONS_RETAIN, _cp2.DELETIONS_COMPACT_BYTES = _save_retain, _save_bytes
    except Exception as e:
        failures.append(f"WP-7 P2 deletions-compaction unit checks errored: {e}")

    # ── WP-7 P3 (firehose/N-1) — transcript BYTE cursor: read_transcript_after streams only the
    #    bytes appended since the last poll (no O(file) re-scan), with offset|generation validity
    #    and a transparent re-tail on recreation. BYTE-EXACT new_offset is the load-bearing detail. ──
    try:
        import http.client as _hc3
        from urllib.parse import quote as _q3

        from teammate_comms import comms as _cp3
        from teammate_comms import dashboard as _dash3

        _td = tempfile.mkdtemp(prefix="tc-wp7-p3-")
        _tp = os.path.join(_td, "transcript.jsonl")

        def _wb3(data, mode="wb"):
            with open(_tp, mode) as _f:
                _f.write(data)

        # (1) missing file → reset, empty.
        _r = _cp3.read_transcript_after(os.path.join(_td, "nope.jsonl"), 0, "", 200)
        if _r != ([], 0, "", True):
            failures.append(f"P3 missing file: {_r}")

        # (2) BYTE-EXACT new_offset under \n + CRLF + blank + garbage + multibyte (the centerpiece).
        _img = (b'{"id":"a","m":"x"}' + b"\n"
                + '{"id":"b","m":"café→"}'.encode("utf-8") + b"\r\n"   # multibyte body + CRLF
                + b"\n"                                                          # blank line
                + b"not json at all" + b"\n"                                     # garbage line
                + b'{"id":"c","m":"y"}' + b"\n")
        _wb3(_img)
        # First read with the fresh sentinel ("") forces a transparent RE-TAIL (not an increment).
        recs, noff, ngen, rst = _cp3.read_transcript_after(_tp, 0, "", 200)
        if not rst:
            failures.append("P3 empty-gen first read did not reset (re-tail)")
        if [r.get("id") for r in recs] != ["a", "b", "c"]:
            failures.append(f"P3 re-tail dropped/added records (blank/garbage skip?): {[r.get('id') for r in recs]}")
        if noff != len(_img):
            failures.append(f"P3 re-tail offset != file size: {noff} vs {len(_img)}")
        # Valid INCREMENT from 0 with the correct generation → byte-exact advance to EOF.
        _gen = ngen
        recs2, noff2, ngen2, rst2 = _cp3.read_transcript_after(_tp, 0, _gen, 200)
        if rst2 or [r.get("id") for r in recs2] != ["a", "b", "c"] or ngen2 != _gen:
            failures.append(f"P3 valid increment wrong: reset={rst2} ids={[r.get('id') for r in recs2]}")
        if noff2 != len(_img):
            failures.append(f"P3 BYTE-EXACT new_offset != raw byte length: {noff2} vs {len(_img)}")
        if recs2[1].get("m") != "café→":
            failures.append(f"P3 multibyte body corrupted: {recs2[1].get('m')!r}")

        # (3) no new bytes (offset==size) → [] and offset unchanged (the cheap steady-state path).
        _r3 = _cp3.read_transcript_after(_tp, len(_img), _gen, 200)
        if _r3 != ([], len(_img), _gen, False):
            failures.append(f"P3 no-new-bytes not a clean no-op: {_r3}")

        # (4) TORN tail: a complete line + a partial (no \n). Only the complete one is consumed;
        #     offset stops BEFORE the partial; completing the partial yields it on the next read.
        _comp = b'{"id":"d","m":"z"}'
        _wb3(_comp + b"\n" + b'{"id":"e","m":"incompl', mode="ab")
        recs4, noff4, _, _ = _cp3.read_transcript_after(_tp, len(_img), _gen, 200)
        if [r.get("id") for r in recs4] != ["d"]:
            failures.append(f"P3 torn-tail consumed a partial line: {[r.get('id') for r in recs4]}")
        if noff4 != len(_img) + len(_comp) + 1:
            failures.append(f"P3 torn-tail offset crossed the partial: {noff4} vs {len(_img)+len(_comp)+1}")
        _wb3(b'ete"}\n', mode="ab")                                    # complete the partial line
        recs4b, _, _, _ = _cp3.read_transcript_after(_tp, noff4, _gen, 200)
        if [r.get("id") for r in recs4b] != ["e"]:
            failures.append(f"P3 completed line not returned next read: {[r.get('id') for r in recs4b]}")

        # (4b) divergence-free: a RESET (re-tail, gen="") and a from-zero INCREMENT return the SAME
        #      records (blank/garbage skipping must agree, or the re-tail dedup would leak/double-count).
        _reset_ids = [r.get("id") for r in _cp3.read_transcript_after(_tp, 0, "", 200)[0]]
        _incr_ids = [r.get("id") for r in _cp3.read_transcript_after(_tp, 0, _gen, 200)[0]]
        if _reset_ids != _incr_ids:
            failures.append(f"P3 reset vs increment record divergence: {_reset_ids} != {_incr_ids}")

        # (5) offset > size → reset; (6) generation mismatch → reset; (6b) NEGATIVE offset (a
        #     hand-crafted cursor) must re-tail cleanly, never wedge the stream on seek().
        if not _cp3.read_transcript_after(_tp, 10**9, _gen, 200)[3]:
            failures.append("P3 offset>size did not reset")
        if not _cp3.read_transcript_after(_tp, 5, "deadbeef", 200)[3]:
            failures.append("P3 generation mismatch did not reset")
        _neg = _cp3.read_transcript_after(_tp, -5, _gen, 200)
        if not _neg[3] or _neg[1] < 0:
            failures.append(f"P3 negative offset did not reset (livelock risk): {_neg[:1]} reset={_neg[3]}")

        # (6c) N-1 FIX: a record teed OUT OF ID ORDER (a lower id appended AFTER a higher one) is
        #      byte-streamed in FILE order — the old id cursor (id>=since) would have skipped it.
        _ooo = b'{"id":"2026-01-01T00:00:05.000000","m":"late-high"}\n' \
               b'{"id":"2026-01-01T00:00:01.000000","m":"earlier-low"}\n'   # lower id appended LAST
        _wb3(_ooo)
        _ogen = _cp3._transcript_generation(_tp, len(_ooo))
        _oids = [r.get("id") for r in _cp3.read_transcript_after(_tp, 0, _ogen, 200)[0]]
        if _oids != ["2026-01-01T00:00:05.000000", "2026-01-01T00:00:01.000000"]:
            failures.append(f"P3 N-1: out-of-order tee not byte-streamed in file order: {_oids}")

        # (7) burst > limit: 10 lines, limit=3 → first 3 records, offset BYTE-EXACT at the 3rd \n;
        #     the rest page out next call (A-1 parity) with byte-exact continuity across the page.
        _lines = [('{"id":"n%d"}' % i).encode("utf-8") for i in range(10)]
        _bimg = b"".join(l + b"\n" for l in _lines)
        _wb3(_bimg)
        _bgen = _cp3._transcript_generation(_tp, len(_bimg))
        recb, noffb, _, _ = _cp3.read_transcript_after(_tp, 0, _bgen, 3)
        _exp_off = sum(len(_lines[i]) + 1 for i in range(3))          # bytes through the 3rd \n
        if [r.get("id") for r in recb] != ["n0", "n1", "n2"] or noffb != _exp_off:
            failures.append(f"P3 burst cap page1 wrong: ids={[r.get('id') for r in recb]} off={noffb} vs {_exp_off}")
        recb2, _, _, _ = _cp3.read_transcript_after(_tp, noffb, _bgen, 3)
        if [r.get("id") for r in recb2] != ["n3", "n4", "n5"]:
            failures.append(f"P3 burst cap page2 (paging continuity) wrong: {[r.get('id') for r in recb2]}")

        # (7b) burst cap with INTERLEAVED blank/garbage at the boundary: the cap counts RECORDS, the
        #      offset counts RAW bytes — junk between the limit-th record and the next is left for
        #      page 2 (offset stops at the limit-th record's \n), and page 2 resumes byte-continuous.
        _jimg = (b'{"id":"j0"}\n' + b'{"id":"j1"}\n' + b"\n" + b"junk\n"   # 2 recs, then blank+garbage
                 + b'{"id":"j2"}\n' + b'{"id":"j3"}\n')
        _wb3(_jimg)
        _jgen = _cp3._transcript_generation(_tp, len(_jimg))
        recj, noffj, _, _ = _cp3.read_transcript_after(_tp, 0, _jgen, 2)        # cap after j1
        _jexp = len(b'{"id":"j0"}\n') + len(b'{"id":"j1"}\n')                   # stops at j1's \n, junk deferred
        if [r.get("id") for r in recj] != ["j0", "j1"] or noffj != _jexp:
            failures.append(f"P3 burst+junk page1 wrong: ids={[r.get('id') for r in recj]} off={noffj} vs {_jexp}")
        recj2, _, _, _ = _cp3.read_transcript_after(_tp, noffj, _jgen, 2)       # page 2 skips the junk
        if [r.get("id") for r in recj2] != ["j2", "j3"]:
            failures.append(f"P3 burst+junk page2 did not skip interleaved junk: {[r.get('id') for r in recj2]}")

        # (8) transcript_tail_and_cursor: stat-then-tail mint — offset == file size, gen set, full tail.
        _wb3(_bimg)                                                    # re-establish a known 10-line image
        _trecs, _toff, _tgen = _cp3.transcript_tail_and_cursor(_tp, 200)
        if _toff != len(_bimg) or _tgen != _bgen or [r.get("id") for r in _trecs] != [f"n{i}" for i in range(10)]:
            failures.append(f"P3 tail_and_cursor mint wrong: off={_toff} gen={_tgen}")

        # (9) real loopback _api_poll: fresh mints a byte cursor; append → cursored returns ONLY the
        #     new record + advances; caught-up → empty; recreate (new first line) → reset re-tails.
        proot3 = tempfile.mkdtemp(prefix="tc-wp7-p3api-")
        _tf3 = _cp3.get_transcript_file(proot3, None)
        _tf3.parent.mkdir(parents=True, exist_ok=True)
        with open(_tf3, "w", encoding="utf-8") as _f:                 # write the firehose bytes directly
            for i in range(2):
                _f.write(json.dumps({"id": f"r{i}", "from": "a", "message": f"m{i}", "kind": "dm"}) + "\n")
        _dres3 = _dash3.start_dashboard(proot3, None, "Operator", port=0, open_browser=False)
        _dport3, _dtok3 = _dres3["port"], _dash3._STATE.token
        try:
            def _poll3(cur):
                c = _hc3.HTTPConnection("127.0.0.1", _dport3, timeout=5)
                c.putrequest("GET", f"/api/poll?cursor={_q3(cur)}&rcursor=&dcursor=",
                             skip_host=True, skip_accept_encoding=True)
                c.putheader("Host", "127.0.0.1")
                c.putheader("X-Dashboard-Token", _dtok3)
                c.endheaders()
                r = c.getresponse()
                d = json.loads(r.read())
                c.close()
                return d
            _pf3 = _poll3("")                                         # fresh → tail + minted byte cursor
            if [r.get("id") for r in _pf3.get("records", [])] != ["r0", "r1"]:
                failures.append(f"P3 _api_poll fresh records wrong: {_pf3.get('records')}")
            _c3 = _pf3.get("cursor", "")
            if "|" not in _c3:
                failures.append(f"P3 _api_poll fresh cursor is not a byte cursor: {_c3!r}")
            with open(_tf3, "a", encoding="utf-8") as _f:             # one new record appended
                _f.write(json.dumps({"id": "r2", "from": "a", "message": "m2", "kind": "dm"}) + "\n")
            _pc3 = _poll3(_c3)
            if [r.get("id") for r in _pc3.get("records", [])] != ["r2"]:
                failures.append(f"P3 _api_poll cursored did not stream ONLY the new record: {_pc3.get('records')}")
            if _pc3.get("cursor") == _c3:
                failures.append("P3 _api_poll cursored did not advance the byte cursor")
            if _poll3(_pc3.get("cursor")).get("records"):
                failures.append("P3 _api_poll caught-up unexpectedly returned records")
            with open(_tf3, "w", encoding="utf-8") as _f:             # RECREATE with a different first line
                _f.write(json.dumps({"id": "z0", "from": "b", "message": "new", "kind": "dm"}) + "\n")
            if [r.get("id") for r in _poll3(_pc3.get("cursor")).get("records", [])] != ["z0"]:
                failures.append("P3 _api_poll recreation did not reset + re-tail")
        finally:
            _dash3.shutdown_dashboard()
    except Exception as e:
        failures.append(f"WP-7 P3 byte-cursor unit checks errored: {e}")

    # ── WP-8 P1 (F-2/F-4/F-5) — identity/registration UX: unregistered-recipient warnings (warn,
    #    never error — open membership is intentional); two-component parent/name project; reincarnate
    #    spawned_by provenance (set unconditionally, never inherited; survives the heartbeat merge). ──
    try:
        from teammate_comms import comms as _c8
        from teammate_comms import server as _s8
        from teammate_comms import spawn as _sp8
        from teammate_comms import tools as _t8

        class _Id8:
            def __init__(self, agent, root):
                self.agent, self.root = agent, root

            def snapshot(self):
                return (self.agent, None, self.root, None)

        # ---- F-2: unregistered DM recipient → DELIVERED (open membership) + a queued/typo NOTE,
        #      NOT an error. A registered recipient gets the channel-status branch, no F-2 note.
        f2root = tempfile.mkdtemp(prefix="tc-wp8-f2-")
        _ctx8 = {"identity": _Id8("alice", f2root)}
        _o1 = _t8._handle_send({"to": "ghost", "message": "hi"}, _ctx8)
        if "no agent record" not in _o1:
            failures.append(f"F-2: DM to an unregistered name missing the queued NOTE: {_o1!r}")
        _gi = _c8.read_json_safe(_c8.get_inboxes_dir(f2root, None) / "ghost_unread.json")
        if not any(m.get("message") == "hi" for m in _gi):      # delivery preserved (open membership)
            failures.append("F-2: a message to an unregistered name was NOT delivered (should queue)")
        _c8.write_agent_record(f2root, None, "realbob", type="full", channel=False)
        if "no agent record" in _t8._handle_send({"to": "realbob", "message": "yo"}, _ctx8):
            failures.append("F-2: a REGISTERED recipient wrongly got the unregistered NOTE")
        _o3 = _t8._handle_group({"action": "create", "group": "g8", "members": ["ghost2"]}, _ctx8)
        if "no agent record" not in _o3 or "Created group" not in _o3:
            failures.append(f"F-2: group-create unregistered-member NOTE/creation wrong: {_o3!r}")

        # ---- F-4: two-component parent/name; bare-name fallback when there's no parent; deep-path
        #      truncate-BEFORE-validate so a long path can't raise out of validate and break register.
        if _s8._project_label("/home/me/api") != "me/api":
            failures.append(f"F-4: two-component label wrong: {_s8._project_label('/home/me/api')!r}")
        if _s8._project_label("solo") != "solo":                # no usable parent → bare name, no leading '/'
            failures.append(f"F-4: bare-name fallback wrong: {_s8._project_label('solo')!r}")
        _lab = _s8._project_label("/" + "x" * 200 + "/proj")    # parent 200 chars → must truncate
        if len(_lab) > _c8.PROFILE_FIELDS["project"]:
            failures.append(f"F-4: deep-path label exceeds the project cap ({len(_lab)})")
        _c8.validate_profile_field("project", _lab)             # must NOT raise (truncate-before-validate)

        # ---- F-5: spawned_by — set UNCONDITIONALLY in build_child_env, NEVER inherited; stored on
        #      the register record; SURVIVES a heartbeat merge.
        if _sp8.build_child_env({}, "child", "/p", spawned_by="parent").get("TEAMMATE_SPAWNED_BY") != "parent":
            failures.append("F-5: build_child_env did not set TEAMMATE_SPAWNED_BY")
        _gc = _sp8.build_child_env({"TEAMMATE_SPAWNED_BY": "grandparent"}, "gc", "/p", spawned_by="parent2")
        if _gc.get("TEAMMATE_SPAWNED_BY") != "parent2":         # grand-child carries its IMMEDIATE parent
            failures.append(f"F-5: grand-child inherited a stale spawned_by: {_gc.get('TEAMMATE_SPAWNED_BY')!r}")
        if "TEAMMATE_SPAWNED_BY" in _sp8.build_child_env({"TEAMMATE_SPAWNED_BY": "stale"}, "c", "/p"):
            failures.append("F-5: a stale spawned_by was inherited when none was passed (never-inherit)")
        f5root = tempfile.mkdtemp(prefix="tc-wp8-f5-")
        _pre_id, _pre_reg = _s8._identity.snapshot(), _s8._registered.is_set()
        _pre_sb = os.environ.get("TEAMMATE_SPAWNED_BY")
        try:
            os.environ["TEAMMATE_SPAWNED_BY"] = "lead"
            _s8.register_identity("childagent", None, f5root)
            _root5, _ = _c8.resolve_comms_root(f5root)
            _rec = _c8.read_agent_record(_root5, None, "childagent")
            if not _rec or _rec.get("spawned_by") != "lead":
                failures.append(f"F-5: spawned_by not stored on the register record: {_rec}")
            # heartbeat-style write (channel/lastHeartbeat only) must PRESERVE spawned_by (field merge)
            _c8.write_agent_record(_root5, None, "childagent", channel=True, lastHeartbeat=_c8.now_timestamp())
            if _c8.read_agent_record(_root5, None, "childagent").get("spawned_by") != "lead":
                failures.append("F-5: spawned_by did NOT survive a heartbeat merge")
        finally:
            if _pre_sb is None:
                os.environ.pop("TEAMMATE_SPAWNED_BY", None)
            else:
                os.environ["TEAMMATE_SPAWNED_BY"] = _pre_sb
            _s8._identity.set(*_pre_id)                          # restore global identity
            _s8._registered.set() if _pre_reg else _s8._registered.clear()
    except Exception as e:
        failures.append(f"WP-8 P1 identity-UX unit checks errored: {e}")

    # ── WP-8 P2 (F-3) — inbox reaction display unified on group-history's NAMES form (the inbox
    #    used to show bare counts '👍 2'; the wake said someone reacted, the inbox couldn't say who). ──
    try:
        from teammate_comms import tools as _t83

        class _IdRx:
            def __init__(self, agent, root):
                self.agent, self.root, self._seen = agent, root, None

            def snapshot(self):
                return (self.agent, None, self.root, None)

            def set_last_seen(self, ids):
                self._seen = set(ids)

            def get_last_seen(self):
                return self._seen

        _g = _t83._REACTIONS
        # (a) the SHARED helper's exact form: reactor names, ', ' within an emoji, '; ' between emojis.
        if _t83._reaction_summary({"thumbsup": ["alice", "bob"]}) != f"{_g['thumbsup']} alice, bob":
            failures.append("F-3: single-emoji names form wrong")
        if _t83._reaction_summary({"thumbsup": ["alice"], "fire": ["bob"]}) != f"{_g['thumbsup']} alice; {_g['fire']} bob":
            failures.append("F-3: multi-emoji separator form wrong")
        # (b) end-to-end: the INBOX now renders reactor NAMES (matching history), not a count.
        f3root = tempfile.mkdtemp(prefix="tc-wp8-f3-")
        _res = _t83.send_dm(f3root, None, "alice", "bob", "hello")
        _t83.react(f3root, None, "carol", _res["id"], "thumbsup")
        _outx = _t83._handle_inbox({}, {"identity": _IdRx("bob", f3root)})
        if f"reactions: {_g['thumbsup']} carol" not in _outx:
            failures.append(f"F-3: inbox did not render reactor NAMES: {_outx!r}")
    except Exception as e:
        failures.append(f"WP-8 P2 inbox-reaction-names unit checks errored: {e}")

    # ── WP-8 P3 (G-2/G-6) — coverage for branches that have bitten this codebase's failure classes:
    #    resolve_comms_root fallbacks, _maybe_auto_register (the TEAMMATE_AGENT path the main scenario
    #    pops), read_json_safe corrupt-reset, group join/leave/members, the spawn launcher SEAM (mocked
    #    Popen, no real spawn), and the dashboard static asset is actually packaged (G-6). ──
    try:
        from teammate_comms import comms as _c9
        from teammate_comms import server as _s9
        from teammate_comms import spawn as _sp9
        from teammate_comms import tools as _t9

        class _Id9:
            def __init__(self, agent, root):
                self.agent, self.root = agent, root

            def snapshot(self):
                return (self.agent, None, self.root, None)

        # ---- G-2: resolve_comms_root — all four ordered branches (explicit > env > config > default).
        _se = {k: os.environ.get(k) for k in ("TEAMMATE_COMMS_DIR", "CLAUDE_CONFIG_DIR")}
        try:
            if _c9.resolve_comms_root("/explicit")[1] != "comms_dir arg":
                failures.append("G-2 resolve_comms_root: explicit comms_dir branch wrong")
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            os.environ["TEAMMATE_COMMS_DIR"] = "/env-comms"
            if _c9.resolve_comms_root(None)[1] != "TEAMMATE_COMMS_DIR":
                failures.append("G-2 resolve_comms_root: TEAMMATE_COMMS_DIR branch wrong")
            os.environ.pop("TEAMMATE_COMMS_DIR", None)
            os.environ["CLAUDE_CONFIG_DIR"] = "/cfg"
            if _c9.resolve_comms_root(None)[1] != "CLAUDE_CONFIG_DIR":
                failures.append("G-2 resolve_comms_root: CLAUDE_CONFIG_DIR branch wrong")
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            if _c9.resolve_comms_root(None)[1] != "~/.claude default":
                failures.append("G-2 resolve_comms_root: default branch wrong")
        finally:
            for _k, _v in _se.items():
                os.environ.pop(_k, None) if _v is None else os.environ.__setitem__(_k, _v)

        # ---- G-2: _maybe_auto_register (the TEAMMATE_AGENT env path) — exercised WITH F-4 (project)
        #      and F-5 (spawned_by), which all flow through register, so the record proves all three.
        ar_root = tempfile.mkdtemp(prefix="tc-wp8-ar-")
        _pre_id9, _pre_reg9 = _s9._identity.snapshot(), _s9._registered.is_set()
        _pre_e9 = {k: os.environ.get(k) for k in
                   ("TEAMMATE_AGENT", "TEAMMATE_COMMS_DIR", "CLAUDE_PROJECT_DIR", "TEAMMATE_SPAWNED_BY")}
        try:
            os.environ.update({"TEAMMATE_AGENT": "autobot", "TEAMMATE_COMMS_DIR": ar_root,
                               "CLAUDE_PROJECT_DIR": "/work/myrepo", "TEAMMATE_SPAWNED_BY": "boss"})
            _s9._maybe_auto_register()
            _r9, _ = _c9.resolve_comms_root(ar_root)
            _arec = _c9.read_agent_record(_r9, None, "autobot")
            if not _arec or _arec.get("type") != "full":
                failures.append(f"G-2 _maybe_auto_register: no full record written: {_arec}")
            elif _arec.get("project") != "work/myrepo" or _arec.get("spawned_by") != "boss":
                failures.append(f"G-2 auto-register did not flow F-4/F-5 through: {_arec}")
        finally:
            for _k, _v in _pre_e9.items():
                os.environ.pop(_k, None) if _v is None else os.environ.__setitem__(_k, _v)
            _s9._identity.set(*_pre_id9)
            _s9._registered.set() if _pre_reg9 else _s9._registered.clear()

        # ---- G-2: read_json_safe resets a CORRUPT file to [] on disk (and returns []).
        _cf = Path(tempfile.mkdtemp(prefix="tc-wp8-rjs-")) / "corrupt.json"
        _cf.write_text("{ this is not json", encoding="utf-8")
        if _c9.read_json_safe(_cf) != [] or json.loads(_cf.read_text(encoding="utf-8")) != []:
            failures.append("G-2 read_json_safe did not reset a corrupt file to []")

        # ---- G-2: group join / members / leave as MCP-dispatched actions (only create+add were tested).
        groot = tempfile.mkdtemp(prefix="tc-wp8-grp-")
        _ag, _bg = {"identity": _Id9("alice", groot)}, {"identity": _Id9("bob", groot)}
        _t9._handle_group({"action": "create", "group": "team"}, _ag)
        if "bob" not in _t9._handle_group({"action": "join", "group": "team"}, _bg):
            failures.append("G-2 group join: bob not in members after join")
        _mem = _t9._handle_group({"action": "members", "group": "team"}, _ag)
        if "alice" not in _mem or "bob" not in _mem:
            failures.append(f"G-2 group members: missing alice/bob: {_mem!r}")
        _t9._handle_group({"action": "leave", "group": "team"}, _bg)
        if "bob" in _t9._handle_group({"action": "members", "group": "team"}, _ag):
            failures.append("G-2 group leave: bob still a member after leave")

        # ---- G-2: spawn launcher SEAM — mock subprocess.Popen so the launcher resolution + argv
        #      construction run WITHOUT actually spawning a terminal (the only hermetic way to cover
        #      spawn_in_terminal; the real detached launch stays out of scope, stated in the diff).
        _real_popen = _sp9.subprocess.Popen
        _popen_calls = []
        try:
            class _FakePopen:
                def __init__(self, *a, **k):
                    _popen_calls.append((a, k))
            _sp9.subprocess.Popen = _FakePopen
            _argv = ["claude", "-p", "hi"]
            _launched = _sp9.spawn_in_terminal(_argv, "/tmp", {"X": "1"})
            if not (_popen_calls and _launched and all(a in _launched for a in _argv)):
                failures.append(f"G-2 spawn_in_terminal seam: argv not execd by the launcher: {_launched}")
        finally:
            _sp9.subprocess.Popen = _real_popen

        # ---- G-6: the dashboard static asset must be PACKAGED (the triple-fallback can otherwise
        #      serve a placeholder on a packaging mistake — assert via the packaged-resource API).
        from importlib.resources import files as _ir_files
        _idx = _ir_files("teammate_comms") / "static" / "index.html"
        if not (_idx.is_file() and len(_idx.read_text(encoding="utf-8")) > 500):
            failures.append("G-6: teammate_comms/static/index.html is not packaged (or is a stub)")
    except Exception as e:
        failures.append(f"WP-8 P3 coverage/packaging unit checks errored: {e}")

    # ── WP-8 P5 (G-5) — teammate_whoami(verbose=True) doctor section: read-only diagnostics
    #    (heartbeat freshness — NOT a pid probe; file sizes; unread counts; lock dirs) + spawned_by. ──
    try:
        from teammate_comms import comms as _cD
        from teammate_comms import tools as _tD

        class _IdD:
            def __init__(self, agent, root):
                self.agent, self.root = agent, root

            def snapshot(self):
                return (self.agent, None, self.root, None)

        import socket as _sockD
        droot = tempfile.mkdtemp(prefix="tc-wp8-g5-")
        # Heartbeat-only CONTRACT pin (G-5 CR): a FRESH heartbeat + a DEAD pid on THIS host. The
        # doctor must report alive=True (heartbeat-only, pid_check=False) — a regression to the
        # default pid_check=True would both flip this to False AND spawn a `tasklist` storm.
        _cD.write_agent_record(droot, None, "doc", type="full", channel=True,
                               host=_sockD.gethostname(), pid=999999,
                               lastHeartbeat=_cD.now_timestamp(), spawned_by="boss")
        _tD.send_dm(droot, None, "x", "doc", "hi")          # creates doc's unread inbox (1)
        _dctx = {"identity": _IdD("doc", droot)}
        # plain whoami: NO doctor section, but the F-5 spawned_by breadcrumb IS surfaced.
        _plain = json.loads(_tD._handle_whoami({}, _dctx))
        if "doctor" in _plain:
            failures.append("G-5: plain whoami wrongly included a doctor section")
        if _plain.get("spawned_by") != "boss":
            failures.append(f"G-5: whoami did not surface spawned_by: {_plain.get('spawned_by')}")
        # verbose: a doctor section with the read-only diagnostics keys + correct heartbeat/unread.
        _doc = json.loads(_tD._handle_whoami({"verbose": True}, _dctx)).get("doctor")
        if not isinstance(_doc, dict) or not all(k in _doc for k in
                                                 ("comms_root", "agents", "files", "unread_counts", "lock_dirs")):
            failures.append(f"G-5: doctor section missing keys: {_doc}")
        elif _doc["agents"].get("doc", {}).get("alive") is not True or _doc["unread_counts"].get("doc") != 1:
            failures.append(f"G-5: doctor heartbeat/unread wrong: {_doc.get('agents')} / {_doc.get('unread_counts')}")
        # unregistered + verbose → a doctor note, no crash.
        _un = json.loads(_tD._handle_whoami({"verbose": True}, {"identity": _IdD(None, None)}))
        if _un.get("registered") is not False or "doctor" not in _un:
            failures.append(f"G-5: unregistered verbose whoami wrong: {_un}")
    except Exception as e:
        failures.append(f"WP-8 P5 doctor unit checks errored: {e}")

    # ── WP-15 — durable cross-session inbox body-suppression ──
    # Tests that {agent}_seen.json persists body suppression across simulated new sessions.
    # Hermetic, isolated temp root per sub-test.  A new session is simulated by resetting
    # identity.last_seen to None (the "never read this session" sentinel) while leaving the
    # on-disk seen_file intact — exactly what a real server restart does.
    try:
        from teammate_comms.tools import _handle_inbox as _hi15
        from teammate_comms.tools import _handle_ack as _ha15
        from teammate_comms import comms as _c15

        _b15_agent = "probe15"

        class _Id15:
            def __init__(self, agent, root, team=None):
                self._agent, self._root, self._team = agent, root, team
                self._ls = None
            def snapshot(self):
                return (self._agent, self._team, self._root, None)
            def get_last_seen(self):
                return self._ls
            def set_last_seen(self, v):
                self._ls = v

        # ── T1 — cross-session suppression ──
        _t1_root = tempfile.mkdtemp(prefix="tc-15-t1-")
        _t1_ix = _c15.get_inboxes_dir(_t1_root, None)
        _c15.ensure_inbox(_t1_ix, _b15_agent)
        _msg_a = {"id": "wp15-a", "from": "alice", "priority": "normal", "message": "body-alpha"}
        _msg_b = {"id": "wp15-b", "from": "bob",   "priority": "normal", "message": "body-beta"}
        _c15.write_json_atomic(_t1_ix / f"{_b15_agent}_unread.json", [_msg_a, _msg_b])
        _ctx15 = {"identity": _Id15(_b15_agent, _t1_root)}
        # Session 1: first read — both bodies appear, seen_file is written.
        _s1 = _hi15({}, _ctx15)
        if "body-alpha" not in _s1 or "body-beta" not in _s1:
            failures.append(f"WP-15 T1: first read must show both bodies: {_s1[:120]!r}")
        # Session 2: fresh Identity (last_seen=None by construction) + same inboxes dir.
        # seen_file persists at {agent}_seen.json — suppression must come from it alone.
        _ctx15["identity"] = _Id15(_b15_agent, _t1_root)
        _s2 = _hi15({}, _ctx15)
        if "body-alpha" in _s2 or "body-beta" in _s2:
            failures.append(f"WP-15 T1: cross-session re-read re-dumped suppressed bodies: {_s2[:120]!r}")
        if "already delivered" not in _s2 and "No new messages" not in _s2:
            failures.append(f"WP-15 T1: count line absent from suppressed output: {_s2[:120]!r}")
        if "this session" in _s2:
            failures.append(f"WP-15 T1: count line still claims 'this session' for prior-session msg: {_s2[:120]!r}")

        # ── T2 — NEVER-MISS holds (new arrival after new-session reset renders full) ──
        _msg_c = {"id": "wp15-c", "from": "carol", "priority": "normal", "message": "body-new"}
        _c15.write_json_atomic(_t1_ix / f"{_b15_agent}_unread.json", [_msg_a, _msg_b, _msg_c])
        _ctx15["identity"] = _Id15(_b15_agent, _t1_root)
        _s3 = _hi15({}, _ctx15)
        if "body-new" not in _s3:
            failures.append(f"WP-15 T2: NEVER-MISS failed — new body absent after new session: {_s3[:120]!r}")
        if "body-alpha" in _s3 or "body-beta" in _s3:
            failures.append(f"WP-15 T2: prior-session bodies leaked in same read as NEVER-MISS: {_s3[:120]!r}")

        # ── T3 — ack("all") startup-drain drains BOTH msg-old and msg-new ──
        # The sentinel is last_seen=None, NOT persisted seen_file — so both messages are drained.
        _t3_root = tempfile.mkdtemp(prefix="tc-15-t3-")
        _t3_ix = _c15.get_inboxes_dir(_t3_root, None)
        _c15.ensure_inbox(_t3_ix, _b15_agent)
        _mo = {"id": "t3-old", "from": "alice", "priority": "normal", "message": "old-body"}
        _mn = {"id": "t3-new", "from": "bob",   "priority": "normal", "message": "new-body"}
        _c15.write_json_atomic(_t3_ix / f"{_b15_agent}_unread.json", [_mo, _mn])
        _c15.write_json_atomic(_t3_ix / f"{_b15_agent}_seen.json", ["t3-old"])  # only old pre-seeded
        _ctx15t3 = {"identity": _Id15(_b15_agent, _t3_root)}
        _ack_r = _ha15({"id": "all"}, _ctx15t3)
        if "Acknowledged all 2" not in _ack_r:
            failures.append(f"WP-15 T3: startup ack-all drained wrong count (want 2 incl. msg-new): {_ack_r!r}")
        _t3_left = _c15.read_json_safe(_t3_ix / f"{_b15_agent}_unread.json")
        if _t3_left:
            failures.append(f"WP-15 T3: inbox not empty after startup ack-all: {_t3_left}")

        # ── T4 — load-time prune: stale id absent from output AND from seen_file after read ──
        _t4_root = tempfile.mkdtemp(prefix="tc-15-t4-")
        _t4_ix = _c15.get_inboxes_dir(_t4_root, None)
        _c15.ensure_inbox(_t4_ix, _b15_agent)
        _ml = {"id": "t4-live", "from": "alice", "priority": "normal", "message": "live-body"}
        _c15.write_json_atomic(_t4_ix / f"{_b15_agent}_unread.json", [_ml])
        _c15.write_json_atomic(_t4_ix / f"{_b15_agent}_seen.json", ["stale-ghost", "t4-live"])
        _ctx15t4 = {"identity": _Id15(_b15_agent, _t4_root)}
        _s4 = _hi15({}, _ctx15t4)
        if "stale-ghost" in _s4:
            failures.append(f"WP-15 T4: stale id resurrected in inbox output: {_s4[:120]!r}")
        _t4_sf = _c15.read_json_safe(_t4_ix / f"{_b15_agent}_seen.json")
        if "stale-ghost" in _t4_sf:
            failures.append(f"WP-15 T4: stale id NOT pruned from seen_file after read: {_t4_sf}")
        if "live-body" in _s4:   # t4-live was in prior seen_file → suppressed
            failures.append(f"WP-15 T4: prior-session body leaked through after load-time prune: {_s4[:120]!r}")

        # ── T5 — show_all=True re-dumps suppressed bodies across the session boundary ──
        _ctx15["identity"] = _Id15(_b15_agent, _t1_root)   # fresh session, seen_file has all three
        _s5_sup = _hi15({}, _ctx15)   # without show_all: prior-session bodies must be suppressed
        if "body-alpha" in _s5_sup or "body-beta" in _s5_sup or "body-new" in _s5_sup:
            failures.append(f"WP-15 T5: without show_all, prior-session bodies leaked through: {_s5_sup[:120]!r}")
        _s5 = _hi15({"show_all": True}, _ctx15)   # with show_all: all bodies re-dump in same session
        if "body-alpha" not in _s5 or "body-beta" not in _s5 or "body-new" not in _s5:
            failures.append(f"WP-15 T5: show_all=True did not re-dump all suppressed bodies: {_s5[:120]!r}")

        # ── T6 — watcher no-noise: after cross-session read, last_seen set → no re-nudge ──
        # guards that set_last_seen fires even on an all-suppressed read (no early-return path skips it)
        _t6_seen = _ctx15["identity"].get_last_seen() or set()
        if not {"wp15-a", "wp15-b", "wp15-c"} <= _t6_seen:
            failures.append(f"WP-15 T6: last_seen after cross-session read missing ids: {_t6_seen}")
        _t6_unread = {m.get("id") for m in _c15.read_json_safe(_t1_ix / f"{_b15_agent}_unread.json")}
        _t6_unseen = _t6_unread - _t6_seen
        if _t6_unseen:
            failures.append(f"WP-15 T6: watcher would re-nudge already-read message ids: {_t6_unseen}")

        # ── T7 — count_only is inert: no seen_file created by a count-only call ──
        # guards write placement: seen_file write must be AFTER the count_only early-return
        _t7_root = tempfile.mkdtemp(prefix="tc-15-t7-")
        _t7_ix = _c15.get_inboxes_dir(_t7_root, None)
        _c15.ensure_inbox(_t7_ix, _b15_agent)
        _c15.write_json_atomic(_t7_ix / f"{_b15_agent}_unread.json",
                               [{"id": "t7-1", "from": "x", "priority": "normal", "message": "t7body"}])
        _ctx15t7 = {"identity": _Id15(_b15_agent, _t7_root)}
        _hi15({"count_only": True}, _ctx15t7)
        if (_t7_ix / f"{_b15_agent}_seen.json").exists():
            failures.append("WP-15 T7: count_only created seen_file (must be inert)")

        # ── T8 — windowed cross-session: window A suppressed, window B renders full ──
        _t8_root = tempfile.mkdtemp(prefix="tc-15-t8-")
        _t8_ix = _c15.get_inboxes_dir(_t8_root, None)
        _c15.ensure_inbox(_t8_ix, _b15_agent)
        _w1 = {"id": "t8-w1", "from": "alice", "priority": "normal", "message": "win-one-body"}
        _w2 = {"id": "t8-w2", "from": "bob",   "priority": "normal", "message": "win-two-body"}
        _w3 = {"id": "t8-w3", "from": "carol", "priority": "normal", "message": "win-thr-body"}
        _c15.write_json_atomic(_t8_ix / f"{_b15_agent}_unread.json", [_w1, _w2, _w3])
        _ctx15t8 = {"identity": _Id15(_b15_agent, _t8_root)}
        # Session 1: read window A = only w3 (since >= "t8-w3").
        _sa = _hi15({"since": "t8-w3"}, _ctx15t8)
        if "win-thr-body" not in _sa:
            failures.append(f"WP-15 T8 session-1: window A did not render w3 body: {_sa[:80]!r}")
        # Session 2: fresh Identity; read window B = newest 2 (w2+w3). w3 suppressed, w2 full.
        _ctx15t8["identity"] = _Id15(_b15_agent, _t8_root)
        _sb = _hi15({"limit": 2}, _ctx15t8)
        if "win-thr-body" in _sb:
            failures.append(f"WP-15 T8: window-A id (w3) not suppressed in session-2 read: {_sb[:80]!r}")
        if "win-two-body" not in _sb:
            failures.append(f"WP-15 T8: window-B new id (w2) wrongly suppressed: {_sb[:80]!r}")

    except Exception as e:
        failures.append(f"WP-15 cross-session suppression unit checks errored: {e}")

    # ── WP-16 AC-4 — handle_safely: a dispatch crash is answered -32603, the loop survives ──
    try:
        from teammate_comms import server as _srv16
        sent16 = []
        _orig_send16 = _srv16.send_message
        _orig_dispatch16 = _srv16.tools_mod.dispatch
        _srv16.send_message = lambda obj: sent16.append(obj)

        def _boom16(name, arguments, ctx):
            raise RuntimeError("WP-16 AC-4 induced crash")

        _srv16.tools_mod.dispatch = _boom16
        try:
            _srv16.handle_safely(
                {"jsonrpc": "2.0", "id": 9016, "method": "tools/call",
                 "params": {"name": "whatever", "arguments": {}}},
                {"identity": None, "register": None},
            )
        finally:
            _srv16.tools_mod.dispatch = _orig_dispatch16
            _srv16.send_message = _orig_send16
        crash_resp = next((m for m in sent16 if m.get("id") == 9016), None)
        if not crash_resp or (crash_resp.get("error") or {}).get("code") != -32603:
            failures.append(f"WP-16 AC-4: handler crash not answered -32603: {sent16}")
    except Exception as e:
        failures.append(f"WP-16 AC-4 unit check errored: {e}")

    # ── WP-17 AC-1/AC-2 (G1) — teammate_list project comparison is cross-OS normalized ──
    # Tautology guard: this MUST fail against current main (raw string compare splits the two
    # spellings) — the assertions below check the SPECIFIC symptom (peer missing from default
    # list), not just "an exception was raised".
    try:
        from teammate_comms.tools import _handle_list as _hl17
        from teammate_comms import comms as _c17

        class _Id17:
            def __init__(self, agent, root, team=None):
                self._agent, self._root, self._team = agent, root, team
            def snapshot(self):
                return (self._agent, self._team, self._root, None)

        _t17_root = tempfile.mkdtemp(prefix="tc-17-g1-")
        _ag17 = _c17.get_agents_dir(_t17_root, None)
        _ag17.mkdir(parents=True, exist_ok=True)
        _hb17 = "2026-01-01T00:00:00.000000"
        _records17 = {
            "caller17":   {"type": "full", "project": "Projects\\Foo", "lastHeartbeat": _hb17},
            "peer17":     {"type": "full", "project": "projects/foo", "lastHeartbeat": _hb17},
            "outsider17": {"type": "full", "project": "other/bar", "lastHeartbeat": _hb17},
        }
        for _n17, _rec17 in _records17.items():
            _c17.write_json_atomic(_ag17 / f"{_n17}.json", _rec17)
        _ctx17 = {"identity": _Id17("caller17", _t17_root)}
        _default17 = _hl17({}, _ctx17)
        if "peer17" not in _default17:
            failures.append(f"WP-17 G1: default list dropped a same-project peer with a "
                             f"different-OS spelling (Windows vs Unix): {_default17[:200]!r}")
        if "outsider17" in _default17:
            failures.append(f"WP-17 G1: default list wrongly showed a different-project outsider: {_default17[:200]!r}")
        _all17 = _hl17({"all": True}, _ctx17)
        if "peer17" not in _all17 or "outsider17" not in _all17:
            failures.append(f"WP-17 G1: all=True did not show every teammate: {_all17[:200]!r}")
    except Exception as e:
        failures.append(f"WP-17 G1 unit checks errored: {e}")

    # ── WP-17 AC-3 (C1) — _handle_inbox locks the unread read; corrupt file still self-heals ──
    try:
        import inspect
        from teammate_comms.tools import _handle_inbox as _hi17c1
        from teammate_comms import comms as _c17c1

        _src17c1 = inspect.getsource(_hi17c1)
        if "file_lock(unread_file)" not in _src17c1:
            failures.append("WP-17 AC-3: _handle_inbox source missing the file_lock(unread_file) tripwire")

        class _Id17c1:
            def __init__(self, agent, root, team=None):
                self._agent, self._root, self._team = agent, root, team
                self._ls = None
            def snapshot(self):
                return (self._agent, self._team, self._root, None)
            def get_last_seen(self):
                return self._ls
            def set_last_seen(self, v):
                self._ls = v

        _t17c1_root = tempfile.mkdtemp(prefix="tc-17-c1-")
        _ix17c1 = _c17c1.get_inboxes_dir(_t17c1_root, None)
        _c17c1.ensure_inbox(_ix17c1, "probe17c1")
        (_ix17c1 / "probe17c1_unread.json").write_text("{torn", encoding="utf-8")
        _ctx17c1 = {"identity": _Id17c1("probe17c1", _t17c1_root)}
        _res17c1 = _hi17c1({}, _ctx17c1)
        if "No unread messages" not in _res17c1:
            failures.append(f"WP-17 AC-3: a really-corrupt unread file did not self-heal to an empty inbox: {_res17c1[:120]!r}")
        _healed17c1 = json.loads((_ix17c1 / "probe17c1_unread.json").read_text(encoding="utf-8"))
        if _healed17c1 != []:
            failures.append(f"WP-17 AC-3: unread file not reset to [] after corruption: {_healed17c1!r}")
    except Exception as e:
        failures.append(f"WP-17 AC-3 unit checks errored: {e}")

    # ── WP-17 AC-4 (C2) — no unbounded read_reactions(root, team) call remains in tools.py ──
    try:
        _tools_src17 = (SRC / "teammate_comms" / "tools.py").read_text(encoding="utf-8")
        _unbounded17 = re.findall(r"read_reactions\(\s*root,\s*team\s*\)", _tools_src17)
        if _unbounded17:
            failures.append(f"WP-17 AC-4: unbounded read_reactions(root, team) call(s) remain in tools.py: {len(_unbounded17)}")
    except Exception as e:
        failures.append(f"WP-17 AC-4 unit check errored: {e}")

    # ── WP-19 AC-1 — instance_id/epoch stamped at register; re-register bumps epoch, keeps profile ──
    try:
        from teammate_comms import server as _srv19
        from teammate_comms.comms import read_agent_record as _rar19

        _t19_root = tempfile.mkdtemp(prefix="tc-19-ac1-")
        _name19 = "probe19ac1"
        _srv19.register_identity(_name19, None, _t19_root,
                                  {"role": "tester19", "personality": "curt-and-dry"})
        _rec19a = _rar19(_t19_root, None, _name19) or {}
        _iid19 = _rec19a.get("instance_id")
        if not (isinstance(_iid19, str) and len(_iid19) == 32):
            failures.append(f"WP-19 AC-1: instance_id missing/malformed after register: {_iid19!r}")
        if _rec19a.get("epoch") != 1:
            failures.append(f"WP-19 AC-1: first register epoch should be 1, got {_rec19a.get('epoch')!r}")

        # Re-register same name: epoch bumps, profile fields (not re-passed) are PRESERVED.
        _srv19.register_identity(_name19, None, _t19_root, {})
        _rec19b = _rar19(_t19_root, None, _name19) or {}
        if _rec19b.get("epoch") != 2:
            failures.append(f"WP-19 AC-1: re-register should bump epoch to 2, got {_rec19b.get('epoch')!r}")
        if _rec19b.get("instance_id") != _iid19:
            failures.append(f"WP-19 AC-1: instance_id changed across re-register in the SAME "
                             f"process: {_iid19!r} -> {_rec19b.get('instance_id')!r}")
        if _rec19b.get("role") != "tester19" or _rec19b.get("personality") != "curt-and-dry":
            failures.append(f"WP-19 AC-1: re-register lost the prior profile: {_rec19b!r}")
    except Exception as e:
        failures.append(f"WP-19 AC-1 unit checks errored: {e}")

    # ── WP-19 gate CR — write_agent_record(bump_epoch=True) hands out DISTINCT epochs from its
    # OWN RETURN VALUE, never from a separate read-back (a read-back would race a competitor's
    # register and could return THEIR epoch — see comms.write_agent_record's docstring). Two
    # bump_epoch writes to the same name must return epochs N+1 and N+2, strictly increasing.
    try:
        from teammate_comms.comms import write_agent_record as _warcr19

        _tcr19_root = tempfile.mkdtemp(prefix="tc-19-epoch-cr-")
        _rcr19a = _warcr19(_tcr19_root, None, "epochprobe19", timeout=5, bump_epoch=True, type="full")
        _rcr19b = _warcr19(_tcr19_root, None, "epochprobe19", timeout=5, bump_epoch=True, type="full")
        if not (isinstance(_rcr19a, dict) and isinstance(_rcr19b, dict)):
            failures.append(f"WP-19 gate CR: write_agent_record must return the merged record "
                             f"dict, not True/False: {_rcr19a!r}, {_rcr19b!r}")
        elif not (_rcr19a.get("epoch") == 1 and _rcr19b.get("epoch") == 2):
            failures.append(f"WP-19 gate CR: two bump_epoch writes should return epochs 1 then "
                             f"2 from their RETURN VALUES: {_rcr19a.get('epoch')!r}, "
                             f"{_rcr19b.get('epoch')!r}")
    except Exception as e:
        failures.append(f"WP-19 gate CR unit check errored: {e}")

    # ── WP-19 AC-2 (flap kill) + TOCTOU tie-break — compute_heartbeat_permit pure-function tests ──
    # No real sleeps: timestamps are injected (Silvie gate addendum (d)).
    try:
        from teammate_comms import channel as _ch19
        from teammate_comms.comms import TIMESTAMP_FMT as _TSFMT19

        _now19 = datetime(2026, 1, 1, 12, 0, 0)
        _my_id19, _my_epoch19 = "mine", 5

        # (b) no record / instance_id-absent record (all pre-WP-19 legacy records) → permit.
        if not _ch19.compute_heartbeat_permit(None, _my_id19, _my_epoch19, _now19):
            failures.append("WP-19 AC-2(b): no record should permit the write")
        if not _ch19.compute_heartbeat_permit({}, _my_id19, _my_epoch19, _now19):
            failures.append("WP-19 AC-2(b): instance_id-absent record should permit the write")

        # AC-2: foreign + FRESH (right now) → SKIP (demoted).
        _foreign_fresh19 = {"instance_id": "other", "epoch": 99,
                            "lastHeartbeat": _now19.strftime(_TSFMT19)}
        if _ch19.compute_heartbeat_permit(_foreign_fresh19, _my_id19, _my_epoch19, _now19):
            failures.append("WP-19 AC-2: foreign+fresh record should SKIP the write (demoted)")

        # AC-2: foreign + STALE (>30s old) → PERMIT (legitimate re-claim of a dead process).
        _stale_hb19 = (_now19 - timedelta(seconds=31)).strftime(_TSFMT19)
        _foreign_stale19 = {"instance_id": "other", "epoch": 99, "lastHeartbeat": _stale_hb19}
        if not _ch19.compute_heartbeat_permit(_foreign_stale19, _my_id19, _my_epoch19, _now19):
            failures.append("WP-19 AC-2: foreign+stale record should PERMIT the write (re-claim)")

        # TOCTOU tie-break: foreign instance_id but epoch matches MINE → I was heartbeat-stomped
        # by a race, not superseded by a real registration → re-claim (write).
        _stomped19 = {"instance_id": "stomper", "epoch": _my_epoch19,
                      "lastHeartbeat": _now19.strftime(_TSFMT19)}
        if not _ch19.compute_heartbeat_permit(_stomped19, _my_id19, _my_epoch19, _now19):
            failures.append("WP-19 TOCTOU: foreign+fresh but epoch==mine should re-claim (write)")

        # A genuinely later registration (foreign instance_id, a DIFFERENT epoch, fresh) → skip.
        _other19 = {"instance_id": "other", "epoch": _my_epoch19 + 1,
                    "lastHeartbeat": _now19.strftime(_TSFMT19)}
        if _ch19.compute_heartbeat_permit(_other19, _my_id19, _my_epoch19, _now19):
            failures.append("WP-19 TOCTOU: foreign+fresh with a DIFFERENT epoch should skip "
                             "(a genuinely later claimant)")
    except Exception as e:
        failures.append(f"WP-19 AC-2/TOCTOU unit checks errored: {e}")

    # ── WP-19 AC-3 (S2) — register warning names a live foreign claimant's host/pid ──
    try:
        from teammate_comms import server as _srv19b
        from teammate_comms.comms import write_agent_record as _war19, now_timestamp as _nt19

        _t19c_root = tempfile.mkdtemp(prefix="tc-19-ac3-")
        _name19c = "probe19ac3"
        _war19(_t19c_root, None, _name19c, type="full", channel=True,
               pid=999999, host="some-other-host", instance_id="foreign-instance",
               epoch=1, lastHeartbeat=_nt19())
        _msg19c = _srv19b.register_identity(_name19c, None, _t19c_root, {})
        if "WARNING" not in _msg19c or "some-other-host" not in _msg19c or "999999" not in _msg19c:
            failures.append(f"WP-19 AC-3: register did not warn naming the foreign host/pid: {_msg19c[:200]!r}")

        # (c) self must never warn: re-registering the SAME identity (own instance_id, fresh)
        # in the SAME process must be silent — twice, to prove it holds on the second call too.
        _name19c2 = "probe19ac3-self"
        _msg19c2 = _srv19b.register_identity(_name19c2, None, _t19c_root, {})
        if "WARNING" in _msg19c2:
            failures.append(f"WP-19 AC-3(c): first register wrongly warned: {_msg19c2[:200]!r}")
        _msg19c3 = _srv19b.register_identity(_name19c2, None, _t19c_root, {})
        if "WARNING" in _msg19c3:
            failures.append(f"WP-19 AC-3(c): own re-register wrongly warned (same instance_id): {_msg19c3[:200]!r}")

        # (c) a STALE foreign record must not warn either (dead claimant, not live).
        _name19c4 = "probe19ac3-stale"
        _stale_hb19c = (datetime.now() - timedelta(seconds=60)).strftime(_TSFMT19)
        _war19(_t19c_root, None, _name19c4, type="full", channel=True,
               pid=999998, host="another-host", instance_id="foreign-instance-2",
               epoch=1, lastHeartbeat=_stale_hb19c)
        _msg19c4 = _srv19b.register_identity(_name19c4, None, _t19c_root, {})
        if "WARNING" in _msg19c4:
            failures.append(f"WP-19 AC-3(c): stale foreign record wrongly warned: {_msg19c4[:200]!r}")
    except Exception as e:
        failures.append(f"WP-19 AC-3 unit checks errored: {e}")

    # ── WP-19 AC-4 — human guard: registering over a type=human record raises CommsError ──
    # Tautology: this MUST fail against current main (register currently succeeds silently).
    try:
        from teammate_comms import server as _srv19d
        from teammate_comms.comms import register_human as _rh19, CommsError as _CE19

        _t19d_root = tempfile.mkdtemp(prefix="tc-19-ac4-")
        _human19 = "Operator19"
        _rh19(_t19d_root, None, _human19)
        try:
            _srv19d.register_identity(_human19, None, _t19d_root, {})
            failures.append("WP-19 AC-4 [tautology: register_identity must raise CommsError over "
                             "a type=human record — reverted code lets this silently succeed]")
        except _CE19 as _e19d:
            if _human19 not in str(_e19d):
                failures.append(f"WP-19 AC-4: human-guard error text must name the human: {_e19d}")
        except Exception as _e19d2:
            failures.append(f"WP-19 AC-4: wrong exception type ({type(_e19d2).__name__}), "
                             f"want CommsError: {_e19d2}")
    except Exception as e:
        failures.append(f"WP-19 AC-4 unit checks errored: {e}")

    # ── WP-19/WP-21 D1 (dashboard half) — collision warning ONLY for a FRESH foreign host ──
    # WP-21 gate addendum tightened this: a host mismatch alone is not enough — a long-dead
    # dashboard's stale host is a silent (fine) takeover; only FRESH presence from a different
    # host should warn.
    try:
        from teammate_comms import dashboard as _dash19
        from teammate_comms.comms import (
            write_agent_record as _war19e,
            now_timestamp as _nt19e,
            TIMESTAMP_FMT as _TSFMT19d1,
        )

        # Fresh + different host → warns.
        _t19e_root = tempfile.mkdtemp(prefix="tc-19-d1-")
        _human19e = "Operator19d1"
        _war19e(_t19e_root, None, _human19e, type="human", host="some-other-machine",
                startedAt=_nt19e(), presence="online", presenceAt=_nt19e(), dashboard_pid=424242)
        _info19e = _dash19.start_dashboard(_t19e_root, None, _human19e, port=0, open_browser=False)
        if "some-other-machine" not in (_info19e.get("warning") or ""):
            failures.append(f"WP-19/21 D1: fresh foreign-host record should warn: {_info19e}")
        _dash19.shutdown_dashboard()

        # Stale + different host → silent (a dead dashboard's stale host is a fine takeover).
        _t19f_root = tempfile.mkdtemp(prefix="tc-19-d1-stale-")
        _human19f = "Operator19d1stale"
        _stale_hb19f = (datetime.now() - timedelta(seconds=120)).strftime(_TSFMT19d1)
        _war19e(_t19f_root, None, _human19f, type="human", host="some-other-machine",
                startedAt=_stale_hb19f, presence="online", presenceAt=_stale_hb19f,
                dashboard_pid=424242)
        _info19f = _dash19.start_dashboard(_t19f_root, None, _human19f, port=0, open_browser=False)
        if _info19f.get("warning"):
            failures.append(f"WP-19/21 D1: stale foreign-host record should NOT warn: {_info19f}")
        _dash19.shutdown_dashboard()
    except Exception as e:
        failures.append(f"WP-19/21 D1 unit check errored: {e}")

    # ── WP-20 AC-1/AC-2 (I3) — locked, verified teammate deletion (tautology on current main) ──
    try:
        from teammate_comms import tools as _tools20
        from teammate_comms import comms as _c20
        from teammate_comms.comms import (
            file_lock as _flock20,
            write_agent_record as _war20,
            read_agent_record as _rar20,
            read_deletions as _rd20,
            get_agents_dir as _gad20,
        )

        _t20_root = tempfile.mkdtemp(prefix="tc-20-ac1-")
        _victim20 = "victim20"
        _war20(_t20_root, None, _victim20, type="full", channel=False, pid=1,
               host="wherever", instance_id="v-instance", epoch=1)
        _c20.ensure_inbox(_c20.get_inboxes_dir(_t20_root, None), _victim20)
        _record_path20 = _gad20(_t20_root, None) / f"{_victim20}.json"

        # Hold the record's lock (simulates a concurrent writer) and attempt removal.
        with _flock20(_record_path20):
            _msg20a = _tools20.remove_teammate(_t20_root, None, "caller20", _victim20)
        if "Removed teammate" in _msg20a and "locked" not in _msg20a.lower():
            failures.append(f"WP-20 AC-1/AC-2 [tautology: removal under a held lock must NOT "
                             f"report unconditional success — reverted code silently no-ops "
                             f"the unlink and reports success anyway]: {_msg20a}")
        if "locked" not in _msg20a.lower():
            failures.append(f"WP-20 AC-1: partial-failure text missing: {_msg20a}")
        _events20a = _rd20(_t20_root, None)
        if any(e.get("target") == "@" + _victim20 for e in _events20a):
            failures.append("WP-20 AC-1: deletion event appended despite the registry record "
                             "surviving (teammate isn't actually gone)")
        if not _rar20(_t20_root, None, _victim20):
            failures.append("WP-20 AC-1: registry record was removed despite the held lock "
                             "(the lock did not actually protect it)")

        # Release + retry: clean success + event appended.
        _msg20b = _tools20.remove_teammate(_t20_root, None, "caller20", _victim20)
        if "Removed teammate" not in _msg20b or "locked" in _msg20b.lower():
            failures.append(f"WP-20 AC-1: retry after lock release should cleanly succeed: {_msg20b}")
        _events20b = _rd20(_t20_root, None)
        if not any(e.get("target") == "@" + _victim20 for e in _events20b):
            failures.append("WP-20 AC-1: deletion event missing after a clean successful removal")
        if _rar20(_t20_root, None, _victim20):
            failures.append("WP-20 AC-1: registry record still present after a clean removal")
    except Exception as e:
        failures.append(f"WP-20 AC-1/AC-2 unit checks errored: {e}")

    # ── WP-20 AC-3/AC-4 (I4) — heartbeat-shaped write stamps type=full; never stomps type=human ──
    try:
        from teammate_comms import channel as _ch20
        from teammate_comms.comms import (
            write_agent_record as _war20b,
            read_agent_record as _rar20b,
            get_inboxes_dir as _gid20b,
            ensure_inbox as _ei20b,
        )

        class _Id20:
            def __init__(self, agent, root, unread_file):
                self._agent, self._team, self._root, self._uf = agent, None, root, unread_file
            def snapshot(self):
                return (self._agent, self._team, self._root, self._uf)
            def get_generation(self):
                return 1
            def get_last_seen(self):
                return None
            def set_last_seen(self, v):
                pass
            def get_instance_id(self):
                return "ghost-instance-20"
            def get_epoch(self):
                return 1

        # AC-3: simulate the delete-then-heartbeat ghost. A registered agent's record is gone
        # (simulating I3's remove_teammate); its still-running watcher's very first heartbeat
        # tick (last_hb starts at 0.0 → fires immediately) must re-create the record WITH
        # type=full, not a type-less ghost.
        _t20c_root = tempfile.mkdtemp(prefix="tc-20-ac3-")
        _ghost20 = "ghost20"
        _ix20c = _gid20b(_t20c_root, None)
        _ei20b(_ix20c, _ghost20)
        _id20 = _Id20(_ghost20, _t20c_root, _ix20c / f"{_ghost20}_unread.json")
        _init20, _reg20, _stop20 = threading.Event(), threading.Event(), threading.Event()
        _init20.set(); _reg20.set()
        _wt20 = threading.Thread(target=_ch20.run_watcher,
                                  args=(lambda obj: None, _id20, _init20, _reg20, _stop20),
                                  daemon=True)
        _wt20.start()
        try:
            _ok20 = wait_until(
                lambda: (_rar20b(_t20c_root, None, _ghost20) or {}).get("type") == "full",
                timeout=3.0)
            if not _ok20:
                failures.append(f"WP-20 AC-3: watcher heartbeat did not re-create a type=full "
                                 f"record: {_rar20b(_t20c_root, None, _ghost20)!r}")
        finally:
            _stop20.set()
            _wt20.join(timeout=2)

        # AC-4: a type=human record must KEEP type=human after a heartbeat-shaped write for the
        # SAME name (unreachable via WP-19's register-time guard in practice, but the guard
        # inside the watcher must hold on its own).
        _t20d_root = tempfile.mkdtemp(prefix="tc-20-ac4-")
        _human20 = "human20"
        _ix20d = _gid20b(_t20d_root, None)
        _ei20b(_ix20d, _human20)
        _war20b(_t20d_root, None, _human20, type="human", host="somewhere", presence="online")
        _id20h = _Id20(_human20, _t20d_root, _ix20d / f"{_human20}_unread.json")
        _init20h, _reg20h, _stop20h = threading.Event(), threading.Event(), threading.Event()
        _init20h.set(); _reg20h.set()
        _wt20h = threading.Thread(target=_ch20.run_watcher,
                                   args=(lambda obj: None, _id20h, _init20h, _reg20h, _stop20h),
                                   daemon=True)
        _wt20h.start()
        try:
            wait_until(lambda: (_rar20b(_t20d_root, None, _human20) or {}).get("channel") is True,
                       timeout=3.0)  # let at least one heartbeat tick land
            _rec20h = _rar20b(_t20d_root, None, _human20) or {}
            if _rec20h.get("type") != "human":
                failures.append(f"WP-20 AC-4: heartbeat-shaped write stomped a human record's "
                                 f"type: {_rec20h!r}")
        finally:
            _stop20h.set()
            _wt20h.join(timeout=2)
    except Exception as e:
        failures.append(f"WP-20 AC-3/AC-4 unit checks errored: {e}")

    # ── WP-20 AC-5 (I2) — reincarnate refuses a human-typed target; gate-off still checks first ──
    try:
        from teammate_comms.tools import _handle_reincarnate as _hr20e, CommsError as _CE20e
        from teammate_comms.comms import write_agent_record as _war20e

        _t20e_root = tempfile.mkdtemp(prefix="tc-20-ac5-")
        _human20e = "human20e"
        _war20e(_t20e_root, None, _human20e, type="human", host="wherever", presence="away")

        class _Id20e:
            def snapshot(self):
                return ("caller20e", None, _t20e_root, None)
        _ctx20e = {"identity": _Id20e()}

        _prev_gate20e = os.environ.pop("TEAMMATE_REINCARNATE_ENABLED", None)
        try:
            # Gate-off path must short-circuit FIRST — before the human-record read.
            try:
                _hr20e({"agent": _human20e, "project_dir": str(REPO)}, _ctx20e)
                failures.append("WP-20 AC-5: reincarnate should raise when gated off")
            except _CE20e as _ge20e:
                if "disabled" not in str(_ge20e):
                    failures.append(f"WP-20 AC-5: gate-off error text wrong: {_ge20e}")

            # Gate enabled + human target → must raise, naming the operator. Tautology: on
            # current main is_channel_alive(existing) is always False for a human record
            # (register_human never sets `channel`) — the live-check is blind, so this would
            # otherwise sail through and spawn a child.
            os.environ["TEAMMATE_REINCARNATE_ENABLED"] = "1"
            try:
                _hr20e({"agent": _human20e, "project_dir": str(REPO)}, _ctx20e)
                failures.append("WP-20 AC-5 [tautology: reincarnate must raise for a type=human "
                                 "target — reverted code's live-check can't see it and would "
                                 "spawn a child over the operator's identity]")
            except _CE20e as _e20e:
                if _human20e not in str(_e20e):
                    failures.append(f"WP-20 AC-5: reincarnate-human error must name the operator: {_e20e}")
        finally:
            if _prev_gate20e is None:
                os.environ.pop("TEAMMATE_REINCARNATE_ENABLED", None)
            else:
                os.environ["TEAMMATE_REINCARNATE_ENABLED"] = _prev_gate20e
    except Exception as e:
        failures.append(f"WP-20 AC-5 unit checks errored: {e}")

    # ── WP-21 AC-1 (B1) — human_presence_online: fresh/aged presenceAt, back-compat no-presenceAt ──
    try:
        from teammate_comms.comms import human_presence_online as _hpo21, TIMESTAMP_FMT as _TSFMT21

        _now21 = datetime.now()
        _fresh21 = {"presence": "online", "presenceAt": _now21.strftime(_TSFMT21)}
        if not _hpo21(_fresh21):
            failures.append("WP-21 AC-1: fresh presenceAt should read online")

        _aged21 = {"presence": "online",
                   "presenceAt": (_now21 - timedelta(seconds=90)).strftime(_TSFMT21)}
        if _hpo21(_aged21):
            failures.append("WP-21 AC-1: aged (>60s) presenceAt should read away")

        _legacy21 = {"presence": "online"}  # no presenceAt key at all — back-compat
        if not _hpo21(_legacy21):
            failures.append("WP-21 AC-1: back-compat — presence=online with NO presenceAt "
                             "key must still read online")
    except Exception as e:
        failures.append(f"WP-21 AC-1 unit checks errored: {e}")

    # ── WP-21 AC-2 (B1) — shutdown clobber guard: only OUR dashboard_pid can mark away ──
    try:
        from teammate_comms.comms import (
            write_agent_record as _war21b, read_agent_record as _rar21b,
            set_human_presence as _shp21b,
        )
        _t21b_root = tempfile.mkdtemp(prefix="tc-21-ac2-")
        _human21b = "human21ac2"
        _war21b(_t21b_root, None, _human21b, type="human", presence="online", dashboard_pid=111)
        # Foreign pid attempt → untouched.
        _shp21b(_t21b_root, None, _human21b, "away", owner_pid=222)
        if (_rar21b(_t21b_root, None, _human21b) or {}).get("presence") != "online":
            failures.append("WP-21 AC-2: a FOREIGN dashboard_pid marked presence away (clobber)")
        # Our pid → away.
        _shp21b(_t21b_root, None, _human21b, "away", owner_pid=111)
        if (_rar21b(_t21b_root, None, _human21b) or {}).get("presence") != "away":
            failures.append("WP-21 AC-2: matching dashboard_pid failed to mark presence away")
    except Exception as e:
        failures.append(f"WP-21 AC-2 unit checks errored: {e}")

    # ── WP-21 AC-3 (G2) — case-variant collision rejected at register; exact re-register OK ──
    # Tautology: this MUST fail against current main (register silently proceeds).
    try:
        from teammate_comms import server as _srv21c
        from teammate_comms.comms import CommsError as _CE21c

        _t21c_root = tempfile.mkdtemp(prefix="tc-21-ac3-")
        _srv21c.register_identity("Bob21", None, _t21c_root, {})
        try:
            _srv21c.register_identity("bob21", None, _t21c_root, {})
            failures.append("WP-21 AC-3 [tautology: register must reject a case-variant "
                             "collision — reverted code silently proceeds, letting Windows "
                             "merge/Linux split the identity]")
        except _CE21c as _e21c:
            if "Bob21" not in str(_e21c):
                failures.append(f"WP-21 AC-3: case-collision error must name the existing "
                                 f"spelling: {_e21c}")
        # Exact re-register of the SAME spelling stays idempotent.
        try:
            _srv21c.register_identity("Bob21", None, _t21c_root, {})
        except Exception as _e21c2:
            failures.append(f"WP-21 AC-3: exact re-register of the same spelling should "
                             f"succeed: {_e21c2}")
    except Exception as e:
        failures.append(f"WP-21 AC-3 unit checks errored: {e}")

    # ── WP-21 AC-4 (G5) — Windows reserved device names rejected; near-misses accepted ──
    try:
        from teammate_comms.comms import (
            validate_agent_name as _van21, validate_group_name as _vgn21,
            validate_project_key as _vpk21, CommsError as _CE21d,
        )
        _rejected21 = ["con", "NUL", "com3", "con.helper"]
        _accepted21 = ["console", "con-bot", "lpt10"]
        for _n21 in _rejected21:
            for _label21, _fn21 in (("agent", _van21), ("group", _vgn21)):
                try:
                    _fn21(_n21)
                    failures.append(f"WP-21 AC-4: {_label21} validator accepted reserved name {_n21!r}")
                except _CE21d:
                    pass
            try:
                _vpk21(_n21)
                failures.append(f"WP-21 AC-4: project key validator accepted reserved name {_n21!r}")
            except _CE21d:
                pass
            try:
                _vpk21(f"parent/{_n21}")
                failures.append(f"WP-21 AC-4: project key validator accepted reserved SEGMENT {_n21!r}")
            except _CE21d:
                pass
        for _n21 in _accepted21:
            try:
                _van21(_n21)
            except _CE21d as _e21d:
                failures.append(f"WP-21 AC-4: agent validator wrongly rejected {_n21!r}: {_e21d}")
            try:
                _vgn21(_n21)
            except _CE21d as _e21d:
                failures.append(f"WP-21 AC-4: group validator wrongly rejected {_n21!r}: {_e21d}")
            try:
                _vpk21(_n21)
            except _CE21d as _e21d:
                failures.append(f"WP-21 AC-4: project key validator wrongly rejected {_n21!r}: {_e21d}")
    except Exception as e:
        failures.append(f"WP-21 AC-4 unit checks errored: {e}")

    # version sync
    pkg = re.search(r'__version__\s*=\s*"([^"]+)"',
                    (SRC / "teammate_comms" / "__init__.py").read_text(encoding="utf-8")).group(1)
    plug = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    pyp = re.search(r'^version\s*=\s*"([^"]+)"',
                    (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE).group(1)
    if not (pkg == plug == pyp):
        failures.append(f"version drift: pkg={pkg} plugin={plug} pyproject={pyp}")

    # ── report ──
    # G-3: a stdout-purity break makes `by_id` lose entries, cascading into many false downstream
    # failures (result()/text() return {}/"" for the missing ids). Surface it FIRST as the probable
    # ROOT CAUSE — do NOT early-exit (every failure stays visible), just re-order so the run isn't
    # misdiagnosed by an arbitrary cascade symptom.
    if bad_stdout:
        failures.insert(0, f"ROOT CAUSE — non-JSON-RPC line(s) on stdout (stdout-purity broke; the "
                           f"failures below are likely cascades of this): {bad_stdout[:3]}")
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
