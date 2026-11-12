"""Cross-machine relay — acceptance tests (docs/cross-machine-plan.md section 5).

Two relays run in one process on loopback (127.0.0.1 = machine "a", 127.0.0.2 = machine
"b") with a fake network object in place of Tailscale and two temporary comms roots.

Run: uv run --no-sync python tests/test_relay.py
"""
import http.client
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

os.environ["TEAMMATE_RELAY_AUTOSTART"] = "0"

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from teammate_comms import comms  # noqa: E402
from teammate_comms import peers as peers_mod  # noqa: E402
from teammate_comms import relay as relay_mod  # noqa: E402
from teammate_comms import server as server_mod  # noqa: E402
from teammate_comms import tools as tools_mod  # noqa: E402
from teammate_comms.comms import CommsError  # noqa: E402

relay_mod.HTTP_TIMEOUT_SECONDS = 1

failures = []
IPS = {"a": "127.0.0.1", "b": "127.0.0.2", "c": "127.0.0.3"}


def check(cond, msg):
    if not cond:
        failures.append(msg)


def check_raises(fn, exc, msg):
    try:
        fn()
        failures.append(f"expected {exc.__name__}, nothing raised: {msg}")
    except exc:
        pass
    except Exception as e:
        failures.append(f"expected {exc.__name__}, got {type(e).__name__}: {msg}: {e}")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeClock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class FakeNet:
    def __init__(self, me, others=("a", "b", "c")):
        self.me = me
        self.state = "running"
        self.table = {m: {"ip": IPS[m], "ips": [IPS[m]], "online": True, "expired": False}
                      for m in others if m != me}

    def refresh(self):
        pass

    def self_info(self):
        if self.state != "running":
            return {"state": "down", "detail": "Tailscale state: Stopped",
                    "machine": self.me, "ip": IPS[self.me]}
        return {"state": "running", "detail": "", "machine": self.me, "ip": IPS[self.me]}

    def peer_info(self, machine):
        return self.table.get(machine)

    def identify(self, ip):
        for machine, info in self.table.items():
            if ip in info["ips"]:
                return machine
        return None

    def devices(self):
        return sorted(self.table)


class FakeIdentity:
    def __init__(self, agent, root):
        self._agent, self._root = agent, root

    def snapshot(self):
        return (self._agent, None, self._root, None)

    def get_last_seen(self):
        return None

    def set_last_seen(self, ids):
        pass


def ctx_for(agent, root):
    return {"identity": FakeIdentity(agent, root), "register": None,
            "auto_register_error": lambda: None}


def add_agent(root, name):
    comms.ensure_inbox(comms.get_inboxes_dir(root, None), name)
    comms.write_agent_record(root, None, name, type="full", channel=True, pid=os.getpid(),
                             host=socket.gethostname(), lastHeartbeat=comms.now_timestamp(),
                             project="demo/app", status="testing")


def unread(root, name):
    return comms.read_json_readonly(comms.get_inboxes_dir(root, None) / f"{name}_unread.json") or []


class World:
    """Machines a (alice) and b (bob, carol), mutually approved."""

    def __init__(self):
        self._tmp = [tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()]
        self.root_a, self.root_b = (Path(t.name) for t in self._tmp)
        self.port_a, self.port_b = free_port(), free_port()
        self.clock = FakeClock()
        relay_mod.update_peers(self.root_a, lambda p: {"b": {"port": self.port_b}})
        relay_mod.update_peers(self.root_b, lambda p: {"a": {"port": self.port_a}})
        self.net_a, self.net_b = FakeNet("a"), FakeNet("b")
        self.ra = relay_mod.Relay(self.root_a, net=self.net_a, port=self.port_a, clock=self.clock)
        self.rb = relay_mod.Relay(self.root_b, net=self.net_b, port=self.port_b, clock=self.clock)
        add_agent(self.root_a, "alice")
        add_agent(self.root_b, "bob")
        add_agent(self.root_b, "carol")
        self.pump(2)

    def pump(self, n=3, step=3, a=True, b=True):
        for _ in range(n):
            self.clock.t += step
            if a:
                self.ra.tick()
            if b:
                self.rb.tick()

    def close(self):
        for root in (self.root_a, self.root_b):
            inboxes = comms.get_inboxes_dir(root, None)
            strays = [p.name for p in inboxes.glob("*") if "@" in p.name] if inboxes.exists() else []
            check(not strays, f"AC-14: remote names became inbox files: {strays}")
        self.ra.close()
        self.rb.close()
        time.sleep(0.2)
        for t in self._tmp:
            try:
                t.cleanup()
            except OSError:
                pass


def wait_closed(ip, port, seconds=5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            socket.create_connection((ip, port), timeout=0.3).close()
            time.sleep(0.1)
        except OSError:
            return True
    return False


def test_names_and_mentions():
    check(comms.split_address("bob@lap") == ("bob", "lap"), "split remote")
    check(comms.split_address("bob") == ("bob", None), "split local")
    check(comms.validate_address("bob@LapTop") == "bob@laptop", "machine lowercased")
    check_raises(lambda: comms.validate_address("bob@lap_top"), CommsError, "bad machine")
    check_raises(lambda: comms.validate_address("bob@a@b"), CommsError, "double @")
    check_raises(lambda: comms.validate_agent_name("bob@lap"), CommsError, "bare-only validator")
    check_raises(lambda: comms.ensure_inbox(Path(tempfile.gettempdir()), "bob@lap"),
                 CommsError, "ensure_inbox refuses a remote name")
    pm = tools_mod._parse_mentions
    members = {"bob", "bob@lap", "lap", "dana"}
    check(pm("hi @bob@lap!", members) == ["bob@lap"], f"AC-8 qualified mention: {pm('hi @bob@lap!', members)}")
    check(pm("mail bob@lap now", members) == [], "AC-8: name@machine without a leading @ is no mention")
    check(pm("ping @bob.", members) == ["bob"], "trailing punctuation stripped")
    check(pm("ping @bob, @dana", members) == ["bob", "dana"], "plain mentions unchanged")
    check(pm("@Bob@LAP", {"Bob@lap"}) == ["Bob@lap"], "machine part case-insensitive")
    check(pm("@bob@here", {"bob"}, me="here") == ["bob"], "self-machine mention is local")
    check(pm("@ghost", members) == [], "non-member dropped")


def test_dm_round_trip():
    w = World()
    try:
        res = tools_mod.send_dm(w.root_a, None, "alice", "bob@b", "hello bob")
        check(res["to_type"] == "remote" and res["to"] == "bob@b", f"AC-1: remote result {res}")
        w.pump()
        got = [m for m in unread(w.root_b, "bob") if m.get("message") == "hello bob"]
        check(len(got) == 1, f"AC-1: bob has one copy, got {len(got)}")
        if got:
            check(got[0]["from"] == "alice@a", f"AC-1: from localized, got {got[0]['from']}")
            check(got[0]["id"] == res["id"], "AC-1: original id kept")
        check(relay_mod.queued_count(w.root_a, "b") == 0, "AC-1: outbox drained")
        info = tools_mod.resolve_message(w.root_b, None, res["id"])
        check(info and info.get("to") == "bob", f"AC-1: transcript tee on B, got {info}")

        res2 = tools_mod.send_dm(w.root_b, None, "bob", "alice@a", "hi alice", reply_to=res["id"])
        w.pump()
        back = [m for m in unread(w.root_a, "alice") if m.get("message") == "hi alice"]
        check(len(back) == 1 and back[0]["from"] == "bob@b" and back[0]["id"] == res2["id"]
              and back[0].get("reply_to") == res["id"], f"AC-2: reply landed on A: {back}")

        local = tools_mod.send_dm(w.root_b, None, "bob", "carol@b", "same machine")
        check(local["to_type"] == "full" and local["to"] == "carol",
              f"self-machine address delivers locally: {local}")
    finally:
        w.close()


def test_unapproved_and_team():
    w = World()
    try:
        check_raises(lambda: tools_mod.send_dm(w.root_a, None, "alice", "x@zzz", "hi"),
                     CommsError, "AC-3: unapproved machine")
        check(not relay_mod.outbox_dir(w.root_a, "zzz").exists(), "AC-3: nothing queued")
        check_raises(lambda: tools_mod.send_dm(w.root_a, "teamx", "alice", "bob@b", "hi"),
                     CommsError, "team namespace refuses remote send")
        check(relay_mod.queued_count(w.root_a, "b") == 0, "team refusal queued nothing")
    finally:
        w.close()


def _post(ip_from, ip_to, port, body):
    conn = http.client.HTTPConnection(ip_to, port, timeout=3, source_address=(ip_from, 0))
    try:
        conn.request("POST", "/v1/exchange", body=json.dumps(body).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    finally:
        conn.close()


def _dm_event(event_id, sender, to, text, msg_id=None):
    return {"id": event_id, "kind": "dm", "payload": {"to": to, "record": {
        "id": msg_id or comms.new_message_id(), "from": sender, "priority": "normal",
        "message": text}}}


def test_auth():
    w = World()
    try:
        ev = _dm_event("a" * 32, "mallory", "bob@b", "from an unapproved machine")
        status, _ = _post(IPS["c"], IPS["b"], w.port_b, {"v": 1, "from": "c", "events": [ev]})
        check(status == 403, f"AC-4: unapproved source gets 403, got {status}")
        status, body = _post(IPS["c"], IPS["b"], w.port_b, {"v": 1, "from": "a", "events": [ev]})
        check(status == 403, f"AC-4: unapproved source claiming an approved name gets 403, got {status}")
        ev2 = _dm_event("b" * 32, "mallory", "bob@b", "wrong from")
        status, body = _post(IPS["a"], IPS["b"], w.port_b, {"v": 1, "from": "zzz", "events": [ev2]})
        check(status == 403 and body.get("error") == "identity_mismatch",
              f"AC-4: from mismatch refused: {status} {body}")
        texts = [m.get("message") for m in unread(w.root_b, "bob")]
        check("from an unapproved machine" not in texts and "wrong from" not in texts,
              "AC-4: refused requests applied nothing")
        status, body = _post(IPS["a"], IPS["b"], w.port_b,
                             {"v": 1, "from": "a", "events": [_dm_event("c" * 32, "alice", "bob@b", "ok")]})
        check(status == 200 and body.get("applied") == ["c" * 32], f"control: approved source applies: {body}")
    finally:
        w.close()


def test_trust_rules():
    w = World()
    try:
        ap = lambda kind, payload: relay_mod.apply_event(w.root_b, "b", "a", kind, payload)  # noqa: E731
        rej = relay_mod.RejectEvent
        check_raises(lambda: ap("dm", _dm_event("x", "mallory@b", "bob@b", "spoof")["payload"]),
                     rej, "AC-5: qualified actor on the wire")
        check_raises(lambda: ap("dm", _dm_event("x", "alice", "dave@other", "third")["payload"]),
                     rej, "DM for a third machine")
        check_raises(lambda: ap("dm", _dm_event("x", "alice", "bob", "bare to")["payload"]),
                     rej, "bare 'to' means the sender's own machine")
        check_raises(lambda: ap("dm", _dm_event("x", "alice", "bob@b", "bad id", msg_id="../x")["payload"]),
                     rej, "malformed message id")
        check_raises(lambda: ap("nope", {}), rej, "unknown kind")
        check(not [m for m in unread(w.root_b, "bob") if m.get("message") in ("spoof", "third", "bare to", "bad id")],
              "rejected events delivered nothing")

        mine = tools_mod.send_dm(w.root_b, None, "carol", "bob", "carol's own message")
        check_raises(lambda: ap("dm", _dm_event("x", "alice", "bob@b", "collide", msg_id=mine["id"])["payload"]),
                     rej, "B2: reusing another author's id")
        check_raises(lambda: ap("deletion", {"target": mine["id"], "by": "alice"}),
                     rej, "AC-10: deleting a message authored elsewhere")
        check(not [m for m in unread(w.root_b, "bob") if m.get("deleted")], "carol's message not tombstoned")

        payload = _dm_event("x", "alice", "bob@b", "once only")["payload"]
        ap("dm", payload)
        ap("dm", payload)
        check(len([m for m in unread(w.root_b, "bob") if m.get("message") == "once only"]) == 1,
              "AC-11: re-applying the same message delivers once")

        calls = {"n": 0}
        real = relay_mod.apply_event

        def flaky(*args, **kwargs):
            calls["n"] += 1
            raise CommsError("Could not acquire lock")
        relay_mod.apply_event = flaky
        try:
            applied, failed = w.rb._apply_events("a", [_dm_event("d" * 32, "alice", "bob@b", "retry me")])
        finally:
            relay_mod.apply_event = real
        check(applied == [] and failed == [] and "d" * 32 not in w.rb._seen_ring("a")["ids"],
              f"B3: a lock failure is retried, not dropped: {applied} {failed}")
        applied, failed = w.rb._apply_events("a", [_dm_event("d" * 32, "alice", "bob@b", "retry me")])
        check(applied == ["d" * 32], "B3: the retry applies")
        applied, failed = w.rb._apply_events("a", [_dm_event("e" * 32, "al@x", "bob@b", "bad")])
        check(applied == [] and len(failed) == 1 and "e" * 32 in w.rb._seen_ring("a")["ids"],
              "an invalid event is reported failed and never retried")
    finally:
        w.close()


def test_groups():
    w = World()
    try:
        alice, bob, carol = ctx_for("alice", w.root_a), ctx_for("bob", w.root_b), ctx_for("carol", w.root_b)
        grp = tools_mod._handle_group
        grp({"action": "create", "group": "design", "members": ["bob@b"]}, alice)
        w.pump()
        meta_b = comms.read_group_meta(w.root_b, None, "design")
        check(meta_b is not None and meta_b.get("members") == ["alice@a", "bob"]
              and meta_b.get("creator") == "alice@a", f"AC-6: group mirrored on B: {meta_b}")

        post = tools_mod.send_group(w.root_a, None, "alice", "#design", "kickoff @bob@b, cc x@b")
        check(post["remote"] == {"b": ["bob@b"]}, f"AC-6: remote fan-out accounting: {post['remote']}")
        w.pump(1, b=False)
        check(relay_mod.queued_count(w.root_b, "a") == 0, "an applied group post is not queued back")
        w.pump()
        got = [m for m in unread(w.root_b, "bob") if m.get("group") == "#design"]
        check(len(got) == 1 and got[0]["id"] == post["id"] and got[0]["from"] == "alice@a",
              f"AC-6: post reached bob with the same id: {got}")
        check(got and got[0].get("mentions") == ["bob"], f"AC-8: mention localized on B: {got and got[0].get('mentions')}")
        hist_b = [m.get("id") for m in comms.read_group_messages(w.root_b, None, "design")]
        check(hist_b == [post["id"]], f"AC-6: B's group transcript holds the post once: {hist_b}")

        relay_mod.update_peers(w.root_b, lambda p: {**p, "c": {}})
        relay_mod.apply_event(w.root_b, "b", "a", "group_members", {
            "group": "design", "op": "add", "names": ["dave@c"], "members": []})
        relay_mod.apply_event(w.root_b, "b", "a", "group_post", {"group": "design", "record": {
            "id": comms.new_message_id(), "from": "alice", "priority": "normal", "message": "no forward"}})
        check("dave@c" in comms.read_group_meta(w.root_b, None, "design")["members"], "third-machine member kept")
        check(relay_mod.queued_count(w.root_b, "c") == 0, "an applied group post is not forwarded to a third machine")
        relay_mod.update_peers(w.root_b, lambda p: {m: c for m, c in p.items() if m != "c"})

        post_b = tools_mod.send_group(w.root_b, None, "bob", "#design", "reply from b")
        w.pump()
        got_a = [m for m in unread(w.root_a, "alice") if m.get("group") == "#design"]
        check(len(got_a) == 1 and got_a[0]["id"] == post_b["id"] and got_a[0]["from"] == "bob@b",
              f"AC-6: B's post reached alice: {got_a}")

        grp({"action": "join", "group": "design"}, carol)
        w.pump()
        check("carol@b" in comms.read_group_meta(w.root_a, None, "design")["members"],
              "AC-7: a join on B adds carol@b on A")
        grp({"action": "leave", "group": "design"}, bob)
        w.pump()
        members_a = comms.read_group_meta(w.root_a, None, "design")["members"]
        check("bob@b" not in members_a and "carol@b" in members_a, f"AC-7: leave on B removed bob on A: {members_a}")

        check_raises(lambda: relay_mod.apply_event(w.root_a, "a", "b", "group_delete",
                                                   {"group": "design", "by": "carol"}),
                     relay_mod.RejectEvent, "AC-7: a non-creator's remote delete")
        check(comms.read_group_meta(w.root_a, None, "design") is not None, "AC-7: group survived")
        grp({"action": "delete", "group": "design"}, alice)
        w.pump()
        check(comms.read_group_meta(w.root_b, None, "design") is None, "AC-7: creator delete removed it on B")
        check(not [m for m in unread(w.root_b, "carol") if m.get("group") == "#design"],
              "AC-7: group copies purged from B inboxes")

        grp({"action": "create", "group": "secret", "members": ["carol"]}, bob)
        rej = relay_mod.RejectEvent
        check_raises(lambda: relay_mod.apply_event(w.root_b, "b", "a", "group_members", {
            "group": "secret", "op": "add", "names": ["alice"], "members": ["alice", "bob@b"]}),
            rej, "B1: a machine with no member cannot add itself to a local group")
        check_raises(lambda: relay_mod.apply_event(w.root_b, "b", "a", "group_post", {
            "group": "secret", "members": ["alice", "bob@b"], "record": {
                "id": comms.new_message_id(), "from": "alice", "priority": "normal", "message": "inject"}}),
            rej, "B1: a machine with no member cannot post into a local group")
        check(comms.read_group_meta(w.root_b, None, "secret")["members"] == ["bob", "carol"],
              "B1: private group membership unchanged")
        grp({"action": "create", "group": "secret", "members": ["bob@b"]}, alice)
        w.pump()
        told = [m for m in unread(w.root_a, "alice") if m.get("from") == comms.RELAY_SENDER]
        check(len(told) == 1 and "REJECTED by b" in told[0]["message"],
              f"a rejected event is reported to its sender: {[m['message'][:60] for m in told]}")
        check(comms.read_group_meta(w.root_b, None, "secret")["members"] == ["bob", "carol"],
              "B1: the rejected create changed nothing on B")
        check(relay_mod.queued_count(w.root_a, "b") == 0, "a rejected event leaves the queue")
        check_raises(lambda: relay_mod.apply_event(w.root_b, "b", "a", "group_members", {
            "group": "elsewhere", "op": "add", "names": ["alice"], "members": ["alice", "dave@other"]}),
            rej, "a group with no member on this machine is not created")
    finally:
        w.close()


def test_review_findings():
    w = World()
    try:
        alice, bob = ctx_for("alice", w.root_a), ctx_for("bob", w.root_b)
        grp = tools_mod._handle_group
        rej = relay_mod.RejectEvent

        calls = {"n": 0}
        real = relay_mod.apply_event

        def broken(*args, **kwargs):
            calls["n"] += 1
            raise FileNotFoundError("gone")
        relay_mod.apply_event = broken
        try:
            ev = [_dm_event("f" * 32, "alice", "bob@b", "never applies")]
            outcomes = [w.rb._apply_events("a", ev) for _ in range(relay_mod.MAX_APPLY_ATTEMPTS)]
        finally:
            relay_mod.apply_event = real
        check(all(o == ([], []) for o in outcomes[:-1]), "a non-lock error is retried a few times")
        check(outcomes[-1][0] == [] and len(outcomes[-1][1]) == 1,
              f"R1: a persistent non-lock error is reported failed, not retried forever: {outcomes[-1]}")

        grp({"action": "create", "group": "g2", "members": ["bob@b"]}, alice)
        post = tools_mod.send_group(w.root_a, None, "alice", "#g2", "soon orphaned")
        w.pump()
        tools_mod.delete_group_local(w.root_b, None, "g2", "bob")
        check_raises(lambda: relay_mod.apply_event(w.root_b, "b", "a", "deletion",
                                                   {"target": post["id"], "by": "alice"}),
                     rej, "R1: a deletion for a group deleted here is rejected, not an I/O error")
        check_raises(lambda: relay_mod.apply_event(w.root_b, "b", "a", "reaction",
                                                   {"target": post["id"], "from": "alice", "emoji": "fire"}),
                     rej, "R1: a reaction for a group deleted here is rejected")
        check_raises(lambda: relay_mod.apply_event(w.root_b, "b", "a", "group_post", {
            "group": "g2", "members": ["alice", "bob@b"], "creator": "alice", "record": {
                "id": comms.new_message_id(), "from": "alice", "priority": "normal", "message": "revive"}}),
            rej, "a post never re-creates a deleted group")
        check(comms.read_group_meta(w.root_b, None, "g2") is None, "deleted group stayed deleted")

        relay_mod.apply_event(w.root_a, "a", "b", "group_members", {
            "group": "g2", "op": "add", "names": ["dave@c"], "members": []})
        out = tools_mod._send_to_group("alice", None, w.root_a, "#g2", {"message": "who gets this"})
        check("NOT SENT to dave@c" in out and "not an approved peer" in out,
              f"R2: a member on an unapproved machine is named as not sent: {out}")
        check(not relay_mod.outbox_dir(w.root_a, "c").exists(), "R2: nothing queued for the unapproved machine")

        grp({"action": "leave", "group": "g2"}, alice)
        told = [m for m in unread(w.root_a, "alice") if m.get("from") == comms.RELAY_SENDER
                and "NOT SENT" in m.get("message", "") and "c (not an approved peer" in m["message"]]
        check(len(told) == 1, f"R4: a membership change that cannot be relayed is reported: "
                              f"{[m.get('message', '')[:60] for m in unread(w.root_a, 'alice')]}")

        grp({"action": "create", "group": "secret", "members": []}, bob)
        grp({"action": "create", "group": "secret", "members": ["bob@b"]}, alice)
        ev = json.loads(sorted(relay_mod.outbox_dir(w.root_a, "b").glob("*.json"))[-1]
                        .read_text(encoding="utf-8"))
        _status, lost = w.rb.handle_exchange("a", {
            "v": relay_mod.PROTOCOL, "from": "a", "ack": [], "failed": [],
            "events": [{"id": ev["id"], "kind": ev["kind"], "payload": ev["payload"]}]})
        check([f["id"] for f in lost["failed"]] == [ev["id"]], "R5: B rejects the group it already owns")
        reborn = relay_mod.Relay(w.root_b, net=w.net_b, port=free_port(), clock=w.clock)
        check(ev["id"] in reborn._seen_ring("a")["failed"], "R5: a rejection survives a relay restart")
        reborn.close()
        w.pump()
        told = [m for m in unread(w.root_a, "alice") if m.get("from") == comms.RELAY_SENDER
                and "REJECTED by b: your group_members" in m.get("message", "")]
        check(len(told) == 1, f"R5: a rejection whose response was lost still reaches the sender: "
                              f"{[m.get('message', '')[:60] for m in unread(w.root_a, 'alice')]}")
        check(relay_mod.queued_count(w.root_a, "b") == 0, "R5: the rejected event left A's queue")

        outsider = tools_mod.send_group(w.root_b, None, "bob", "#secret", "private to b")
        check_raises(lambda: relay_mod.apply_event(w.root_b, "b", "a", "reaction", {
            "target": outsider["id"], "from": "alice", "emoji": "fire"}),
            rej, "R6: a machine with no member in a group cannot react to its messages")

        grp({"action": "create", "group": "locked", "members": []}, alice)
        relay_mod.update_peers(w.root_b, lambda p: {"a": {"port": free_port()}})
        w.rb._peers.clear()
        grp({"action": "create", "group": "locked", "members": ["alice@a"]}, bob)
        w.pump(4)
        told = [m for m in unread(w.root_b, "bob")
                if m.get("from") == comms.RELAY_SENDER and "REJECTED by a" in m.get("message", "")]
        check(len(told) == 1, f"R3: a rejection of a pulled event reaches its sender: "
                              f"{[m.get('message', '')[:50] for m in unread(w.root_b, 'bob')]}")
        check(comms.read_group_meta(w.root_a, None, "locked")["members"] == ["alice"],
              "R3: the rejected add changed nothing on A")
        check(relay_mod.queued_count(w.root_b, "a") == 0, "R3: the rejected event left B's queue")

        relay_mod.update_peers(w.root_b, lambda p: {})
        w.pump(3, step=20, b=False)
        view = relay_mod.peer_view(w.root_a, "b")
        check(view["state"] == "not_approved" and "teammate_peers" in view["text"],
              f"being unapproved on the other side is its own state: {view['state']}")

        check(relay_mod._version_key("0.16.0") < relay_mod._version_key("0.17.0")
              and relay_mod._version_key("0.9.0") < relay_mod._version_key("0.16.0"), "version ordering")
        check_raises(lambda: comms.validate_machine_name("nul"), CommsError, "reserved device name as machine")
    finally:
        w.close()


def test_reactions_and_deletions():
    w = World()
    try:
        res = tools_mod.send_dm(w.root_a, None, "alice", "bob@b", "react to me")
        w.pump()
        tools_mod.react(w.root_b, None, "bob", res["id"], "thumbsup")
        w.pump(1, a=False)
        check(relay_mod.queued_count(w.root_a, "b") == 0, "an applied reaction is not queued back")
        w.pump()
        rx = [r for r in comms.read_reactions(w.root_a, None) if r.get("target") == res["id"]]
        check(len(rx) == 1 and rx[0].get("from") == "bob@b" and rx[0].get("target_from") == "alice"
              and rx[0].get("emoji") == "thumbsup", f"AC-9: reaction arrived on A: {rx}")

        out = tools_mod.delete_message(w.root_a, None, "alice", res["id"])
        check("everywhere" in out, f"AC-10: author delete reports everywhere: {out}")
        w.pump(1, b=False)
        check(relay_mod.queued_count(w.root_b, "a") == 0, "an applied deletion is not queued back")
        w.pump()
        copy = [m for m in unread(w.root_b, "bob") if m.get("id") == res["id"]]
        check(len(copy) == 1 and copy[0].get("deleted") is True
              and copy[0].get("deleted_by") == "alice@a", f"AC-10: tombstoned on B: {copy}")

        other = tools_mod.send_dm(w.root_a, None, "alice", "bob@b", "operator delete")
        w.pump()
        out = tools_mod.delete_message(w.root_b, None, "carol", other["id"], is_operator=True)
        check("this machine only" in out, f"AC-10: non-author delete stays local: {out}")
        check(relay_mod.queued_count(w.root_b, "a") == 0, "AC-10: non-author delete queued nothing")
    finally:
        w.close()


def test_queue_while_down():
    w = World()
    try:
        w.rb._close_listener()
        check(wait_closed(IPS["b"], w.port_b), "setup: B's listener closed")
        ids = [tools_mod.send_dm(w.root_a, None, "alice", "bob@b", f"queued {i}")["id"] for i in range(3)]
        w.pump(3, step=20, b=False)
        check(relay_mod.queued_count(w.root_a, "b") == 3, "AC-11: events stay queued while B is down")
        view = relay_mod.peer_view(w.root_a, "b")
        check(view["state"] == "relay_down" and not view["ok"], f"AC-11: state is relay_down: {view['state']}")
        report = "\n".join(relay_mod.delivery_report(w.root_a, "bob", "b"))
        check("NOT DELIVERED" in report and "3 event(s)" in report, f"send result warns loudly: {report}")
        listing = tools_mod._handle_list({"all": True}, ctx_for("alice", w.root_a))
        check("b: ⚠️ NOT REACHABLE" in listing, "teammate_list flags the unreachable machine")

        w.pump(3, step=40)
        got = [m["id"] for m in unread(w.root_b, "bob") if str(m.get("message", "")).startswith("queued")]
        check(got == ids, f"AC-11: delivered once, in order: {got} vs {ids}")
        w.pump(2)
        again = [m["id"] for m in unread(w.root_b, "bob") if str(m.get("message", "")).startswith("queued")]
        check(again == ids, "AC-11: no duplicate on later exchanges")
        check(relay_mod.peer_view(w.root_a, "b")["ok"], "state returns to ok")
    finally:
        w.close()


def test_one_way_connectivity():
    w = World()
    try:
        relay_mod.update_peers(w.root_a, lambda p: {"b": {"port": free_port()}})
        w.ra._peers.clear()
        res = tools_mod.send_dm(w.root_a, None, "alice", "bob@b", "pulled by b")
        res_b = tools_mod.send_dm(w.root_b, None, "bob", "alice@a", "pushed by b")
        w.pump(4)
        check([m["id"] for m in unread(w.root_b, "bob") if m.get("message") == "pulled by b"] == [res["id"]],
              "AC-12: A->B delivers over B's connection")
        check([m["id"] for m in unread(w.root_a, "alice") if m.get("message") == "pushed by b"] == [res_b["id"]],
              "AC-12: B->A delivers")
        w.pump(2)
        check(relay_mod.queued_count(w.root_a, "b") == 0, "AC-12: A's outbox drained after B's ack")
        check(relay_mod.peer_view(w.root_a, "b")["ok"], "AC-12: A reports b reachable")
    finally:
        w.close()


def test_notices():
    w = World()
    try:
        w.rb._close_listener()
        check(wait_closed(IPS["b"], w.port_b), "setup: B's listener closed")
        tools_mod.send_dm(w.root_a, None, "alice", "bob@b", "will be late")
        w.pump(2, step=10, b=False)
        check(not [m for m in unread(w.root_a, "alice") if m.get("from") == comms.RELAY_SENDER],
              "AC-13: no notice before the threshold")
        w.pump(3, step=30, b=False)
        notes = [m for m in unread(w.root_a, "alice") if m.get("from") == comms.RELAY_SENDER]
        check(len(notes) == 1 and "NOT DELIVERED" in notes[0]["message"],
              f"AC-13: exactly one undelivered notice: {[n['message'][:40] for n in notes]}")
        w.pump(3, step=30, b=False)
        check(len([m for m in unread(w.root_a, "alice") if m.get("from") == comms.RELAY_SENDER]) == 1,
              "AC-13: the notice is not repeated during the same outage")
        w.pump(4, step=40)
        notes = [m for m in unread(w.root_a, "alice") if m.get("from") == comms.RELAY_SENDER]
        check(len(notes) == 2 and "reachable again" in notes[1]["message"],
              f"AC-13: one recovery notice: {[n['message'][:40] for n in notes]}")
        check([m for m in unread(w.root_b, "bob") if m.get("message") == "will be late"],
              "AC-13: the late message was delivered")
    finally:
        w.close()


def test_roster_and_status():
    w = World()
    try:
        w.pump(2, step=11)
        listing = tools_mod._handle_list({"all": True}, ctx_for("alice", w.root_a))
        check("b: reachable" in listing and "bob@b: type=full, channel=live" in listing,
              f"roster of b shown in teammate_list: {listing[-400:]}")
        prof = tools_mod._handle_profile({"agent": "bob@b"}, ctx_for("alice", w.root_a))
        check("Profile: bob@b" in prof and "demo/app" in prof, "remote profile from the roster")
        res = tools_mod._handle_send({"to": "bob@b", "message": "status line"}, ctx_for("alice", w.root_a))
        check("bob is live on b" in res, f"send result names the live recipient: {res}")
        res = tools_mod._handle_send({"to": "nobody@b", "message": "typo"}, ctx_for("alice", w.root_a))
        check("no registered teammate named 'nobody'" in res, f"unknown remote name is flagged: {res}")
        peers = tools_mod._handle_peers({}, ctx_for("alice", w.root_a))
        check("b: reachable" in peers, f"teammate_peers list: {peers}")

        w.net_a.state = "down"
        w.pump(1, b=False)
        check(relay_mod.peer_view(w.root_a, "b")["state"] == "tailscale_down", "Tailscale down is its own state")
        check(w.ra._server is None, "no listener without Tailscale")
        res = tools_mod._handle_send({"to": "bob@b", "message": "while down"}, ctx_for("alice", w.root_a))
        check("NOT DELIVERED" in res and "Tailscale is not connected" in res, f"send warns about Tailscale: {res}")

        check_raises(lambda: tools_mod._handle_peers({"action": "add", "machine": "Bad_Name"},
                                                     ctx_for("alice", w.root_a)), CommsError, "bad machine name")
        check("c" not in relay_mod.read_peers(w.root_a), "a bad name approved nothing")
        out = tools_mod._handle_peers({"action": "add", "machine": "c"}, ctx_for("alice", w.root_a))
        check("c" in relay_mod.read_peers(w.root_a) and "Approved 'c'" in out,
              f"the tool approves a peer: {out}")
        tools_mod._handle_peers({"action": "remove", "machine": "c"}, ctx_for("alice", w.root_a))
        check_raises(lambda: tools_mod._handle_group({"action": "join", "group": "nowhere"},
                                                     ctx_for("alice", w.root_a)), CommsError,
                     "joining a group that is not on this machine is a clear error")
        check(peers_mod.main(["add", "c", "--comms-dir", str(w.root_a)], net=w.net_a) == 0
              and "c" in relay_mod.read_peers(w.root_a), "the user command approves a peer")
        check(peers_mod.main(["add", "Bad_Name", "--comms-dir", str(w.root_a)], net=w.net_a) == 1,
              "the user command rejects a bad name")
        check(peers_mod.main(["add", "a", "--comms-dir", str(w.root_a)], net=w.net_a) == 1,
              "the user command rejects this machine's own name")
        check(peers_mod.main(["remove", "c", "--comms-dir", str(w.root_a)], net=w.net_a) == 0
              and "c" not in relay_mod.read_peers(w.root_a), "the user command removes a peer")
        tools_mod._handle_peers({"action": "remove", "machine": "b"}, ctx_for("alice", w.root_a))
        check("b" not in relay_mod.read_peers(w.root_a) and relay_mod.queued_count(w.root_a, "b") == 0,
              "remove drops the peer and its queue")
    finally:
        w.close()


def test_status_before_first_exchange():
    w = World()
    try:
        seen = {}
        real = w.ra._exchange

        def spy(machine, cfg, st):
            seen["published"] = relay_mod.status_file(w.root_a).exists()
            return real(machine, cfg, st)
        w.ra._exchange = spy
        relay_mod.status_file(w.root_a).unlink()
        w.ra._status_last = None
        w.ra._peers["b"]["next_due"] = 0
        w.pump(1, b=False)
        check(seen.get("published") is True,
              f"the relay publishes its status before it tries a peer: {seen}")
    finally:
        w.close()


def test_utc_ids_and_send_time():
    from datetime import datetime, timezone

    def stamp(epoch):
        return datetime.fromtimestamp(epoch, timezone.utc).strftime(comms.TIMESTAMP_FMT)

    t0 = 1_800_000_000.0
    clock = {"now": t0}
    real_clock, real_local = comms._id_clock, comms._local_as_utc_epoch
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    comms._id_clock = lambda: clock["now"]
    try:
        check(comms.new_message_id().startswith(stamp(t0) + "."), "an id carries the UTC time")
        check(comms.new_message_id(root).startswith(stamp(t0) + "."),
              "a root with no comms tree yet gets plain UTC ids")
        check(not (root / "TeammateComms").exists(), "minting an id does not create the comms tree")

        (root / "TeammateComms").mkdir()
        comms._local_as_utc_epoch = lambda e: e + 7200
        comms._id_floors.clear()
        last_local_id = stamp(t0 + 7200 - 1) + ".ffff"
        ids = []
        for step in (0, 0.5, 60, 3600, 7199, 7200, 7201, 7272, 7273, 7300, 9000):
            clock["now"] = t0 + step
            ids.append(comms.new_message_id(root))
        check(ids[0] > last_local_id, "east of UTC, the first UTC-era id sorts above existing local ids")
        check(all(a[:26] < b[:26] for a, b in zip(ids, ids[1:])),
              f"ids keep increasing across the hand-over to plain UTC: {[i[11:26] for i in ids]}")
        check(ids[-1].startswith(stamp(t0 + 9000) + "."), "ids return to plain UTC once it passes the floor")
        clock["now"] = t0 - 500
        check(comms.new_message_id(root)[:26] >= stamp(t0 + 7200),
              "a clock set backwards never yields an id below the floor")
        floor = json.loads((root / "TeammateComms" / comms.ID_FLOOR_FILE).read_text(encoding="utf-8"))
        check(floor == {"floor": t0 + 7200, "created": t0}, f"the floor is stored once: {floor}")

        comms._id_floors.clear()
        clock["now"] = t0 + 100
        check(comms.new_message_id(root).startswith(stamp(t0 + 7200 + 1) + "."),
              "another process reads the same floor instead of making its own")
        (root / "TeammateComms" / comms.ID_FLOOR_FILE).write_text("garbage", encoding="utf-8")
        comms._id_floors.clear()
        first = comms.new_message_id(root)
        check(first[:26] >= stamp(t0 + 7200 + 100), "a corrupt floor file is replaced by a higher floor")
        repaired = json.loads((root / "TeammateComms" / comms.ID_FLOOR_FILE).read_text(encoding="utf-8"))
        check(repaired == {"floor": t0 + 100 + 7200, "created": t0 + 100},
              f"the corrupt floor file is repaired on disk: {repaired}")
        comms._id_floors.clear()
        clock["now"] = t0 + 300
        check(comms.new_message_id(root).startswith(stamp(t0 + 100 + 7200 + 2) + "."),
              "a later process uses the repaired floor, not one of its own")
        leftovers = [p.name for p in (root / "TeammateComms").iterdir() if p.name.endswith(".tmp")]
        check(not leftovers, f"no temp files are left behind: {leftovers}")
        clock["now"] = t0 + 100
        e = 1_700_000_000.0
        check(real_local(e) == e + datetime.fromtimestamp(e).astimezone().utcoffset().total_seconds(),
              "the local-clock reading matches this machine's UTC offset")
        (root / "TeammateComms" / comms.MIGRATED_MARKER).write_text("{}", encoding="utf-8")
        check(comms.legacy_repopulated_entries(root) == [],
              "the floor file is not mistaken for an old session writing to a migrated root")
        (root / "TeammateComms" / comms.MIGRATED_MARKER).unlink()

        comms._local_as_utc_epoch = lambda e: e - 6 * 3600
        comms._id_floors.clear()
        (root / "TeammateComms" / comms.ID_FLOOR_FILE).unlink()
        check(comms.new_message_id(root).startswith(stamp(t0 + 100) + "."),
              "west of UTC the floor never changes an id")

        src = (REPO / "src" / "teammate_comms")
        import re
        bare = [p.name for p in src.glob("*.py")
                if re.search(r"(?<!def )new_message_id\(\s*(None)?\s*\)", p.read_text(encoding="utf-8"))]
        check(not bare, f"every id minted in the package passes the comms root: {bare}")

        clock["now"] = t0 + 200
        add_agent(root, "ann")
        add_agent(root, "ben")
        sent = tools_mod.send_dm(root, None, "ann", "ben", "timed")
        got = [m for m in unread(root, "ben") if m["id"] == sent["id"]]
        check(got and got[0].get("ts") == t0 + 200, f"a direct message carries its send time: {got}")
        tools_mod._handle_group({"action": "create", "group": "tz", "members": ["ben"]}, ctx_for("ann", root))
        post = tools_mod.send_group(root, None, "ann", "#tz", "timed post")
        got = [m for m in unread(root, "ben") if m["id"] == post["id"]]
        check(got and got[0].get("ts") == t0 + 200, "a group post carries its send time")
        block = "\n".join(tools_mod._format_message_block(got[0]))
        check(f"| sent: {comms.local_time_text(t0 + 200)} ---" in block and comms.local_time_text(t0 + 200),
              f"the message block shows the send time in local time: {block[:160]}")
        check("sent:" not in "\n".join(tools_mod._format_message_block({"id": "x", "from": "y", "message": "z"})),
              "a message without a send time shows none")

        for bad in ("soon", True, -5, 1e12, float("nan")):
            rec_id = comms.new_message_id()
            relay_mod.apply_event(root, "b", "a", "dm", {"to": "ben@b", "record": {
                "id": rec_id, "from": "zed", "priority": "normal", "message": "bad ts", "ts": bad}})
            got = [m for m in unread(root, "ben") if m["id"] == rec_id]
            check(got and "ts" not in got[0], f"a malformed send time ({bad!r}) is dropped, the message kept")
        rec_id = comms.new_message_id()
        relay_mod.apply_event(root, "b", "a", "dm", {"to": "ben@b", "record": {
            "id": rec_id, "from": "zed", "priority": "normal", "message": "good ts", "ts": t0 + 5.123456}})
        got = [m for m in unread(root, "ben") if m["id"] == rec_id]
        check(got and got[0].get("ts") == round(t0 + 5.123456, 3), "a relayed message keeps its send time")
    finally:
        comms._id_clock, comms._local_as_utc_epoch = real_clock, real_local
        comms._id_floors.clear()
        try:
            tmp.cleanup()
        except OSError:
            pass


def test_tailscale_parsing():
    net = relay_mod.TailscaleNet()
    net._at = time.monotonic() + 3600
    net._data = {
        "BackendState": "Running", "MagicDNSSuffix": "tail1.ts.net",
        "Self": {"DNSName": "Desk-Top.tail1.ts.net.", "HostName": "DESK", "TailscaleIPs": ["fd7a::1", "100.64.0.1"]},
        "Peer": {
            "k1": {"DNSName": "laptop.tail1.ts.net.", "TailscaleIPs": ["100.64.0.2", "fd7a::2"], "Online": True},
            "k2": {"DNSName": "laptop.other.ts.net.", "TailscaleIPs": ["100.99.0.9"], "Online": True},
            "k3": {"DNSName": "shared.tail1.ts.net.", "TailscaleIPs": ["100.64.0.7"], "ShareeNode": True},
            "k4": {"DNSName": "old.tail1.ts.net.", "TailscaleIPs": ["100.64.0.4"], "Online": False, "Expired": True},
        },
    }
    me = net.self_info()
    check(me == {"state": "running", "detail": "", "machine": "desk-top", "ip": "100.64.0.1"}, f"self parsed: {me}")
    check(net.devices() == ["laptop", "old"], f"only same-tailnet, unshared devices: {net.devices()}")
    check(net.identify("100.64.0.2") == "laptop" and net.identify("fd7a::2") == "laptop", "identify by address")
    check(net.identify("100.99.0.9") is None, "a same-named device from another tailnet is not identified")
    check(net.identify("100.64.0.7") is None, "a shared-in device is not identified")
    check(net.peer_info("old")["expired"] is True and net.peer_info("old")["online"] is False, "expired flag read")
    net._data = {**net._data, "BackendState": "NeedsLogin"}
    check(net.self_info()["state"] == "down" and "NeedsLogin" in net.self_info()["detail"], "logged-out is down")
    net._data = None
    net._error = "Tailscale is not installed"
    check(net.self_info()["state"] == "down" and net.devices() == [], "no Tailscale is down, no devices")


def test_lifecycle_guards():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        check(relay_mod.ensure_relay(root) == "no_peers", "no peers, no relay")
        relay_mod.update_peers(root, lambda p: {"b": {}})
        check(relay_mod.ensure_relay(root) == "disabled", "autostart disabled by env in tests")
        check(not relay_mod.relay_alive(root), "nothing running")
    check_raises(lambda: server_mod.register_identity("Comms-Relay", None, None), CommsError,
                 "the relay's sender name is reserved")


def main():
    sections = [
        ("names and mentions (AC-8)", test_names_and_mentions),
        ("DM round trip (AC-1, AC-2)", test_dm_round_trip),
        ("unapproved machine and team namespace (AC-3)", test_unapproved_and_team),
        ("connection authorization (AC-4)", test_auth),
        ("trust rules, id collision, retry policy (AC-5, AC-10, AC-11)", test_trust_rules),
        ("groups (AC-6, AC-7, AC-8)", test_groups),
        ("implementation-review findings (R1-R3)", test_review_findings),
        ("reactions and deletions (AC-9, AC-10)", test_reactions_and_deletions),
        ("queue while the peer is down (AC-11)", test_queue_while_down),
        ("one-way connectivity (AC-12)", test_one_way_connectivity),
        ("undelivered and recovery notices (AC-13)", test_notices),
        ("roster, status and peers tool", test_roster_and_status),
        ("Tailscale status parsing", test_tailscale_parsing),
        ("lifecycle guards", test_lifecycle_guards),
        ("UTC ids, switchover floor and send time", test_utc_ids_and_send_time),
        ("status published before the first exchange", test_status_before_first_exchange),
    ]
    for label, fn in sections:
        n_before = len(failures)
        print(f"  {label}... ", end="", flush=True)
        try:
            fn()
        except Exception as e:
            import traceback
            failures.append(f"{label} CRASHED: {type(e).__name__}: {e}")
            traceback.print_exc()
        print("ok" if len(failures) == n_before else f"FAIL ({len(failures) - n_before} new)")

    print()
    if failures:
        print(f"FAIL ({len(failures)} total failure(s)):")
        for f in failures:
            print("  -", f[:600])
        sys.exit(1)
    print("ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
