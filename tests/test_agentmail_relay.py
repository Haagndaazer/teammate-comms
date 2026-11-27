"""Agent email through the relay: delivery, attachments, the tool (docs/email-inbox-plan.md).

Run: uv run --no-sync python tests/test_agentmail_relay.py
"""
import hashlib
import os
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

os.environ["TEAMMATE_RELAY_AUTOSTART"] = "0"

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from teammate_comms import agentmail as am  # noqa: E402
import test_mailbox as tm  # noqa: E402

failures = tm.failures
check, call, add_agent, unread = tm.check, tm.call, tm.add_agent, tm.unread
ADDRESS = "alice-desk@agents.example.com"
ADDRESSES = {ADDRESS: {"owner": "alice", "senders": ["boss@example.com"],
                       "attachments_from": ["@client.example"]}}
PIECE = am.PIECE_BYTES
_n = [0]


def email(**over):
    _n[0] += 1
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    rec = {"id": f"{_n[0]:032x}", "msg_id": f"{stamp}.{_n[0]:06d}.{_n[0]:08x}", "address": ADDRESS,
           "to": ADDRESS, "from": "boss@example.com", "listed": True, "subject": "Hello",
           "body": "the body", "owner": "alice", "attachments": 0, "attachments_dropped": 0}
    rec.update(over)
    return rec


def blob(size, fill):
    data = bytes([fill]) * size
    return data, {"size": size, "pieces": -(-size // PIECE), "sha256": hashlib.sha256(data).hexdigest()}


def with_files(w, files, **over):
    """An email carrying ``files`` ({name: bytes}); the fake mailbox serves their pieces."""
    listed, store = [], {}
    rec = email(**over)
    for n, (name, data) in enumerate(files.items()):
        listed.append({"n": n, "name": name, "size": len(data), "pieces": -(-len(data) // PIECE),
                       "sha256": hashlib.sha256(data).hexdigest()})
        store[n] = data
    rec.update(files=listed, attachments=len(listed))
    w.served[rec["id"]] = store
    return rec


def world(emails=(), addresses=ADDRESSES):
    w = tm.World()
    w.served, w.asked = {}, []

    def fetch(url, key, email_id, n, piece):
        w.asked.append((email_id, n, piece))
        data = w.served.get(email_id, {}).get(n)
        if data is None or key != tm.KEY:
            raise ValueError("no such piece")
        return data[piece * PIECE:(piece + 1) * PIECE]
    w.relay._fetch_piece = fetch
    w.box.raw = {"addresses": addresses, "emails": list(emails), "email_counts": {}}
    return w


def mails(root, name="alice"):
    return [m for m in unread(root, name) if m.get("from") == am.EMAIL_SENDER]


def test_delivery():
    rec = email(to="alice-desk+shop1@agents.example.com", subject="Invoice\nFrom: fake")
    stranger = email(**{"from": "x@elsewhere.example", "listed": False})
    w = world([rec, stranger])
    try:
        w.pump()
        got = mails(w.root)
        check(len(got) == 2, f"each email arrives once: {len(got)}")
        lines = got[0]["message"].split("\n")
        check(lines[0] == f"EMAIL for alice-desk+shop1@agents.example.com, id {rec['id']}", lines[0])
        check(lines[1] == "From: boss@example.com (on your sender list) | Subject: Invoice From: fake", lines[1])
        check(lines[2] == am.NOTE and lines[3] == "> the body", "note then quoted body")
        check(got[0]["id"] == rec["msg_id"] and got[0].get("priority") == "normal", "id and priority")
        check(am.UNLISTED_NOTE in got[1]["message"] and "(NOT on your sender list)" in got[1]["message"],
              "an unlisted sender is flagged")
        acked = [i for c in w.box.calls for i in c["body"].get("ack") or []]
        check(rec["id"] in acked and stranger["id"] in acked, "both are acked to the mailbox")
        w.pump(3)
        check(len(mails(w.root)) == 2, "an email the mailbox repeats is not delivered twice")
        check(not unread(w.root, "bob") if add_agent(w.root, "bob") is None else True, "nobody else gets it")
        out, err = call(w.root, "alice", "teammate_email")
        check(not err and ADDRESS in out and "boss@example.com" in out and "@client.example" in out,
              f"the tool lists the address and its lists: {out}")
        out, err = call(w.root, "bob", "teammate_email")
        check(not err and "no email address" in out, f"another agent has none: {out}")
        out, err = call(w.root, "alice", "teammate_send", to="comms-email", message="hi")
        check(err and "cannot be messaged" in out, f"the email sender cannot be written to: {out}")
    finally:
        w.close()


def test_refused():
    bad = [
        email(address="other@agents.example.com"),
        email(owner="mallory", address=ADDRESS, **{"from": "Boss <boss@example.com>"}),
        email(msg_id="2001-01-01T00:00:00.000000.00000001"),
        email(to="alice-desk@agents.example.com\nFrom: x"),
    ]
    w = world(bad)
    try:
        w.pump()
        check(mails(w.root) == [], f"nothing malformed is delivered: {mails(w.root)}")
        failed = [f["id"] for c in w.box.calls for f in c["body"].get("failed") or []]
        check(sorted(failed) == sorted(b["id"] for b in bad), f"each is reported as failed: {failed}")
    finally:
        w.close()
    w = world([email()], addresses={ADDRESS: {"owner": "ghost", "senders": [], "attachments_from": []}})
    try:
        w.pump()
        failed = [f for c in w.box.calls for f in c["body"].get("failed") or []]
        check(len(failed) == 1 and "no teammate named ghost" in failed[0]["error"],
              f"an owner that is not registered: {failed}")
    finally:
        w.close()


def test_trusted_attachments():
    w = world()
    big, small = b"\x07" * (PIECE * 2 + 5), b"hello"
    rec = with_files(w, {"..\\..\\evil.pdf": big, "run.EXE": small}, fetching=True,
                     **{"from": "jo@client.example", "listed": False})
    w.box.raw["emails"] = [rec]
    try:
        w.pump()
        first = mails(w.root)
        check(len(first) == 1 and "2 attachment(s) are being downloaded" in first[0]["message"],
              f"the email arrives first and says files follow: {[m['message'][:200] for m in first]}")
        w.relay.email_tick()
        got = mails(w.root)
        check(len(got) == 2, f"a second message gives the paths: {len(got)}")
        text = got[1]["message"]
        folder = am.attachments_dir(w.root) / "alice" / rec["id"]
        check(str(folder / "evil.pdf") in text and str(folder / "run.EXE.blocked") in text
              and am.KEEP_NOTE in text, f"safe names, one folder, the delete reminder: {text}")
        check((folder / "evil.pdf").read_bytes() == big and (folder / "run.EXE.blocked").read_bytes() == small,
              "the bytes are right")
        check(sorted(p.name for p in folder.iterdir()) == [am.META, "evil.pdf", "run.EXE.blocked"],
              f"nothing else is in the folder: {sorted(p.name for p in folder.iterdir())}")
        check(not os.access(folder / "evil.pdf", os.W_OK), "saved files are read-only")
        if os.name == "nt":
            mark = open(f"{folder / 'evil.pdf'}:Zone.Identifier", "rb").read()
            check(mark == b"[ZoneTransfer]\r\nZoneId=3\r\n", f"marked as downloaded from the internet: {mark!r}")
        check(not list((am.email_dir(w.root) / "held").glob("*.json")), "nothing is left held")
        w.pump()
        done = [i for c in w.box.calls for i in c["body"].get("attachments_done") or []]
        check(rec["id"] in done, f"the mailbox is told it can delete its copy: {done}")
        w.pump()
        check(am.done_ids(w.root, "cloudbot") == [], "and is told only until it has heard")
        out, err = call(w.root, "bob", "teammate_email", action="delete_attachments", id=rec["id"])
        check(err, "another agent cannot delete them")
        out, err = call(w.root, "alice", "teammate_email", action="delete_attachments", id=rec["id"])
        check(not err and not folder.exists(), f"the owner deletes them: {out}")
        out, err = call(w.root, "alice", "teammate_email", action="delete_attachments", id="../../x")
        check(err and "email id" in out, f"a bad id is refused: {out}")
    finally:
        w.close()


def test_held_then_fetched():
    w = world()
    data = b"\x01" * 100
    rec = with_files(w, {"report.pdf": data})
    w.box.raw["emails"] = [rec]
    add_agent(w.root, "bob")
    try:
        w.pump()
        w.relay.email_tick()
        got = mails(w.root)
        check(len(got) == 1 and "1 attachment(s) held, not downloaded." in got[0]["message"], "held by default")
        check(w.asked == [], "nothing is downloaded without approval")
        out, err = call(w.root, "bob", "teammate_email", action="fetch_attachments", id=rec["id"])
        check(err and "belong to you" in out, f"another agent cannot fetch it: {out}")
        out, err = call(w.root, "alice", "teammate_email", action="fetch_attachments", id="nope")
        check(err, "a bad id is refused")
        out, err = call(w.root, "alice", "teammate_email", action="fetch_attachments", id=rec["id"])
        check(not err and "1 attachment(s)" in out, f"the owner asks for it: {out}")
        w.relay.email_tick()
        folder = am.attachments_dir(w.root) / "alice" / rec["id"]
        check((folder / "report.pdf").read_bytes() == data, "then it is downloaded")
        check(len(mails(w.root)) == 2 and unread(w.root, "bob") == [], "and only the owner is told")
    finally:
        w.close()


def test_bad_downloads():
    w = world()
    good = b"\x02" * 50
    rec = with_files(w, {"a.pdf": good}, fetching=True)
    w.served[rec["id"]][0] = b"\x03" * 50
    w.box.raw["emails"] = [rec]
    try:
        w.pump()
        for _ in range(am.MAX_TRIES):
            w.relay.email_tick()
        folder = am.attachments_dir(w.root) / "alice" / rec["id"]
        check(not (folder / "a.pdf").exists() and not list(folder.glob("*.partial")),
              "a file with the wrong checksum is not kept")
        last = mails(w.root)[-1]["message"]
        check("could not be downloaded" in last and len(mails(w.root)) == 2, f"the owner is told once: {last}")
        w.relay.email_tick()
        check(len(mails(w.root)) == 2, "and it stops trying")
        w.served[rec["id"]][0] = good
        call(w.root, "alice", "teammate_email", action="fetch_attachments", id=rec["id"])
        w.relay.email_tick()
        check((folder / "a.pdf").read_bytes() == good, "asking again works once the file is right")
    finally:
        w.close()
    w = world()
    bad = email(files=[{"n": 0, "name": "a.pdf", "size": 10, "pieces": 3, "sha256": "0" * 64}], attachments=1,
                fetching=True)
    huge = email(files=[{"n": 0, "name": "a.pdf", "size": am.FILE_BYTES + 1, "pieces": 7, "sha256": "0" * 64}],
                 attachments=1, fetching=True)
    w.box.raw["emails"] = [bad, huge]
    try:
        w.pump()
        got = mails(w.root)
        check(len(got) == 2 and all("1 attachment(s) were too large or too many" in m["message"] for m in got),
              f"a file list that does not add up is dropped, the email still arrives: {[m['message'][:300] for m in got]}")
        check(not (am.email_dir(w.root) / "held").exists() or not list((am.email_dir(w.root) / "held").iterdir()),
              "and nothing is held")
    finally:
        w.close()


def test_sweep():
    w = world()
    rec = with_files(w, {"a.pdf": b"x" * 10}, fetching=True)
    w.box.raw["emails"] = [rec]
    try:
        w.pump()
        w.relay.email_tick()
        folder = am.attachments_dir(w.root) / "alice" / rec["id"]
        stray = am.attachments_dir(w.root) / "alice" / "kept-by-user"
        stray.mkdir()
        (stray / "mine.txt").write_text("keep", encoding="utf-8")
        check(am.sweep(w.root, time.time() + am.KEEP_SECONDS - 60) == 0 and folder.exists(), "kept for 14 days")
        check(am.sweep(w.root, time.time() + am.KEEP_SECONDS + 60) == 1 and not folder.exists(), "then removed")
        check((stray / "mine.txt").exists(), "a folder the relay did not make is left alone")
    finally:
        w.close()


def test_review_fixes():
    import http.client
    w = world()
    first = with_files(w, {"a.pdf": b"a" * 10, "b.pdf": b"b" * 10}, fetching=True)
    second = with_files(w, {"c.pdf": b"c" * 10}, fetching=True)
    w.box.raw["emails"] = [first, second]
    real = w.relay._fetch_piece

    def flaky(url, key, email_id, n, piece):
        if email_id == first["id"] and n == 1:
            raise http.client.IncompleteRead(b"")
        return real(url, key, email_id, n, piece)
    w.relay._fetch_piece = flaky
    try:
        w.pump()
        w.relay.email_tick()
        other = am.attachments_dir(w.root) / "alice" / second["id"]
        check((other / "c.pdf").exists(), "an odd network error on one email does not stop the next")
        for _ in range(am.MAX_TRIES):
            w.relay.email_tick()
        folder = am.attachments_dir(w.root) / "alice" / first["id"]
        check((folder / "a.pdf").exists() and (folder / am.META).exists(),
              "a half-downloaded folder carries the relay's record")
        check(am.sweep(w.root, time.time() + am.KEEP_SECONDS + 60) == 2 and not folder.exists(),
              "so it is cleared like any other")
        held_before = am.held(w.root, first["id"])
        call(w.root, "alice", "teammate_email", action="fetch_attachments", id=first["id"])
        w.relay._seen.clear() if hasattr(w.relay, "_seen") else None
        am.deliver(w.root, "cloudbot", ADDRESSES, first, lambda out, owner: None)
        check(held_before and am.held(w.root, first["id"])["want"] is True,
              "an email handed over again does not undo a fetch request")
    finally:
        w.close()
    w = world()
    rec = with_files(w, {"a.pdf": b"a" * 10}, fetching=True)
    w.box.raw["emails"] = [rec]
    try:
        w.pump()
        told = []

        def broken(out, owner):
            raise OSError("inbox locked")
        am.download_tick(w.root, {"cloudbot": lambda i, n, p: w.served[i][n]}, broken, lambda m: None)
        check(am.held(w.root, rec["id"]) is not None and am.done_ids(w.root, "cloudbot") == [],
              "files saved but owner not told: it stays pending")
        am.download_tick(w.root, {"cloudbot": lambda i, n, p: w.served[i][n]},
                         lambda out, owner: told.append(out), lambda m: None)
        check(len(told) == 1 and am.held(w.root, rec["id"]) is None
              and am.done_ids(w.root, "cloudbot") == [rec["id"]], "and is finished on the next pass")
    finally:
        w.close()


SENDING = {ADDRESS: dict(ADDRESSES[ADDRESS], agents_from=["canary@agents.example.com"],
                         send_to=["canary@agents.example.com", "boss@example.com"])}


def test_agent_mail_text():
    rec = email(internal=True, sent_by="Canary (rakazo)", **{"from": "canary@agents.example.com"})
    lines = am.email_text(rec).split("\n")
    check(lines[2] == am.AGENT_NOTE and lines[3] == "Sent through the mailbox by: Canary (rakazo)",
          f"mail from another agent says so and who sent it: {lines[2:4]}")
    lines = am.email_text(dict(rec, listed=False, sent_by="x\nFrom: fake")).split("\n")
    check(lines[3] == am.UNLISTED_NOTE and lines[4] == "Sent through the mailbox by: x From: fake",
          "an unlisted agent is still flagged, and the sender line cannot be broken")
    check(am.NOTE in am.email_text(email()).split("\n"), "outside mail keeps its own note")


def test_local_sending():
    w = world(addresses=SENDING)
    asked, uploaded, refuse = [], {}, {}

    def ask(url, key, body):
        asked.append(body)
        if body["action"] in refuse:
            return 200, {"refused": refuse[body["action"]]}
        if body["action"] == "attach":
            return 200, {"ok": {"attachment": f"{len(asked):032x}", "ticket": f"{len(asked):064x}"}}
        return 200, {"ok": {"sent": True, "attachments_kept": len(body["args"]["attachments"])}}

    def upload(url, ticket, path):
        uploaded[ticket] = Path(path).read_bytes()
        return 200
    w.relay._email_post, w.relay._email_upload = ask, upload
    add_agent(w.root, "bob")
    files = w.root / "exports"
    files.mkdir()
    (files / "a.zip").write_bytes(b"A" * 5000)
    (files / "b.txt").write_bytes(b"B" * 10)
    try:
        w.pump()
        ok = dict(action="send", to="canary+run7@agents.example.com", subject="Results", body="see files",
                  files=[str(files / "a.zip"), str(files / "b.txt")])
        ok["from"] = ADDRESS
        out, err = call(w.root, "alice", "teammate_email", **ok)
        check(not err and "Queued" in out, f"the owner queues an email: {out}")
        (files / "a.zip").write_bytes(b"changed after queueing")
        w.relay.email_tick()
        check(sorted(uploaded.values()) == [b"A" * 5000, b"B" * 10], "the files as they were when queued are uploaded")
        attach = [b for b in asked if b["action"] == "attach"]
        check([a["args"]["name"] for a in attach] == ["a.zip", "b.txt"] and attach[0]["args"]["size"] == 5000
              and attach[0]["agent"] == "alice", f"each file is announced with its size and hash: {attach}")
        sent = [b for b in asked if b["action"] == "send"][0]["args"]
        check(sent["to"] == "canary+run7@agents.example.com" and sent["subject"] == "Results"
              and len(sent["attachments"]) == 2 and len(sent["request_id"]) == 32, f"then the email is sent: {sent}")
        last = mails(w.root)[-1]["message"]
        check("was sent, with 2 of 2 attachment(s)" in last, f"the owner is told: {last}")
        check(not list(am.outbox_dir(w.root).iterdir()), "nothing is left staged")

        for bad, why in ((dict(ok, to="stranger@elsewhere.example"), "may only write to"),
                         (dict(ok, **{"from": "other@agents.example.com"}), "your own addresses"),
                         (dict(ok, subject=" "), "subject"), (dict(ok, body=""), "body"),
                         (dict(ok, files=[str(files / "missing.zip")]), "not a regular file"),
                         (dict(ok, files=[str(files)]), "not a regular file"),
                         (dict(ok, files="a.zip"), "list of at most")):
            out, err = call(w.root, "alice", "teammate_email", **bad)
            check(err and why in out, f"refused ({why}): {out}")
        out, err = call(w.root, "bob", "teammate_email", **ok)
        check(err and "your own addresses" in out, f"another agent cannot send as alice: {out}")
        check(not list(am.outbox_dir(w.root).iterdir()), "a refused request stages nothing")

        before = len(asked)
        forged = am.queue_send(w.root, "alice", ADDRESS, "boss@example.com", "s", "b", [])
        request = am._read(am.outbox_dir(w.root) / f"{forged}.json", {})
        am.write_json_atomic(am.outbox_dir(w.root) / f"{forged}.json", dict(request, agent="bob"))
        w.relay.email_tick()
        check(len(asked) == before and not list(am.outbox_dir(w.root).iterdir()),
              "a request whose agent does not own the address is dropped unsent")

        refuse["send"] = "the daily sending limit is reached"
        call(w.root, "alice", "teammate_email", **dict(ok, files=[]))
        w.relay.email_tick()
        check("was not sent: the daily sending limit is reached" in mails(w.root)[-1]["message"],
              f"a refusal from the mailbox is passed on: {mails(w.root)[-1]['message']}")
        refuse.clear()

        def down(url, key, body):
            raise OSError("no network")
        w.relay._email_post = down
        call(w.root, "alice", "teammate_email", **dict(ok, files=[]))
        told = len(mails(w.root))
        w.relay.email_tick()
        check(len(mails(w.root)) == told and len(list(am.outbox_dir(w.root).glob("*.json"))) == 1,
              "while the mailbox is unreachable the request waits")
        am.send_tick(w.root, {"cloudbot": (lambda body: down(0, 0, body), None)}, lambda out, owner: None,
                     lambda m: None, now=time.time() + am.SEND_TRY_SECONDS + 5)
        check(not list(am.outbox_dir(w.root).iterdir()), "and is given up after an hour")
        out, err = call(w.root, "alice", "teammate_email")
        check("may send to: canary@agents.example.com, boss@example.com" in out, f"list shows who it may write to: {out}")

        w.relay._email_post = ask
        asked.clear()
        uploaded.clear()
        crashed = am.queue_send(w.root, "alice", ADDRESS, "boss@example.com", "s", "b", [str(files / "b.txt")])
        fail_send = {"on": True}

        def flaky(url, key, body):
            if body["action"] == "send" and fail_send["on"]:
                raise OSError("connection lost after the mailbox accepted it")
            return ask(url, key, body)
        w.relay._email_post = flaky
        w.relay.email_tick()
        fail_send["on"] = False
        w.relay.email_tick()
        attaches = [b for b in asked if b["action"] == "attach"]
        sends = [b for b in asked if b["action"] == "send"]
        check(len(attaches) == 1 and len(uploaded) == 1, f"a retry does not upload the file again: {len(attaches)}")
        check(len(sends) == 1 and sends[0]["args"]["request_id"] == crashed, "and repeats the send under the same request id")

        w.relay._email_post = ask
        for forged in ({"at": "soon"}, {"at": time.time() + 10 ** 6}, {"files": [{"staged": "../../secret", "size": 1, "sha256": "0" * 64, "name": "x"}]},
                       {"files": "nope"}, {"mailbox": "gone"}):
            rid = am.queue_send(w.root, "alice", ADDRESS, "boss@example.com", "s", "b", [])
            request = am._read(am.outbox_dir(w.root) / f"{rid}.json", {})
            am.write_json_atomic(am.outbox_dir(w.root) / f"{rid}.json", dict(request, **forged))
        before = len([b for b in asked if b["action"] == "attach"])
        w.relay.email_tick()
        check(len([b for b in asked if b["action"] == "attach"]) == before, "a forged file entry uploads nothing")
        am.send_tick(w.root, {"cloudbot": (lambda body: ask(0, 0, body), upload)}, lambda out, owner: None,
                     lambda m: None, now=time.time() + am.SEND_TRY_SECONDS + 5)
        check(not list(am.outbox_dir(w.root).iterdir()), f"damaged or forged requests never block the queue: {list(am.outbox_dir(w.root).iterdir())}")
    finally:
        w.close()


def main():
    tests = [("agent mail text", test_agent_mail_text), ("local sending", test_local_sending),
             ("review fixes", test_review_fixes), ("delivery", test_delivery), ("refused records", test_refused),
             ("trusted attachments", test_trusted_attachments),
             ("held then fetched", test_held_then_fetched), ("bad downloads", test_bad_downloads),
             ("sweep", test_sweep)]
    for label, fn in tests:
        before = len(failures)
        print(f"  {label}...", end=" ", flush=True)
        try:
            fn()
        except Exception as e:
            import traceback
            failures.append(f"{label} crashed: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        print("ok" if len(failures) == before else "FAILED")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
