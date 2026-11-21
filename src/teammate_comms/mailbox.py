"""Cloud mailbox links: a hosted mailbox that carries direct messages between this machine
and cloud agents that cannot reach it (see docs/cloud-mailbox-plan.md).

This module owns the link file, the key file rules and the HTTPS client. The relay drives
the exchange; nothing here listens for connections.
"""
import base64
import http.client
import ipaddress
import json
import os
import re
import time
import unicodedata
import urllib.parse
from pathlib import Path

from .comms import (
    MACHINE_NAME_PATTERN,
    CommsError,
    file_lock,
    get_relay_dir,
    read_json_readonly,
    validate_agent_name,
    validate_machine_name,
    write_json_atomic,
)

PROTOCOL = 1
IDLE_SECONDS = 10
BUSY_SECONDS = 1
HTTP_TIMEOUT_SECONDS = 10
BATCH_EVENTS = 20
BATCH_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
INBOUND_PER_WINDOW = 60
INBOUND_MAILBOX_PER_WINDOW = 180
INBOUND_WINDOW_SECONDS = 600
APPLY_BUDGET_SECONDS = 5
SYNC_DEADLINE_SECONDS = 30
RATE_REFUSAL = (f"refused: more than {INBOUND_PER_WINDOW} messages from one cloud agent in "
                f"{INBOUND_WINDOW_SECONDS // 60} minutes. Send fewer messages")
KEY_FILE_MAX_BYTES = 256
ID_FUTURE_SECONDS = 900
ROSTER_REFRESH_SECONDS = 300
CLOUD_NAME_CHARS = 64
MIN_WORKER = "1.1.0"
PROTECT_KEYS = os.name == "nt"
ID_PAST_SECONDS = 15 * 86400
NOTE_CHARS = 200
LINK_ACTION = "link_mailbox"
UNLINK_ACTION = "unlink_mailbox"
CLOUD_ONLY_DM = ("is a cloud agent reached through a mailbox. A cloud agent can only be sent "
                 "direct messages with teammate_send: it cannot join a group, and it cannot "
                 "be started or shut down from here")

ALLOW_INSECURE = False

KEY_PREFIX = "tcm_"
_KEY_RE = re.compile(r"tcm_[A-Za-z0-9_-]{32,124}")
_HEADER_LOOKALIKE = re.compile(r"-{3,}(?=\s*id\s*:)", re.IGNORECASE)
_BOT_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}")


def mailboxes_file(root):
    return get_relay_dir(root) / "mailboxes.json"


def cloud_name(name):
    """An agent's name as a mailbox carries it: at most 64 characters."""
    return name[:CLOUD_NAME_CHARS]


def _dpapi(data, protect):
    """Encrypt or decrypt bytes for the current Windows user on this machine."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = Blob(len(data), buf), Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out)):
        raise OSError("Windows data protection refused")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


_unsealed = {}


def seal(key):
    return base64.b64encode(_dpapi(key.encode("utf-8"), True)).decode("ascii")


def unseal(blob):
    """The key inside a stored blob, or None when this user on this machine cannot open it."""
    if blob not in _unsealed:
        try:
            _unsealed[blob] = _dpapi(base64.b64decode(blob, validate=True), False).decode("utf-8")
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if len(_unsealed) > 64:
            _unsealed.pop(next(iter(_unsealed)))
    return _unsealed[blob]


def _stored(root):
    """Every well-formed stored link as ``{name: {"url", "you_are", "key" or None}}``."""
    data = read_json_readonly(mailboxes_file(root))
    boxes = data.get("mailboxes") if isinstance(data, dict) else None
    out = {}
    for name, cfg in (boxes.items() if isinstance(boxes, dict) else ()):
        if not (isinstance(name, str) and MACHINE_NAME_PATTERN.fullmatch(name)
                and isinstance(cfg, dict) and isinstance(cfg.get("url"), str)
                and isinstance(cfg.get("you_are"), str)
                and MACHINE_NAME_PATTERN.fullmatch(cfg["you_are"])):
            continue
        key = None
        if isinstance(cfg.get("key_protected"), str) and PROTECT_KEYS:
            key = unseal(cfg["key_protected"])
        if key is None:
            key = cfg.get("key")
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            key = None
        out[name] = {"url": cfg["url"], "key": key, "you_are": cfg["you_are"]}
    return out


def protect_stored(root):
    """Seal any key still stored as plain text. Does nothing where keys are not sealed."""
    path = mailboxes_file(root)
    if not PROTECT_KEYS or not path.exists():
        return
    boxes = (read_json_readonly(path) or {}).get("mailboxes") or {}
    if any(isinstance(cfg, dict) and "key" in cfg for cfg in boxes.values()):
        with file_lock(path):
            _write(root, _stored(root))


def read_mailboxes(root):
    """Linked mailboxes as ``{name: {"url", "key", "you_are"}}``."""
    return {name: cfg for name, cfg in _stored(root).items() if cfg["key"]}


def unreadable(root):
    """Linked names whose stored key cannot be opened here, for example a copied comms folder."""
    return sorted(name for name, cfg in _stored(root).items() if not cfg["key"])


def _write(root, boxes):
    path = mailboxes_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = (read_json_readonly(path) or {}).get("mailboxes") or {}
    out = {}
    for name, cfg in boxes.items():
        if not cfg["key"]:
            out[name] = kept[name]
        elif PROTECT_KEYS:
            out[name] = {"url": cfg["url"], "you_are": cfg["you_are"],
                         "key_protected": seal(cfg["key"])}
        else:
            out[name] = cfg
    write_json_atomic(path, {"mailboxes": out})
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def clean_url(value):
    """The mailbox base address, or raise CommsError. Only https to a named host is allowed."""
    if not isinstance(value, str) or not value.strip():
        raise CommsError("'url' is required: the https address of the mailbox.")
    raw = value.strip()
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError:
        raise CommsError(f"{raw!r} is not a valid address.")
    host = (parts.hostname or "").lower()
    if parts.username or parts.password or parts.query or parts.fragment or not host:
        raise CommsError("'url' must be a plain https address with no user name, query or "
                         "fragment.")
    local = host in ("localhost", "127.0.0.1")
    if not (ALLOW_INSECURE and local):
        if parts.scheme != "https":
            raise CommsError("'url' must start with https://.")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise CommsError("'url' must name a host, not an IP address.")
        if host == "localhost" or "." not in host:
            raise CommsError("'url' must be the public address of the mailbox.")
    path = parts.path.rstrip("/")
    for tail in ("/pc/sync", "/mcp"):
        if path.endswith(tail):
            path = path[:-len(tail)]
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}{path}"


def read_key_file(value):
    """The machine key held in a file. The file must hold one key and nothing else."""
    if not isinstance(value, str) or not value.strip():
        raise CommsError("'key_file' is required: the path of the file holding this machine's "
                         "mailbox key.")
    path = Path(value.strip()).expanduser()
    try:
        if not path.is_file() or path.stat().st_size > KEY_FILE_MAX_BYTES:
            raise CommsError(f"{path} is not a mailbox key file.")
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise CommsError(f"{path} could not be read as a mailbox key file.")
    if not _KEY_RE.fullmatch(text):
        raise CommsError(f"{path} is not a mailbox key file.")
    return text


def post_sync(url, key, body, timeout=HTTP_TIMEOUT_SECONDS):
    """One sync call. Returns ``(status, object)``; never follows a redirect."""
    parts = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(parts.hostname, parts.port, timeout=timeout)
    try:
        conn.request("POST", (parts.path.rstrip("/") or "") + "/pc/sync",
                     body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                     headers={"Content-Type": "application/json", "Accept": "application/json",
                              "Authorization": "Bearer " + key,
                              "User-Agent": "teammate-comms-relay"})
        resp = conn.getresponse()
        deadline = time.monotonic() + SYNC_DEADLINE_SECONDS
        chunks, size = [], 0
        while size <= MAX_RESPONSE_BYTES:
            chunk = resp.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if time.monotonic() > deadline:
                raise TimeoutError("the mailbox answered too slowly")
        raw = b"".join(chunks)
    finally:
        conn.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        return resp.status, {}
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        obj = {}
    return resp.status, obj if isinstance(obj, dict) else {}


def clean_note(value):
    """Text a mailbox returned, made safe to show an agent on one line."""
    return " ".join(str(value or "").split())[:NOTE_CHARS]


def said(value):
    """Mailbox-supplied text, attributed so it is never read as teammate-comms speaking."""
    note = clean_note(value)
    return f"the mailbox said: {note}" if note else ""


def clean_bots(raw):
    """The cloud agents a mailbox reported, as roster entries."""
    bots, seen = [], set()
    for entry in (raw if isinstance(raw, list) else [])[:50]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        try:
            validate_agent_name(name)
        except CommsError:
            continue
        if (name in seen or not _BOT_NAME_RE.fullmatch(name)
                or name.lower() == "comms-relay"):
            continue
        seen.add(name)
        last = entry.get("last_seen")
        waiting = entry.get("waiting")
        bots.append({
            "name": name, "type": "cloud", "live": False,
            "last_seen": last if isinstance(last, (int, float)) and not isinstance(last, bool)
            and 0 < last < 4e9 else None,
            "waiting": waiting if isinstance(waiting, int) and not isinstance(waiting, bool)
            and 0 <= waiting <= 100000 else 0,
            "doorbell": entry.get("doorbell") is True,
            "doorbell_error": said(entry.get("doorbell_error")),
        })
    return bots


def neutralise(text):
    """Stop message text from imitating the inbox's own message header."""
    text = "\n".join(text.splitlines())
    text = "".join(ch for ch in text
                   if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))
    return _HEADER_LOOKALIKE.sub("- - -", text).lstrip()


def sync_failure(status, resp):
    """``(state, detail)`` for a sync call that did not return 200."""
    if status in (401, 403):
        return "mailbox_refused", ""
    if status == 503 and resp.get("error") == "not_configured":
        return "mailbox_error", "it has no valid configuration (MAILBOX_CONFIG)"
    if status == 400:
        return "mailbox_error", ("it did not understand this version of teammate-comms; "
                                 "redeploy the mailbox from the same version as the plugin")
    return "mailbox_error", f"HTTP {status}. {said(resp.get('error'))}".strip()


def link(root, name, url, key_file, taken, post=None):
    """Link this machine to a mailbox after proving the key works. ``taken`` maps a name
    that cannot be used to the reason."""
    name = validate_machine_name(name, param="machine")
    if name in taken:
        raise CommsError(f"{name!r} cannot be used as a mailbox name: {taken[name]}. Pick "
                         f"another short name, for example 'cloudbot'.")
    if name in _stored(root):
        raise CommsError(f"{name!r} is already a linked mailbox. Unlink it first if it should "
                         f"point somewhere else or use a new key.")
    base = clean_url(url)
    key = read_key_file(key_file)
    try:
        status, resp = (post or post_sync)(base, key, {"v": PROTOCOL})
    except Exception as e:
        raise CommsError(f"NOT LINKED: the mailbox at {base} could not be reached "
                         f"({type(e).__name__}). Check the address and that it is deployed.")
    if status != 200:
        state, detail = sync_failure(status, resp)
        why = ("the mailbox does not accept this key. Check that the key file is the one "
               "made for this machine and that the mailbox configuration was uploaded"
               if state == "mailbox_refused" else detail)
        raise CommsError(f"NOT LINKED: {why}.")
    me = resp.get("you_are")
    if (resp.get("v") != PROTOCOL or not isinstance(me, str)
            or not MACHINE_NAME_PATTERN.fullmatch(me)):
        raise CommsError(f"NOT LINKED: {base} did not answer like a teammate-comms mailbox.")
    path = mailboxes_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        boxes = _stored(root)
        if name in boxes:
            raise CommsError(f"{name!r} is already a linked mailbox.")
        boxes[name] = {"url": base, "key": key, "you_are": me}
        _write(root, boxes)
    bots = [b["name"] for b in clean_bots(resp.get("bots"))]
    return name, me, bots


def unlink(root, name):
    name = validate_machine_name(name, param="machine")
    path = mailboxes_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        boxes = _stored(root)
        if name not in boxes:
            raise CommsError(f"{name!r} is not a linked mailbox.")
        boxes.pop(name)
        _write(root, boxes)
    return name
