"""A local, stdlib-only web console for teammate-comms (the ``teammate_dashboard`` tool).

Opens a Slack-style page in the browser showing all teammate messaging (group chats +
DMs) and a live roster, and lets the human operator participate as a first-class
teammate. Mirrors the *pattern* of vibe-cognition's dashboard (token-secured localhost
server in a background daemon thread, single-file frontend, browser auto-open) but is
implemented with pure ``http.server`` so teammate-comms keeps its zero-dependency rule.

Hard invariants:
- **stdout is the JSON-RPC stream.** This server writes ONLY to its own sockets and to
  stderr — never stdout. ``log_message``/``log_error`` and the server's ``handle_error``
  are all routed to ``_log`` (stderr). The browser is opened via ``os.startfile`` on
  Windows (no stdout inheritance).
- Loopback-only bind (127.0.0.1), per-launch random token, Host-header allowlist.
- The server lives in the hosting instance's process; it dies when that instance exits
  (``shutdown_dashboard`` is called from the stdio ``finally``).
"""

import json
import os
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Reject a POST body larger than this before reading it (audit D-3). A multi-MB body would be
# stored and re-served to every poller forever; the message-length cap (tools.MAX_MESSAGE_CHARS,
# 64 KB) sits well under this so an over-long message returns an informative 400, not this 413.
MAX_BODY_BYTES = 1024 * 1024

from . import tools as _tools
from .comms import (
    CommsError,
    _window,   # collision-safe burst-paging window, shared with the read_* helpers
    get_agents_dir,
    get_groups_dir,
    group_read_positions,
    is_channel_alive,
    read_agent_record,
    read_deletions,
    read_deletions_set,
    read_group_meta,
    read_reactions,
    read_transcript,
    register_human,
    set_human_presence,
)


def _log(msg):
    """Diagnostics → stderr ONLY (stdout is the JSON-RPC stream)."""
    print(f"[teammate-comms] dashboard: {msg}", file=sys.stderr, flush=True)


def _redact_query(s):
    """Scrub any URL query string from a log line. The dashboard token rides `?token=...` in
    the bootstrap GET, so request lines and error logs would otherwise leak the secret to
    stderr / the debug log (audit D-2). Replaces `?<query>` (up to the next whitespace/quote)
    with `?<redacted>`. Apply at EVERY sink that logs a path or request line."""
    return re.sub(r'\?[^\s"\']*', '?<redacted>', s)


# ── single-file frontend ────────────────────────────────────────────────────────

_INDEX_CACHE = None


def _load_index():
    """Load static/index.html once and cache it (token substituted per serve)."""
    global _INDEX_CACHE
    if _INDEX_CACHE is None:
        primary = Path(__file__).resolve().parent / "static" / "index.html"
        try:
            _INDEX_CACHE = primary.read_text(encoding="utf-8")
        except OSError:
            try:  # wheel-install fallback
                from importlib.resources import files
                _INDEX_CACHE = (files("teammate_comms") / "static" / "index.html").read_text(encoding="utf-8")
            except Exception as e:
                _log(f"index.html missing: {e}")
                _INDEX_CACHE = "<!doctype html><title>teammate dashboard</title><p>index.html missing</p>"
    return _INDEX_CACHE


# ── server + handler ────────────────────────────────────────────────────────────

class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, token, root, team, human_name):
        super().__init__(addr, handler)
        self.token = token
        self.root = root
        self.team = team
        self.human_name = human_name

    def handle_error(self, request, client_address):  # never dump to stdout
        _log(f"request error from {client_address}")


class _DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive → every response MUST set Content-Length
    server_version = "teammate-comms-dashboard"

    # Route ALL logging to stderr — the default writes to sys.stderr already, but be
    # explicit so nothing can ever reach stdout.
    def log_message(self, fmt, *args):
        _log("http " + _redact_query(fmt % args))      # request line carries ?token= — redact (D-2)

    def log_error(self, fmt, *args):
        _log("http " + _redact_query(fmt % args))

    # ── helpers ──
    def _host_ok(self):
        host = self.headers.get("Host", "")
        if not host:
            return False  # reject missing Host (don't default-allow)
        if host.startswith("["):           # IPv6 literal: [::1]:7842
            hostname = host[1:].split("]", 1)[0]
        elif ":" in host:
            hostname = host.rsplit(":", 1)[0]
        else:
            hostname = host
        return hostname in ("127.0.0.1", "localhost")

    def _token_ok(self, provided):
        return bool(provided) and secrets.compare_digest(str(provided), self.server.token)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body_str):
        body = body_str.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── verbs ──
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path, qs = parsed.path, parse_qs(parsed.query)
            if not self._host_ok():
                return self._json(403, {"error": "invalid host"})
            if path == "/":
                if not self._token_ok(qs.get("token", [None])[0]):
                    return self._json(403, {"error": "missing or invalid token"})
                token_js = json.dumps(self.server.token)
                return self._html(_load_index().replace('"%TOKEN%"', token_js))
            if path.startswith("/api/"):
                if not self._token_ok(self.headers.get("X-Dashboard-Token")):
                    return self._json(401, {"error": "missing or invalid token"})
                if path == "/api/conversations":
                    return self._api_conversations()
                if path == "/api/poll":
                    return self._api_poll(qs.get("cursor", [""])[0],
                                          qs.get("rcursor", [""])[0],
                                          qs.get("dcursor", [""])[0])
            return self._json(404, {"error": "not found"})
        except Exception as e:  # never leak a stack to stdout / never hang the socket
            _log(_redact_query(f"GET {self.path} failed: {e}"))   # path carries ?token= (D-2)
            try:
                return self._json(500, {"error": "internal error"})
            except Exception:
                pass

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            if not self._host_ok():
                return self._json(403, {"error": "invalid host"})
            if not self._token_ok(self.headers.get("X-Dashboard-Token")):
                return self._json(401, {"error": "missing or invalid token"})
            if parsed.path not in ("/api/send", "/api/react", "/api/delete"):
                return self._json(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:                 # reject before reading (D-3)
                return self._json(413, {"error": "request body too large"})
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "invalid json"})
            if parsed.path == "/api/react":
                return self._api_react(payload)
            if parsed.path == "/api/delete":
                return self._api_delete(payload)
            return self._api_send(payload)
        except Exception as e:
            _log(_redact_query(f"POST {self.path} failed: {e}"))
            try:
                return self._json(500, {"error": "internal error"})
            except Exception:
                pass

    # ── REST endpoints ──
    def _api_conversations(self):
        root, team, me = self.server.root, self.server.team, self.server.human_name
        roster, peers = [], []
        agents_dir = get_agents_dir(root, team)
        if agents_dir.exists():
            for p in sorted(agents_dir.glob("*.json")):
                rec = read_agent_record(root, team, p.stem)
                if not isinstance(rec, dict):
                    continue
                kind = rec.get("type", "unknown")
                online = (rec.get("presence") == "online") if kind == "human" \
                    else is_channel_alive(rec, pid_check=False)
                roster.append({
                    "agent": p.stem, "type": kind, "online": bool(online),
                    "project": rec.get("project"), "role": rec.get("role"),
                    "status": rec.get("status"),
                })
                if p.stem != me:
                    peers.append(p.stem)
        groups = []
        groups_dir = get_groups_dir(root, team)
        if groups_dir.exists():
            for gp in sorted(d for d in groups_dir.iterdir() if d.is_dir()):
                meta = read_group_meta(root, team, gp.name)
                if not isinstance(meta, dict):
                    continue
                members = meta.get("members", [])
                # read-receipt positions (read-only inference from each member's _read.json)
                reads = group_read_positions(root, team, gp.name, members)
                groups.append({"id": "#" + gp.name, "name": gp.name,
                               "members": members, "reads": reads})
        dms = [{"id": "@" + peer, "peer": peer} for peer in peers]
        return self._json(200, {"me": me, "groups": groups, "roster": roster, "dms": dms})

    def _api_poll(self, cursor, rcursor, dcursor):
        root, team = self.server.root, self.server.team
        # oldest_first=bool(cursor): a fresh load (no cursor) takes the newest tail; once
        # walking a cursor we page OLDEST-first and advance the cursor only to the last
        # returned id, so a burst larger than `limit` drains across polls instead of being
        # skipped (audit A-1). All three sub-streams thread the same policy.
        records = read_transcript(root, team, since=(cursor or None), limit=200,
                                  oldest_first=bool(cursor))
        new_cursor = records[-1]["id"] if records else cursor
        # Reaction events sub-stream (own cursor). The frontend folds add/remove into
        # per-message chips client-side. Ambient — never woke anyone.
        reactions = read_reactions(root, team, since=(rcursor or None), limit=500,
                                   oldest_first=bool(rcursor))
        new_rcursor = reactions[-1]["id"] if reactions else rcursor
        # Deletions sub-stream (own cursor). The frontend folds these into a deleted-set and
        # re-renders affected messages/groups — the firehose is append-only and id-keyed, so an
        # in-place tombstone never re-crosses `cursor`. One full read of the live jsonl (bounded
        # near DELETIONS_RETAIN by compaction — same cost as the old cursored since-scan) drives
        # all three cases below; `floor` is its oldest id.
        #   FRESH load (no dcursor): the COMPLETE deleted-set = the compacted baseline (set-file)
        #   UNIONed with the ENTIRE live jsonl. The FULL jsonl read (limit=None) is DELIBERATE and
        #   load-bearing for C-2: a bounded newest-N tail would skip any event sitting between the
        #   retained tail and the compaction gate (not yet folded into the set) — do NOT "optimize"
        #   this back to a tail read. The union folds idempotently (keyed by target).
        #   LAGGED cursored (dcursor < floor): events in (dcursor, floor) were compacted into the
        #   set-file and this client never saw them — a suspended tab resuming. Replay the SAME
        #   complete union THIS poll (the rescue), so a deleted message can't silently render
        #   undeleted until the user happens to reload. Fires once: new_dcursor then advances past
        #   the floor, so the steady state falls through to the cheap incremental walk.
        #   INCREMENTAL cursored (dcursor >= floor): only events since the cursor, burst-paged
        #   oldest-first via _window (A-1: advance to the last RETURNED id so a burst > limit pages
        #   out across polls; the cut swallows any same-id collision group so the cursor can't stall).
        jsonl = read_deletions(root, team, limit=None)   # entire live file (bounded), oldest→newest
        floor = jsonl[0]["id"] if jsonl else ""
        if (not dcursor) or (floor and dcursor < floor):
            deletions = list(read_deletions_set(root, team).values()) + jsonl
            new_dcursor = jsonl[-1]["id"] if jsonl else dcursor   # caught fully up; track the tail
        else:
            deletions = _window([e for e in jsonl if e.get("id", "") >= dcursor], 1000, True)
            new_dcursor = deletions[-1]["id"] if deletions else dcursor   # last RETURNED id (burst paging)
        return self._json(200, {"records": records, "cursor": new_cursor,
                                "reactions": reactions, "rcursor": new_rcursor,
                                "deletions": deletions, "dcursor": new_dcursor})

    def _api_react(self, payload):
        root, team, me = self.server.root, self.server.team, self.server.human_name
        if not me:
            return self._json(409, {"error": "no human identity registered"})
        try:
            rec = _tools.react(root, team, me, payload.get("target"),
                               payload.get("emoji"), bool(payload.get("remove")))
        except CommsError as e:
            return self._json(400, {"error": str(e)})
        return self._json(200, {"ok": True, "id": rec.get("id")})

    def _api_delete(self, payload):
        # The console acts AS the human operator, so it has operator delete power over
        # ANY message (author-or-operator) and may remove offline teammates. It can NOT
        # remove the operator's own identity (guarded here + in remove_teammate).
        root, team, me = self.server.root, self.server.team, self.server.human_name
        if not me:
            return self._json(409, {"error": "no human identity registered"})
        msg = payload.get("message")
        who = payload.get("teammate")
        has_msg = bool(isinstance(msg, str) and msg.strip())
        has_who = bool(isinstance(who, str) and who.strip())
        if has_msg == has_who:
            return self._json(400, {"error": "provide exactly one of 'message' or 'teammate'"})
        try:
            if has_msg:
                _tools.delete_message(root, team, me, msg, is_operator=True)
            else:
                name = who.strip()
                if name == me:
                    return self._json(400, {"error": "the operator can't be removed from the console"})
                _tools.remove_teammate(root, team, me, name, is_operator=True)
        except CommsError as e:
            return self._json(400, {"error": str(e)})
        return self._json(200, {"ok": True})

    def _api_send(self, payload):
        root, team, me = self.server.root, self.server.team, self.server.human_name
        if not me:
            return self._json(409, {"error": "no human identity registered"})
        to = (payload.get("to") or "").strip()
        message = payload.get("message")
        priority = payload.get("priority") or "normal"
        # Thread + type from the console (B-1): pass reply_to/post_type through to the cores,
        # which validate them (a bad post_type → CommsError → 400; reply_to is an unvalidated
        # citation hint). '' → None so the core stores no key.
        post_type = payload.get("post_type") or None
        reply_to = payload.get("reply_to") or None
        if not to:
            return self._json(400, {"error": "'to' is required"})
        try:
            if to.startswith("#"):
                res = _tools.send_group(root, team, me, to, message, priority,
                                        post_type=post_type, reply_to=reply_to)
            else:
                res = _tools.send_dm(root, team, me, to, message, priority,
                                     post_type=post_type, reply_to=reply_to)
        except CommsError as e:
            return self._json(400, {"error": str(e)})
        return self._json(200, {"ok": True, "id": res.get("id")})


# ── lifecycle ───────────────────────────────────────────────────────────────────

class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.httpd = None
        self.thread = None
        self.port = None
        self.token = None
        self.root = None
        self.team = None
        self.human_name = None


_STATE = _State()


def _open_browser(url):
    """Open the page without ever letting a child process touch our stdout."""
    try:
        if os.name == "nt":
            os.startfile(url)  # noqa: S606 — does not inherit our stdout
            return
        import webbrowser
        webbrowser.open(url)  # POSIX best-effort
    except Exception as e:
        _log(f"could not open browser: {e}")


def start_dashboard(root, team, human_name, port=7842, open_browser=True):
    """Start (or return the already-running) dashboard for this process.

    Idempotent PER PROCESS: a second call returns the same URL. Binds 127.0.0.1 first
    (preferred port → +10 → OS-assigned); only after a successful bind does it register
    the human + mark them online, so a bind failure never leaves a registered human
    with no server. Returns ``{url, status, port}``.
    """
    with _STATE.lock:
        if _STATE.httpd is not None:
            url = f"http://127.0.0.1:{_STATE.port}/?token={_STATE.token}"
            result = {"url": url, "status": "already-running", "port": _STATE.port}
        else:
            token = secrets.token_urlsafe(32)
            httpd, chosen, last_err = None, None, None
            for p in [port + i for i in range(11)] + [0]:
                try:
                    httpd = _DashboardServer(("127.0.0.1", p), _DashboardHandler,
                                             token, root, team, human_name)
                    chosen = httpd.server_address[1]
                    break
                except OSError as e:
                    last_err = e
            if httpd is None:
                raise CommsError(f"Could not bind a dashboard port on 127.0.0.1: {last_err}")
            try:
                register_human(root, team, human_name)
            except CommsError as e:
                _log(f"human registration failed (continuing): {e}")
            thread = threading.Thread(target=httpd.serve_forever,
                                      name="teammate-dashboard", daemon=True)
            thread.start()
            _STATE.httpd, _STATE.thread, _STATE.port = httpd, thread, chosen
            _STATE.token, _STATE.root, _STATE.team, _STATE.human_name = \
                token, root, team, human_name
            url = f"http://127.0.0.1:{chosen}/?token={token}"
            result = {"url": url, "status": "running", "port": chosen}
    if open_browser:
        _open_browser(result["url"])
    return result


def shutdown_dashboard():
    """Stop the dashboard (idempotent, never raises). Called from the stdio finally."""
    with _STATE.lock:
        httpd = _STATE.httpd
        root, team, human_name = _STATE.root, _STATE.team, _STATE.human_name
        _STATE.httpd = _STATE.thread = _STATE.port = None
        _STATE.token = _STATE.root = _STATE.team = _STATE.human_name = None
    if httpd is None:
        return
    if human_name and root is not None:
        try:
            set_human_presence(root, team, human_name, "away")
        except Exception as e:
            _log(f"presence-away failed: {e}")
    try:
        httpd.shutdown()
        httpd.server_close()
    except Exception as e:
        _log(f"shutdown error: {e}")
