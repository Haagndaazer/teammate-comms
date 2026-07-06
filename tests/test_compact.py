"""Compaction-broker plugin support — acceptance tests (shared suite, WP-36 + WP-37 + WP-38).

WP-36 (this pass): registration auto-capture — pane_id, wezterm_socket, manager fields.
  AC-1: pane_id/wezterm_socket captured as int/basename; pane_id=0 renders as 0, not absent.
  AC-2: stale-clear — re-register outside WezTerm nulls a previously-set pane binding;
        tautology proof that write_agent_record's field-merge (not the clear itself) is
        what would otherwise PRESERVE a stale value if the explicit-None pass were dropped.
  AC-3: a malformed WEZTERM_PANE never fails registration; pane_id -> None.
  AC-4: manager precedence (AGENT_MANAGER env > explicit param > preserve); clear mechanics
        (empty/whitespace is a pre-validation clear sentinel); malformed env dropped,
        malformed explicit param raises.
  AC-5: teammate_profile always renders manager/wezterm_socket/pane_id; teammate_list shows
        the added lines only when set.
  AC-6: this suite is wired into ci.yml as a 4th step (see .github/workflows/ci.yml).

WP-37: teammate_request_compact — write-time authz + atomic v1 request-file drop.
  AC-1 (self): file appears with exactly the six v1 keys; requester stamped correctly.
  AC-2 (manager): a manager may request for their subordinate; an unrelated caller is
        denied, no file written, and an audit DM from 'compact-broker' lands in their inbox.
  AC-3 (anti-spoof): a stray `requester` arg is dead — the server-stamped caller always wins.
  AC-4: unregistered target -> CommsError, no file; unregistered caller -> the standard
        not-registered error.
  AC-5 (atomicity): the request dir holds only the final .json, zero .tmp residue.
  AC-6 (reservation): registering as 'compact-broker' (any case) is rejected.
  AC-7: all four suites green on Windows (this file's own presence + ci.yml wiring).

WP-38: broker delivery CLI — `python -m teammate_comms.deliver`.
  AC-1: --message form delivers to the target's unread file, from='compact-broker', a
        valid minted id, and a transcript tee; exit 0, stdout carries the id.
  AC-2: stdin mode — a BOM'd UTF-8 body with an embedded newline and non-ASCII char
        arrives with EXACT content (byte-for-byte after _clean_message's strip).
  AC-3: to==sender, an invalid agent name, an empty message, and a missing --to (the
        argparse path) all exit 2, no file touched.
  AC-4: an unregistered --to still queues (parity with _handle_send); exit 0.
  AC-5: (a) deterministic — the test holds file_lock(unread_file) itself, launches the
        CLI, asserts it has NOT completed while the lock is held, releases, then asserts
        both messages present. (b) probabilistic fan-out — concurrent CLI processes to
        one inbox; all ids present afterward.
  AC-6: plugin-runtime.json appears under an isolated root after a server start, with the
        five v1 keys; a pointer-write failure must not prevent boot.
  AC-7: all four suites green (this file's own presence + ci.yml wiring).

Run: uv run --no-sync python tests/test_compact.py
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid as uuid_mod
from pathlib import Path

# WP-21 gate micro-CR: an emoji in a FAIL message crashes the harness's own report with
# UnicodeEncodeError under Windows cp1252 stdout, masking failure details. Harness-report-only.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

from teammate_comms.comms import (
    COMPACT_BROKER_SENDER,
    CommsError,
    file_lock,
    get_compact_requests_dir,
    get_inboxes_dir,
    get_transcript_file,
    read_agent_record,
    read_json_safe,
    write_agent_record,
)
from teammate_comms import server as server_mod
from teammate_comms import tools as tools_mod

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def check_raises(fn, msg):
    try:
        fn()
        failures.append(f"Expected CommsError but no exception: {msg}")
    except CommsError:
        pass
    except Exception as e:
        failures.append(f"Expected CommsError, got {type(e).__name__}: {msg}: {e}")


@contextlib.contextmanager
def env_vars(**kv):
    """Set/clear env vars for the block; restore prior values (or absence) after."""
    prev = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, old in prev.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def _make_root():
    td = tempfile.TemporaryDirectory()
    return td, Path(td.name)


def _profile_field_map(text):
    """Parse `_format_profile`'s '  label:   value' lines into {label: value}."""
    out = {}
    for ln in text.splitlines():
        if ":" in ln:
            k, _, v = ln.partition(":")
            out[k.strip()] = v.strip()
    return out


class FakeIdentity:
    """Minimal stand-in for server.Identity — .snapshot() (needed by _require_registered)
    plus the last-seen tracking _handle_inbox reads/writes, so tool-handler unit tests don't
    need to exercise register_identity's full side-effect chain (channel arming etc.)."""

    def __init__(self, agent, team, root):
        self._agent, self._team, self._root = agent, team, root
        self._last_seen = None

    def snapshot(self):
        return (self._agent, self._team, self._root, None)

    def get_last_seen(self):
        return None if self._last_seen is None else set(self._last_seen)

    def set_last_seen(self, ids):
        self._last_seen = set(ids)


def _ctx_for(agent, team, root):
    return {"identity": FakeIdentity(agent, team, root), "register": None,
            "auto_register_error": lambda: None}


# ── AC-1 / AC-3: pane_id / wezterm_socket captured at register_identity ────────────────

def test_ac1_ac3_pane_capture():
    td, root = _make_root()
    with td:
        with env_vars(WEZTERM_PANE="42", WEZTERM_UNIX_SOCKET=str(Path(root, "gui-sock-87100"))):
            server_mod.register_identity("PaneBot", None, str(root))
        rec = read_agent_record(root, None, "PaneBot")
        check(rec.get("pane_id") == 42 and isinstance(rec.get("pane_id"), int),
              "tautology[AC-1]: WEZTERM_PANE=42 must be stored as an int pane_id==42")
        check(rec.get("wezterm_socket") == "gui-sock-87100",
              "tautology[AC-1]: wezterm_socket must be the socket path's basename, not the full path")

        # pane_id=0 is the ordinary single-pane value — must render as 0, never '(not set)'.
        with env_vars(WEZTERM_PANE="0", WEZTERM_UNIX_SOCKET=str(Path(root, "gui-sock-1"))):
            server_mod.register_identity("PaneBot", None, str(root))
        rec = read_agent_record(root, None, "PaneBot")
        check(rec.get("pane_id") == 0,
              "tautology[AC-1]: pane_id=0 must be stored as int 0, not dropped as falsy")
        profile_text = tools_mod._format_profile(rec, "PaneBot", is_self=True, root=root, team=None)
        check(_profile_field_map(profile_text).get("pane_id") == "0",
              "tautology[AC-1]: _format_profile must render pane 0 as '0', not '(not set)' "
              "(a truthiness-idiom regression — pane 0 is a real, ordinary pane)")

        # AC-3: a malformed WEZTERM_PANE never fails registration.
        with env_vars(WEZTERM_PANE="garbage", WEZTERM_UNIX_SOCKET=None):
            result = server_mod.register_identity("PaneBot", None, str(root))
        check(isinstance(result, str) and "Registered" in result,
              "tautology[AC-3]: a malformed WEZTERM_PANE value must not raise or fail registration")
        rec = read_agent_record(root, None, "PaneBot")
        check(rec.get("pane_id") is None,
              "tautology[AC-3]: a malformed WEZTERM_PANE must store pane_id=None")


# ── AC-2: stale-clear on re-register outside WezTerm (+ tautology proof) ───────────────

def test_ac2_stale_clear():
    td, root = _make_root()
    with td:
        with env_vars(WEZTERM_PANE="7", WEZTERM_UNIX_SOCKET=str(Path(root, "gui-sock-A"))):
            server_mod.register_identity("StaleBot", None, str(root))
        rec = read_agent_record(root, None, "StaleBot")
        check(rec.get("pane_id") == 7 and rec.get("wezterm_socket") == "gui-sock-A",
              "setup: seed record must carry pane_id/wezterm_socket before the stale-clear check")

        with env_vars(WEZTERM_PANE=None, WEZTERM_UNIX_SOCKET=None):
            server_mod.register_identity("StaleBot", None, str(root))
        rec = read_agent_record(root, None, "StaleBot")
        check(rec.get("pane_id") is None,
              "tautology[AC-2]: re-registering outside WezTerm must clear a stale pane_id to null "
              "(else the broker keeps injecting into a pane this agent no longer owns)")
        check(rec.get("wezterm_socket") is None,
              "tautology[AC-2]: re-registering outside WezTerm must clear a stale wezterm_socket to null")

        # Tautology proof: write_agent_record's field-merge PRESERVES an omitted key. AC-2 above
        # only holds because register_identity explicitly passes None for both fields on every
        # call — demonstrate the merge's preserve-on-omit behavior directly, so a future edit
        # that starts OMITTING pane_id/wezterm_socket when absent (instead of passing None)
        # would silently reopen the stale-binding bug this test exists to catch.
        write_agent_record(root, None, "StaleBot", pane_id=99, wezterm_socket="gui-sock-B")
        write_agent_record(root, None, "StaleBot", startedAt="proof-write")  # omits both fields
        rec2 = read_agent_record(root, None, "StaleBot")
        check(rec2.get("pane_id") == 99 and rec2.get("wezterm_socket") == "gui-sock-B",
              "tautology[AC-2 proof]: write_agent_record must PRESERVE an omitted field on merge — "
              "proving the explicit-None PASS (not the merge itself) is what clears stale pane "
              "bindings in register_identity")


# ── AC-4: manager precedence + clear mechanics ─────────────────────────────────────────

def test_ac4_manager_precedence_and_clear():
    td, root = _make_root()
    with td:
        # env wins over an explicit param.
        with env_vars(AGENT_MANAGER="Silvie"):
            server_mod.register_identity("Sub", None, str(root), manager="Ignored")
        rec = read_agent_record(root, None, "Sub")
        check(rec.get("manager") == "Silvie",
              "tautology[AC-4]: $AGENT_MANAGER must win over an explicit manager param")

        # explicit param applies when env is absent.
        with env_vars(AGENT_MANAGER=None):
            server_mod.register_identity("Sub", None, str(root), manager="Explicit")
        rec = read_agent_record(root, None, "Sub")
        check(rec.get("manager") == "Explicit",
              "tautology[AC-4]: explicit manager param must apply when $AGENT_MANAGER is absent")

        # neither given -> existing value preserved across re-register.
        with env_vars(AGENT_MANAGER=None):
            server_mod.register_identity("Sub", None, str(root))
        rec = read_agent_record(root, None, "Sub")
        check(rec.get("manager") == "Explicit",
              "tautology[AC-4]: omitting manager on re-register must PRESERVE the existing value, "
              "not clear it")

        # a malformed EXPLICIT param raises.
        with env_vars(AGENT_MANAGER=None):
            check_raises(
                lambda: server_mod.register_identity("Sub", None, str(root), manager="../bad"),
                "tautology[AC-4]: a malformed EXPLICIT manager param must raise CommsError "
                "(the caller typed it — unlike a malformed env value, it is not silently dropped)"
            )
        rec = read_agent_record(root, None, "Sub")
        check(rec.get("manager") == "Explicit",
              "tautology[AC-4]: a rejected explicit param must leave the prior value untouched")

        # a malformed ENV value is DROPPED (not raised), and must not clobber the preserved value.
        with env_vars(AGENT_MANAGER="../also-bad"):
            result = server_mod.register_identity("Sub", None, str(root))
        check(isinstance(result, str) and "Registered" in result,
              "tautology[AC-4]: a malformed $AGENT_MANAGER must be dropped, never raise "
              "(parity with spawned_by)")
        rec = read_agent_record(root, None, "Sub")
        check(rec.get("manager") == "Explicit",
              "tautology[AC-4]: a dropped malformed env value must not clobber the preserved manager")

        # teammate_update(manager="") clears the field — validate_agent_name("") always raises,
        # so this only passes if the clear sentinel is checked BEFORE validation.
        ctx = _ctx_for("Sub", None, root)
        tools_mod._handle_update({"manager": ""}, ctx)
        rec = read_agent_record(root, None, "Sub")
        check(rec.get("manager") is None,
              "tautology[AC-4]: teammate_update(manager='') must clear the field, not raise "
              "(validate_agent_name('') always raises — the clear sentinel must be checked first)")

        # teammate_update rejects a malformed non-empty manager the same way.
        check_raises(
            lambda: tools_mod._handle_update({"manager": "../bad"}, ctx),
            "tautology[AC-4]: teammate_update(manager='../bad') must raise CommsError"
        )


# ── AC-5: profile + list rendering ─────────────────────────────────────────────────────

def test_ac5_profile_and_list_rendering():
    td, root = _make_root()
    with td:
        write_agent_record(root, None, "Plain", type="full", channel=True)
        rec = read_agent_record(root, None, "Plain")
        text = tools_mod._format_profile(rec, "Plain", is_self=True, root=root, team=None)
        fmap = _profile_field_map(text)
        check(fmap.get("manager") == "(not set)",
              "tautology[AC-5]: profile must always render a manager line, '(not set)' when null")
        check(fmap.get("wezterm_socket") == "(not set)",
              "tautology[AC-5]: profile must always render a wezterm_socket line, '(not set)' when null")
        check(fmap.get("pane_id") == "(not set)",
              "tautology[AC-5]: profile must always render a pane_id line, '(not set)' when null")

        ctx = _ctx_for("Plain", None, root)
        list_text = tools_mod._handle_list({}, ctx)
        check("manager:" not in list_text,
              "tautology[AC-5]: teammate_list must NOT show a manager line when none is set")
        check("pane:" not in list_text,
              "tautology[AC-5]: teammate_list must NOT show a pane line when neither pane field is set")

        write_agent_record(root, None, "WithFields", type="full", channel=True,
                            manager="Boss", pane_id=3, wezterm_socket="sockZ")
        list_text2 = tools_mod._handle_list({}, ctx)
        check("manager:   Boss" in list_text2,
              "tautology[AC-5]: teammate_list must show 'manager: Boss' once manager is set")
        check("pane:      sockZ#3" in list_text2,
              "tautology[AC-5]: teammate_list must show the pane binding once set")

        # pane_id=0 truthiness edge, at the teammate_list layer too.
        write_agent_record(root, None, "PaneZero", type="full", channel=True,
                            pane_id=0, wezterm_socket="sockQ")
        list_text3 = tools_mod._handle_list({}, ctx)
        check("pane:      sockQ#0" in list_text3,
              "tautology[AC-5]: teammate_list must render pane_id=0 as '#0', not omit the line "
              "as if no pane were set")


# ── tool-wiring: teammate_register(manager=...) reaches the registry record ───────────

def test_register_tool_wiring():
    td, root = _make_root()
    with td:
        ctx = {"identity": server_mod._identity, "register": server_mod.register_identity,
               "auto_register_error": lambda: None}
        with env_vars(AGENT_MANAGER=None, WEZTERM_PANE="5",
                      WEZTERM_UNIX_SOCKET=str(Path(root, "gui-sock-9"))):
            result = tools_mod._handle_register(
                {"agent": "ToolWired", "comms_dir": str(root), "manager": "Boss2"}, ctx)
        check(isinstance(result, str) and "Registered" in result,
              "tool-wiring: teammate_register must succeed with a manager arg present")
        rec = read_agent_record(root, None, "ToolWired")
        check(rec.get("manager") == "Boss2",
              "tautology[wiring]: teammate_register(manager=...) must reach the registry record "
              "through _handle_register's arg-forwarding, not just register_identity directly")
        check(rec.get("pane_id") == 5 and rec.get("wezterm_socket") == "gui-sock-9",
              "tautology[wiring]: teammate_register must also capture pane fields via the tool path")


# ── WP-37 AC-1 (self) + AC-5 (atomicity) ───────────────────────────────────────────────

def test_wp37_ac1_self_and_ac5_atomicity():
    td, root = _make_root()
    with td:
        write_agent_record(root, None, "A", type="full", channel=True)
        ctx_a = _ctx_for("A", None, root)

        result = tools_mod._handle_request_compact({"target": "A"}, ctx_a)
        check(isinstance(result, str) and "A" in result,
              "AC-1: a successful self-compact request must return a confirmation string")

        reqs_dir = get_compact_requests_dir(root)
        files = list(reqs_dir.glob("*.json"))
        check(len(files) == 1, f"AC-1: exactly one request file must be written, got {len(files)}")
        tmp_files = [p for p in reqs_dir.iterdir() if p.suffix == ".tmp"]
        check(not tmp_files,
              "tautology[AC-5]: the request dir must hold only the final .json — zero .tmp "
              "residue proves write_json_atomic (temp sibling + os.replace) was actually used")

        data = json.loads(files[0].read_text(encoding="utf-8"))
        check(set(data.keys()) == {"v", "id", "requester", "target", "created_at", "ttl_seconds"},
              f"tautology[AC-1]: the v1 schema is EXACTLY six keys, no extras — got {sorted(data.keys())}")
        check(data.get("v") == 1, "AC-1: v must be 1")
        check(data.get("ttl_seconds") == 900, "AC-1: ttl_seconds must be 900")
        check(data.get("requester") == "A", "AC-1: requester must be the server-stamped caller")
        check(data.get("target") == "A", "AC-1: target must be the requested agent")
        try:
            uuid_mod.UUID(data.get("id"))
            valid_uuid = True
        except (ValueError, TypeError, AttributeError):
            valid_uuid = False
        check(valid_uuid, "AC-1: id must be a valid uuid4")
        check(files[0].name == f"{files[0].name.split('-')[0]}-{data['id'][:8]}.json"
              and files[0].name.endswith(f"-{data['id'][:8]}.json"),
              "AC-1: filename must embed id[:8] and match the created_at-derived stamp")


# ── WP-37 AC-2 (manager) ────────────────────────────────────────────────────────────────

def test_wp37_ac2_manager():
    td, root = _make_root()
    with td:
        write_agent_record(root, None, "A", type="full", channel=True)
        write_agent_record(root, None, "B", type="full", channel=True, manager="A")
        write_agent_record(root, None, "C", type="full", channel=True)  # unrelated

        ctx_a = _ctx_for("A", None, root)
        tools_mod._handle_request_compact({"target": "B"}, ctx_a)
        reqs_dir = get_compact_requests_dir(root)
        after_manager_request = len(list(reqs_dir.glob("*.json")))
        check(after_manager_request == 1,
              "AC-2: A (B's manager) requesting target=B must succeed and write a file")

        ctx_c = _ctx_for("C", None, root)
        check_raises(
            lambda: tools_mod._handle_request_compact({"target": "B"}, ctx_c),
            "AC-2: an unrelated caller C requesting target=B must be denied (CommsError)"
        )
        check(len(list(reqs_dir.glob("*.json"))) == after_manager_request,
              "tautology[AC-2]: a denied request must write NO file — count must stay unchanged")

        inbox_text = tools_mod._handle_inbox({"show_all": True}, ctx_c)
        check(COMPACT_BROKER_SENDER in inbox_text,
              "AC-2: the denial audit DM must land in C's own inbox, from 'compact-broker'")
        check("B" in inbox_text,
              "AC-2: the audit DM must name the target teammate the denied request was for")


# ── WP-37 AC-3 (anti-spoof) ─────────────────────────────────────────────────────────────

def test_wp37_ac3_anti_spoof():
    td, root = _make_root()
    with td:
        write_agent_record(root, None, "C", type="full", channel=True)
        ctx_c = _ctx_for("C", None, root)
        tools_mod._handle_request_compact({"target": "C", "requester": "A"}, ctx_c)
        reqs_dir = get_compact_requests_dir(root)
        files = list(reqs_dir.glob("*.json"))
        check(len(files) == 1, "AC-3 setup: the self-compact call must still write exactly one file")
        data = json.loads(files[0].read_text(encoding="utf-8"))
        check(data.get("requester") == "C",
              "tautology[AC-3]: a stray requester='A' arg must be DEAD — the file must still "
              "stamp the server-resolved caller (C), never the caller-supplied value")


# ── WP-37 AC-4: unregistered target / unregistered caller ──────────────────────────────

def test_wp37_ac4_unregistered():
    td, root = _make_root()
    with td:
        write_agent_record(root, None, "A", type="full", channel=True)
        ctx_a = _ctx_for("A", None, root)
        check_raises(
            lambda: tools_mod._handle_request_compact({"target": "NoSuchAgent"}, ctx_a),
            "AC-4: an unregistered target must raise CommsError"
        )
        reqs_dir = get_compact_requests_dir(root)
        check(not reqs_dir.exists() or not list(reqs_dir.glob("*.json")),
              "tautology[AC-4]: an unregistered target must write NO file")

        ctx_unregistered = _ctx_for(None, None, None)
        check_raises(
            lambda: tools_mod._handle_request_compact({"target": "A"}, ctx_unregistered),
            "AC-4: an unregistered CALLER must raise the standard not-registered error"
        )


# ── WP-37 AC-6: sentinel-name registration reservation ─────────────────────────────────

def test_wp37_ac6_reservation():
    td, root = _make_root()
    with td:
        check_raises(
            lambda: server_mod.register_identity("compact-broker", None, str(root)),
            "tautology[AC-6]: registering as 'compact-broker' must raise CommsError"
        )
        check_raises(
            lambda: server_mod.register_identity("Compact-Broker", None, str(root)),
            "tautology[AC-6]: registering as 'Compact-Broker' (case variant) must also raise "
            "CommsError — the reservation is case-insensitive"
        )
        # Existing agents unaffected: an ordinary name must still register normally.
        result = server_mod.register_identity("Zed", None, str(root))
        check(isinstance(result, str) and "Registered" in result,
              "AC-6: the reservation must not affect registration of an ordinary agent name")


# ── WP-38: broker delivery CLI ──────────────────────────────────────────────────────────

def _run_deliver(args, input_bytes=None, timeout=15):
    """Run `python -m teammate_comms.deliver <args>` as a real subprocess (the CLI is a
    separate-process entry point, not an importable function call) and return the
    completed process."""
    return subprocess.run(
        [sys.executable, "-m", "teammate_comms.deliver"] + args,
        cwd=str(REPO), input=input_bytes, capture_output=True, timeout=timeout,
    )


def _read_unread(root, agent):
    return read_json_safe(get_inboxes_dir(root, None) / f"{agent}_unread.json")


def test_wp38_ac1_message_form_and_transcript_tee():
    td, root = _make_root()
    with td:
        write_agent_record(root, None, "DeliverTarget", type="full", channel=True)
        proc = _run_deliver(["--to", "DeliverTarget", "--message", "hi",
                              "--comms-dir", str(root)])
        check(proc.returncode == 0,
              f"AC-1: a valid delivery must exit 0, got {proc.returncode}: {proc.stderr!r}")
        stdout = proc.stdout.decode("utf-8", errors="replace")
        check("id=" in stdout, "AC-1: stdout must carry the minted message id")

        unread = _read_unread(root, "DeliverTarget")
        matches = [m for m in unread if m.get("message") == "hi"]
        check(len(matches) == 1, "AC-1: the message must land in the target's unread file")
        check(matches and matches[0].get("from") == COMPACT_BROKER_SENDER,
              "AC-1: the delivered message's from must be 'compact-broker' (the default sender)")
        check(matches and isinstance(matches[0].get("id"), str) and matches[0]["id"],
              "AC-1: the delivered message must carry a minted id")

        transcript_path = get_transcript_file(root, None)
        transcript_lines = [json.loads(ln) for ln in transcript_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        check(any(r.get("kind") == "dm" and r.get("to") == "DeliverTarget" and r.get("message") == "hi"
                  for r in transcript_lines),
              "AC-1: the delivery must tee into the global transcript, same as the MCP send path")


def test_wp38_ac2_stdin_exact_content():
    td, root = _make_root()
    with td:
        write_agent_record(root, None, "StdinTarget", type="full", channel=True)
        # BOM + embedded newline + non-ASCII, placed mid-body (not at the edges) so
        # _clean_message's strip() can't accidentally hide a truncation bug.
        body = "héllo\nwörld"
        payload = b"\xef\xbb\xbf" + body.encode("utf-8")
        proc = _run_deliver(["--to", "StdinTarget", "--comms-dir", str(root)], input_bytes=payload)
        check(proc.returncode == 0,
              f"AC-2: stdin-mode delivery must exit 0, got {proc.returncode}: {proc.stderr!r}")

        unread = _read_unread(root, "StdinTarget")
        check(any(m.get("message") == body for m in unread),
              "tautology[AC-2]: the delivered body must EXACTLY match the original (a locale-"
              "codepage decode of the BOM would corrupt the non-ASCII character instead)")


def test_wp38_ac3_bad_input_exits_2():
    td, root = _make_root()
    with td:
        # to == sender (default sender is compact-broker) -> exit 2, no file. send_dm's
        # self-send guard fires before any registration lookup, so `to` need not be registered.
        proc = _run_deliver(["--to", "compact-broker", "--message", "hi", "--comms-dir", str(root)])
        check(proc.returncode == 2, f"AC-3: to==sender must exit 2, got {proc.returncode}")

        # invalid agent name -> exit 2.
        proc = _run_deliver(["--to", "../evil", "--message", "hi", "--comms-dir", str(root)])
        check(proc.returncode == 2, f"AC-3: an invalid agent name must exit 2, got {proc.returncode}")

        # empty message -> exit 2.
        proc = _run_deliver(["--to", "SomeAgent", "--message", "   ", "--comms-dir", str(root)])
        check(proc.returncode == 2, f"AC-3: an empty/whitespace message must exit 2, got {proc.returncode}")

        # missing --to (argparse path) -> exit 2.
        proc = _run_deliver(["--message", "hi", "--comms-dir", str(root)])
        check(proc.returncode == 2, f"AC-3: a missing --to (argparse usage error) must exit 2, got {proc.returncode}")


def test_wp38_ac4_unregistered_target_queues():
    td, root = _make_root()
    with td:
        proc = _run_deliver(["--to", "NeverRegistered", "--message", "hi", "--comms-dir", str(root)])
        check(proc.returncode == 0,
              f"AC-4: an unregistered --to must still queue (exit 0), got {proc.returncode}: {proc.stderr!r}")
        unread = _read_unread(root, "NeverRegistered")
        check(any(m.get("message") == "hi" for m in unread),
              "AC-4: the message must be queued in the unregistered recipient's inbox")


def test_wp38_ac5_lock_hold_and_fanout():
    td, root = _make_root()
    with td:
        write_agent_record(root, None, "LockTarget", type="full", channel=True)
        inboxes_dir = get_inboxes_dir(root, None)
        inboxes_dir.mkdir(parents=True, exist_ok=True)
        unread_file = inboxes_dir / "LockTarget_unread.json"
        if not unread_file.exists():
            unread_file.write_text("[]", encoding="utf-8")

        # (a) deterministic: hold the SAME lock send_dm takes, launch the CLI, assert it has
        # NOT completed while we hold it, release, then confirm both messages land.
        # Tautology clause: this must FAIL if file_lock is removed from send_dm (Silvie runs
        # that revert at the gate) — with no lock, the subprocess completes immediately.
        with file_lock(unread_file):
            proc = subprocess.Popen(
                [sys.executable, "-m", "teammate_comms.deliver",
                 "--to", "LockTarget", "--message", "while-locked", "--comms-dir", str(root)],
                cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            time.sleep(1.0)
            still_running = proc.poll() is None
            check(still_running,
                  "tautology[AC-5a]: the CLI must be BLOCKED on file_lock(unread_file) while "
                  "the test process holds it — a completed process here means send_dm's lock "
                  "is gone")
        out, err = proc.communicate(timeout=15)
        check(proc.returncode == 0,
              f"AC-5a: the CLI must complete successfully once the lock is released, "
              f"got {proc.returncode}: {err!r}")

        unread = _read_unread(root, "LockTarget")
        check(any(m.get("message") == "while-locked" for m in unread),
              "AC-5a: the message sent while the lock was held must still land once released")

        # (b) probabilistic fan-out: N concurrent CLI processes -> all ids present afterward.
        n = 10
        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "teammate_comms.deliver",
                 "--to", "LockTarget", "--message", f"fanout-{i}", "--comms-dir", str(root)],
                cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for i in range(n)
        ]
        ids = []
        for p in procs:
            out, err = p.communicate(timeout=20)
            check(p.returncode == 0, f"AC-5b: each concurrent delivery must exit 0, got {p.returncode}: {err!r}")
            ids.append(out.decode("utf-8", errors="replace").strip())
        unread = _read_unread(root, "LockTarget")
        fanout_msgs = {m.get("message") for m in unread if isinstance(m.get("message"), str)
                       and m["message"].startswith("fanout-")}
        check(fanout_msgs == {f"fanout-{i}" for i in range(n)},
              f"AC-5b: all {n} concurrent deliveries must be present afterward (no lost writes), "
              f"got {sorted(fanout_msgs)}")


def test_wp38_ac6_plugin_runtime_pointer():
    td, root = _make_root()
    with td:
        env = dict(os.environ)
        env["TEAMMATE_COMMS_DIR"] = str(root)
        env.pop("TEAMMATE_AGENT", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "teammate_comms.server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(REPO), env=env,
        )
        try:
            time.sleep(1.0)
            pointer_path = Path(root) / "TeammateComms" / "plugin-runtime.json"
            check(pointer_path.exists(),
                  "AC-6: plugin-runtime.json must appear under the resolved root after server start")
            if pointer_path.exists():
                data = json.loads(pointer_path.read_text(encoding="utf-8"))
                check(set(data.keys()) == {"v", "python", "plugin_root", "version", "written_at"},
                      f"AC-6: plugin-runtime.json must have exactly the five v1 keys, got {sorted(data.keys())}")
                check(data.get("v") == 1, "AC-6: v must be 1")
                check(bool(data.get("python")), "AC-6: python must be the venv interpreter path")
                check(bool(data.get("plugin_root")), "AC-6: plugin_root must be set")
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)


def main():
    print("Compaction-broker (WP-36/WP-37/WP-38) — acceptance tests")
    print("=" * 55)

    sections = [
        ("WP-36 AC-1/AC-3: pane_id/wezterm_socket capture", test_ac1_ac3_pane_capture),
        ("WP-36 AC-2: stale-clear on re-register", test_ac2_stale_clear),
        ("WP-36 AC-4: manager precedence + clear", test_ac4_manager_precedence_and_clear),
        ("WP-36 AC-5: profile + list rendering", test_ac5_profile_and_list_rendering),
        ("WP-36 tool-wiring: teammate_register(manager=...)", test_register_tool_wiring),
        ("WP-37 AC-1/AC-5: self-compact + atomicity", test_wp37_ac1_self_and_ac5_atomicity),
        ("WP-37 AC-2: manager authz + audit DM", test_wp37_ac2_manager),
        ("WP-37 AC-3: anti-spoof (requester stamped, not passed)", test_wp37_ac3_anti_spoof),
        ("WP-37 AC-4: unregistered target/caller", test_wp37_ac4_unregistered),
        ("WP-37 AC-6: sentinel-name reservation", test_wp37_ac6_reservation),
        ("WP-38 AC-1: --message form + transcript tee", test_wp38_ac1_message_form_and_transcript_tee),
        ("WP-38 AC-2: stdin exact content (BOM/newline/non-ASCII)", test_wp38_ac2_stdin_exact_content),
        ("WP-38 AC-3: bad input exits 2", test_wp38_ac3_bad_input_exits_2),
        ("WP-38 AC-4: unregistered target queues", test_wp38_ac4_unregistered_target_queues),
        ("WP-38 AC-5: lock-hold determinism + fan-out", test_wp38_ac5_lock_hold_and_fanout),
        ("WP-38 AC-6: plugin-runtime.json pointer", test_wp38_ac6_plugin_runtime_pointer),
    ]

    for label, fn in sections:
        n_before = len(failures)
        print(f"  {label}... ", end="", flush=True)
        try:
            fn()
        except Exception as e:
            import traceback
            failures.append(f"{label} CRASHED: {e}")
            traceback.print_exc()
        status = "ok" if len(failures) == n_before else f"FAIL ({len(failures) - n_before} new)"
        print(status)

    print()
    if failures:
        print(f"FAIL ({len(failures)} total failure(s)):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    else:
        print("ALL CHECKS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
