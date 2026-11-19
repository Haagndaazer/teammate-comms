"""End to end: the real relay code against the real Worker under `wrangler dev`.

    python cloud-mailbox-worker/test/integration.py

Needs Node and network access to fetch wrangler. Not part of the default test run.
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE.parent))
os.environ["TEAMMATE_RELAY_AUTOSTART"] = "0"

import make_config  # noqa: E402
from teammate_comms import comms  # noqa: E402
from teammate_comms import mailbox as mailbox_mod  # noqa: E402
from teammate_comms import relay as relay_mod  # noqa: E402
from teammate_comms import tools as tools_mod  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok    " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


class NoTailscale:
    def refresh(self):
        pass

    def self_info(self):
        return {"state": "down", "detail": "not installed", "machine": None, "ip": None}

    def peer_info(self, machine):
        return None

    def identify(self, ip):
        return None

    def devices(self):
        return []


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(port, method, path, key=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if key:
        headers["Authorization"] = "Bearer " + key
    conn.request(method, path, body=None if body is None else json.dumps(body).encode("utf-8"),
                 headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    try:
        return resp.status, json.loads(raw) if raw else None
    except ValueError:
        return resp.status, raw


def main():
    work = Path(tempfile.mkdtemp(prefix="mailbox-it-"))
    secrets_dir, worker, root = work / "secrets", work / "worker", work / "comms"
    shutil.copytree(HERE.parent / "src", worker / "src")
    shutil.copy(HERE.parent / "wrangler.jsonc", worker / "wrangler.jsonc")
    make_config.main(["--out", str(secrets_dir), "--machine", "desk", "--bot", "Grok", "--bot", "Bell"])
    config = json.loads((secrets_dir / "config.json").read_text(encoding="utf-8"))
    config["bots"]["Bell"].update(doorbell_url="https://127.0.0.1:9/ring", doorbell_key="ring-key")
    (secrets_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    make_config.main(["--out", str(secrets_dir), "--rebuild"])
    upload = json.loads((secrets_dir / "wrangler-secrets.json").read_text(encoding="utf-8"))
    (worker / ".dev.vars").write_text(
        "MAILBOX_CONFIG='" + upload["MAILBOX_CONFIG"] + "'\nDOORBELL_MIN_SECONDS=20\n", encoding="utf-8")
    bot_key = config["bots"]["Grok"]["key"]
    port = free_port()
    npx = shutil.which("npx") or "npx"
    log = open(work / "wrangler.log", "w", encoding="utf-8")
    proc = subprocess.Popen([npx, "--yes", "wrangler@4", "dev", "--port", str(port), "--ip", "127.0.0.1"],
                            cwd=worker, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    try:
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                if call(port, "POST", "/nothing")[0] == 404:
                    break
            except OSError:
                time.sleep(1)
        else:
            print((work / "wrangler.log").read_text(encoding="utf-8", errors="replace")[-3000:])
            raise SystemExit("wrangler dev did not start")

        check(call(port, "GET", "/.well-known/oauth-protected-resource")[0] == 404, "discovery probe is a plain 404")
        check(call(port, "POST", "/mcp", body={})[0] == 401, "no key is refused on /mcp")
        check(call(port, "POST", "/pc/sync", key=bot_key, body={"v": 1})[0] == 401, "a bot key is refused on the machine endpoint")
        check(call(port, "POST", "/mcp", key=config["machines"]["desk"], body={})[0] == 401, "a machine key is refused on the bot endpoint")
        check(call(port, "GET", "/mcp", key=bot_key)[0] == 405, "GET /mcp with a key is 405")

        mailbox_mod.ALLOW_INSECURE = True
        for name in ("alice",):
            comms.ensure_inbox(comms.get_inboxes_dir(root, None), name)
            comms.write_agent_record(root, None, name, type="full", channel=True, pid=os.getpid(),
                                     host=socket.gethostname(), lastHeartbeat=comms.now_timestamp())
        relay = relay_mod.Relay(root, net=NoTailscale(), port=1)
        text = relay_mod.link_mailbox(root, "cloudbot", f"http://127.0.0.1:{port}",
                                      str(secrets_dir / "machine-desk.key"), net=NoTailscale())
        check("Grok@cloudbot" in text and "Bell@cloudbot" in text, "linked; the mailbox reports its cloud agents")

        def rpc(method, params=None, rid=1):
            body = {"jsonrpc": "2.0", "method": method}
            if rid is not None:
                body["id"] = rid
            if params is not None:
                body["params"] = params
            return call(port, "POST", "/mcp", key=bot_key, body=body)

        def tool(name, args=None):
            status, reply = rpc("tools/call", {"name": name, "arguments": args or {}})
            out = reply["result"]
            return (out["content"][0]["text"] if out.get("isError") else json.loads(out["content"][0]["text"]))

        status, init = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                          "clientInfo": {"name": "it", "version": "0"}})
        check(status == 200 and init["result"]["protocolVersion"] == "2025-11-25", "MCP initialize")
        check(rpc("notifications/initialized", rid=None)[0] == 202, "notification answers 202")
        check([t["name"] for t in rpc("tools/list")[1]["result"]["tools"]]
              == ["mail_check", "mail_send", "mail_teammates"], "three tools listed")

        big = ("\U0001F600" * (tools_mod.MAX_MESSAGE_CHARS // 2 - 20)) + " end-marker-quartz"
        sent = tools_mod.send_dm(root, None, "alice", "Grok@cloudbot", big)
        tools_mod.send_dm(root, None, "alice", "Bell@cloudbot", "ring please")
        relay.mailbox_tick()
        check(relay_mod.queued_count(root, "cloudbot") == 0, "outbox drained into the real mailbox")
        mates = tool("mail_teammates")
        check(mates["machines"][0]["teammates"] == [{"address": "alice@desk", "running": True}],
              f"the bot sees the roster: {mates}")
        got = tool("mail_check")
        check(got["count"] == 1 and got["messages"][0]["message"] == big
              and got["messages"][0]["from"] == "alice@desk" and got["messages"][0]["id"] == sent["id"],
              "a full-size message of 4-byte characters arrives exact")
        check(tool("mail_check")["previous_batch"]["count"] == 1, "an empty check points at the batch just handed over")

        reply = tool("mail_send", {"to": "alice", "message": "hello from the cloud", "reply_to": sent["id"]})
        check(reply["to"] == "alice@desk", f"bare name resolved by the roster: {reply}")
        check("no teammate named ghost" in tool("mail_send", {"to": "ghost@desk", "message": "x"}),
              "an unknown recipient is refused at once")
        relay._peer("cloudbot")["next_due"] = 0
        relay.mailbox_tick()
        inbox = comms.read_json_readonly(comms.get_inboxes_dir(root, None) / "alice_unread.json") or []
        mine = [m for m in inbox if m.get("from") == "Grok@cloudbot"]
        check(len(mine) == 1 and mine[0]["message"] == mailbox_mod.CLOUD_BANNER + "\nhello from the cloud"
              and mine[0]["id"] == reply["sent"] and mine[0].get("reply_to") == sent["id"],
              f"the reply lands in alice's inbox with the banner: {mine}")
        st = relay._peer("cloudbot")
        st["next_due"] = 0
        relay.mailbox_tick()
        st["next_due"] = 0
        relay.mailbox_tick()
        status, sync = mailbox_mod.post_sync(f"http://127.0.0.1:{port}", config["machines"]["desk"], {"v": 1})
        check(sync["messages"] == [], "the reply is acked and gone from the mailbox")

        deadline = time.time() + 45
        bell = {}
        while time.time() < deadline and not bell.get("doorbell_error"):
            time.sleep(2)
            bell = next(b for b in mailbox_mod.post_sync(
                f"http://127.0.0.1:{port}", config["machines"]["desk"], {"v": 1})[1]["bots"] if b["name"] == "Bell")
        check("could not be reached" in bell.get("doorbell_error", ""),
              f"the alarm rang the doorbell in the real runtime and reported the failure: {bell}")
        check("ring-key" not in json.dumps(sync) and "ring-key" not in json.dumps(bell), "no doorbell key in any answer")
        relay.close()
    finally:
        proc.terminate()
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        log.close()
        time.sleep(1)
        shutil.rmtree(work, ignore_errors=True)
    print("\nALL CHECKS PASSED" if not failures else f"\n{len(failures)} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
