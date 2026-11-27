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


def main():
    tests = [("review fixes", test_review_fixes), ("delivery", test_delivery), ("refused records", test_refused),
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
