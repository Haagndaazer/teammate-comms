"""Codex parity suite (WP-43..WP-46) — acceptance tests.

WP-43: harness module + registration handoff.
  AC-1: harness table — unset -> claude-code; codex -> codex; unknown -> claude-code with
        exactly one stderr warning per unknown value.
  AC-2: register stores `harness` + `harness_session`; omitting harness_session on a later
        register clears it (stored None); the session hint appears only when the harness
        needs a session and none was given.
  AC-3: `project_dir` arg beats CLAUDE_PROJECT_DIR for the project label and need not exist.
  AC-4: a same-identity re-register does not bump the identity generation; a changed
        harness_session bumps only the waker generation; a different agent bumps identity.
  AC-5: list/profile/whoami render `harness:` for agents, `(not set)` for pre-upgrade
        records, and never for the human operator.
  AC-6: non-string harness_session / project_dir via dispatch -> clean isError.

Run: uv run --no-sync python tests/test_codex.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

from teammate_comms import channel as channel_mod
from teammate_comms import harness as harness_mod
from teammate_comms import server as server_mod
from teammate_comms import tools as tools_mod
from teammate_comms.comms import (
    ensure_inbox,
    get_inboxes_dir,
    read_agent_record,
    register_human,
    write_agent_record,
    write_json_atomic,
)

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


@contextlib.contextmanager
def env_vars(**kv):
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


class FakeIdentity:
    def __init__(self, agent, team, root):
        self._agent, self._team, self._root = agent, team, root

    def snapshot(self):
        return (self._agent, self._team, self._root, None)


def _ctx_for(agent, team, root):
    return {"identity": FakeIdentity(agent, team, root), "register": server_mod.register_identity,
            "auto_register_error": lambda: None}


def _profile_field_map(text):
    out = {}
    for ln in text.splitlines():
        if ":" in ln:
            k, _, v = ln.partition(":")
            out[k.strip()] = v.strip()
    return out


SESSION = "01a08225-2258-7cf1-aca5-d690e1a52f7e"
HINT_MARK = "Re-register with harness_session"


def test_wp43_ac1_harness_table():
    with env_vars(TEAMMATE_HARNESS=None):
        check(harness_mod.current().name == "claude-code", "AC-1: unset env must resolve to claude-code")
    with env_vars(TEAMMATE_HARNESS="codex"):
        h = harness_mod.current()
        check(h.name == "codex" and h.wake == "codex-queue" and h.durable_wake and h.needs_session,
              "AC-1: TEAMMATE_HARNESS=codex must resolve to the codex row")
    with env_vars(TEAMMATE_HARNESS="  Codex "):
        check(harness_mod.current().name == "codex", "AC-1: value is trimmed + case-folded")
    err = io.StringIO()
    with env_vars(TEAMMATE_HARNESS="hermes-9"), contextlib.redirect_stderr(err):
        first = harness_mod.current()
        second = harness_mod.current()
    check(first.name == "claude-code" and second.name == "claude-code",
          "AC-1: unknown harness must fall back to claude-code")
    check(err.getvalue().count("unknown TEAMMATE_HARNESS") == 1,
          "AC-1: exactly one warning per unknown value, not one per call")
    check(harness_mod.by_name("CODEX") is harness_mod.HARNESSES["codex"]
          and harness_mod.by_name(None) is None,
          "AC-1: by_name folds case and rejects non-strings")
    for row in harness_mod.HARNESSES.values():
        check(row.needs_session == bool(row.session_hint),
              f"AC-1: {row.name}: session_hint must be set iff needs_session")


def test_wp43_ac2_register_stores_and_clears_session():
    td, root = _make_root()
    with td, env_vars(TEAMMATE_HARNESS="codex", CLAUDE_PROJECT_DIR=None):
        result = server_mod.register_identity("Cx", None, str(root), harness_session=SESSION)
        rec = read_agent_record(root, None, "Cx")
        check(rec.get("harness") == "codex", "AC-2: record must carry harness=codex")
        check(rec.get("harness_session") == SESSION, "AC-2: record must carry the given session id")
        check(HINT_MARK not in result, "AC-2: no session hint when the session was given")
        check(server_mod._identity.get_harness_session() == SESSION,
              "AC-2: Identity must hold the session for the watcher")

        result = server_mod.register_identity("Cx", None, str(root))
        rec = read_agent_record(root, None, "Cx")
        check("harness_session" in rec and rec["harness_session"] is None,
              "tautology[AC-2]: omitting harness_session must store None, not preserve the old id")
        check(HINT_MARK in result, "AC-2: session hint must appear when a needs_session harness "
                                   "registers without one")
        check(server_mod._identity.get_harness_session() is None,
              "AC-2: Identity session must be cleared too")

        result = server_mod.register_identity("Cx", None, str(root), harness_session="  x  y ")
        check(read_agent_record(root, None, "Cx").get("harness_session") == "x y",
              "AC-2: session id is whitespace-collapsed")
        try:
            server_mod.register_identity("Cx", None, str(root), harness_session="--thread")
            check(False, "AC-2: a session id starting with '-' must be rejected")
        except Exception as e:
            check("must not start with '-'" in str(e), f"AC-2: leading-dash rejection text ({e})")
    td2, root2 = _make_root()
    with td2, env_vars(TEAMMATE_HARNESS=None, CLAUDE_PROJECT_DIR=None):
        result = server_mod.register_identity("Cl", None, str(root2))
        rec = read_agent_record(root2, None, "Cl")
        check(rec.get("harness") == "claude-code", "AC-2: default harness recorded as claude-code")
        check(HINT_MARK not in result, "AC-2: claude-code never emits the session hint")
        check(result.endswith("teammate_inbox to read them."),
              "AC-2: claude-code register text ends exactly as in v0.15.0")


def test_wp43_ac3_project_dir_arg():
    td, root = _make_root()
    with td, env_vars(TEAMMATE_HARNESS=None, CLAUDE_PROJECT_DIR="C:/some/path/envproj"):
        server_mod.register_identity("Pd", None, str(root),
                                     project_dir="E:/definitely/missing/argproj")
        rec = read_agent_record(root, None, "Pd")
        check(rec.get("project") == "missing/argproj",
              f"AC-3: explicit project_dir must win over env and need not exist (got {rec.get('project')!r})")
        server_mod.register_identity("Pd2", None, str(root))
        check(read_agent_record(root, None, "Pd2").get("project") == "path/envproj",
              "AC-3: without the arg the env auto-fill is unchanged")
        server_mod.register_identity("Pd3", None, str(root), project_dir="   ")
        check(read_agent_record(root, None, "Pd3").get("project") == "path/envproj",
              "AC-3: a blank project_dir falls through to env")


def test_wp43_ac4_generation_semantics():
    td, root = _make_root()
    with td, env_vars(TEAMMATE_HARNESS="codex", CLAUDE_PROJECT_DIR=None):
        server_mod.register_identity("Gen", None, str(root))
        g1, w1 = server_mod._identity.get_generation(), server_mod._identity.get_waker_generation()
        server_mod.register_identity("Gen", None, str(root), harness_session=SESSION)
        g2, w2 = server_mod._identity.get_generation(), server_mod._identity.get_waker_generation()
        check(g2 == g1, "tautology[AC-4]: same-identity re-register must NOT bump the identity generation")
        check(w2 == w1 + 1, "AC-4: a new harness_session must bump the waker generation once")
        server_mod.register_identity("Gen", None, str(root), harness_session=SESSION)
        g3, w3 = server_mod._identity.get_generation(), server_mod._identity.get_waker_generation()
        check(g3 == g2 and w3 == w2, "AC-4: an unchanged re-register bumps neither generation")
        server_mod.register_identity("Gen", None, str(root))
        check(server_mod._identity.get_waker_generation() == w3 + 1,
              "AC-4: clearing the session bumps the waker generation")
        server_mod.register_identity("Other", None, str(root))
        check(server_mod._identity.get_generation() == g3 + 1,
              "AC-4: a different agent bumps the identity generation")


def test_wp43_ac5_render():
    td, root = _make_root()
    with td:
        with env_vars(TEAMMATE_HARNESS="codex", CLAUDE_PROJECT_DIR=None):
            server_mod.register_identity("Rx", None, str(root), harness_session=SESSION)
        register_human(root, None, "human")
        legacy = read_agent_record(root, None, "Rx")
        write_agent_record(root, None, "Legacy", type="full", channel=False)
        ctx = _ctx_for("Rx", None, root)
        text = tools_mod._handle_list({"all": True}, ctx)
        lines = text.splitlines()
        check("      harness:   codex" in lines, "AC-5: list must render harness: codex for Rx")
        check("      harness:   (not set)" in lines, "AC-5: list renders (not set) for a pre-upgrade record")
        human_idx = next(i for i, ln in enumerate(lines) if ln.startswith("  - human"))
        check(not lines[human_idx + 1].strip().startswith("harness:"),
              "AC-5: the human operator gets no harness line")
        prof = _profile_field_map(tools_mod._format_profile(legacy, "Rx", is_self=True, root=root, team=None))
        check(prof.get("harness") == "codex", "AC-5: profile renders harness")
        hrec = read_agent_record(root, None, "human")
        htext = tools_mod._format_profile(hrec, "human", root=root, team=None)
        check("harness:" not in htext, "AC-5: human profile has no harness line")
        who = json.loads(tools_mod._handle_whoami({}, ctx))
        check(who.get("harness") == "codex" and who.get("harness_session") == SESSION,
              "AC-5: whoami exposes harness + harness_session")


def test_wp43_ac6_dispatch_non_str():
    td, root = _make_root()
    with td, env_vars(TEAMMATE_HARNESS=None, TEAMMATE_COMMS_DIR=str(root)):
        ctx = _ctx_for(None, None, None)
        text, is_error = tools_mod.dispatch("teammate_register",
                                            {"agent": "Bad", "harness_session": 123}, ctx)
        check(is_error and "harness_session" in text, "AC-6: non-str harness_session -> clean isError")
        text, is_error = tools_mod.dispatch("teammate_register",
                                            {"agent": "Bad", "project_dir": ["x"]}, ctx)
        check(is_error and "project_dir" in text, "AC-6: non-str project_dir -> clean isError")
        check(read_agent_record(root, None, "Bad") is None, "AC-6: no record written on rejected input")


class WatcherIdentity:
    def __init__(self, agent, root, session):
        self.agent, self.root, self.session = agent, root, session
        self.generation, self.waker_generation = 1, 1
        self.unread_file = get_inboxes_dir(root, None) / f"{agent}_unread.json"

    def snapshot_with_generation(self):
        return (self.agent, None, self.root, self.unread_file, self.generation)

    def snapshot_waker(self):
        return (self.session, self.waker_generation)

    def get_instance_id(self):
        return "test-instance"

    def get_epoch(self):
        return 1


class Runner:
    def __init__(self, block=None, sleep=0.0, raise_once=False):
        self.calls, self.block, self.sleep, self.raise_once = [], block, sleep, raise_once
        self.started = threading.Event()

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.started.set()
        if self.raise_once:
            self.raise_once = False
            raise OSError("codex missing")
        if self.block is not None:
            self.block.wait(10)
        if self.sleep:
            time.sleep(self.sleep)
        return types.SimpleNamespace(returncode=0)


@contextlib.contextmanager
def watcher(harness, session, runner=None, sent=None, hb=None):
    td, root = _make_root()
    agent = "Wx"
    ensure_inbox(get_inboxes_dir(root, None), agent)
    ident = WatcherIdentity(agent, root, session)
    init, reg, stop = threading.Event(), threading.Event(), threading.Event()
    init.set()
    reg.set()
    sent = [] if sent is None else sent
    factory = (lambda h, s, sm: channel_mod.make_waker(h, s, sm, runner=runner, exe="codex"))
    old_hb = channel_mod.HEARTBEAT_SECONDS
    if hb is not None:
        channel_mod.HEARTBEAT_SECONDS = hb
    err = io.StringIO()
    with td, env_vars(TEAMMATE_HARNESS=harness), contextlib.redirect_stderr(err):
        t = threading.Thread(target=channel_mod.run_watcher,
                             args=(sent.append, ident, init, reg, stop),
                             kwargs={"waker_factory": factory}, daemon=True)
        t.start()
        time.sleep(1.2)
        try:
            yield types.SimpleNamespace(root=root, agent=agent, ident=ident, sent=sent, err=err)
        finally:
            stop.set()
            t.join(5)
            channel_mod.HEARTBEAT_SECONDS = old_hb


def _drop(root, agent, frm, text):
    f = get_inboxes_dir(root, None) / f"{agent}_unread.json"
    msgs = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    msgs.append({"id": f"{time.time():.6f}-{len(msgs)}", "from": frm, "priority": "normal",
                 "message": text})
    write_json_atomic(f, msgs)


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def test_wp44_ac1_codex_fresh_wake_argv():
    runner = Runner()
    with watcher("codex", SESSION, runner=runner) as w:
        _drop(w.root, w.agent, "bob", "hi")
        check(_wait(lambda: len(runner.calls) == 1), "AC-1: one codex queue call for a fresh message")
        check(runner.calls and runner.calls[0] == ["codex", "queue", "--thread", SESSION,
                                                    "--message", "📬 1 new message(s) from bob."],
              f"AC-1: exact argv (got {runner.calls[:1]})")
        time.sleep(1.0)
        check(len(runner.calls) == 1, "AC-1: no duplicate wake for the same message")
        check(not w.sent, "AC-1: no channel notification on codex")
        check("wake-emit kind=fresh" in w.err.getvalue() and "harness=codex rc=0" in w.err.getvalue(),
              "AC-1: breadcrumbs for the emit and the runner outcome")


def test_wp44_ac2_dropped_wake_not_retired():
    gate = threading.Event()
    runner = Runner(block=gate)
    with watcher("codex", SESSION, runner=runner) as w:
        _drop(w.root, w.agent, "bob", "one")
        check(_wait(lambda: runner.started.is_set()), "AC-2: first wake dispatched")
        _drop(w.root, w.agent, "carol", "two")
        time.sleep(1.5)
        check(len(runner.calls) == 1, "AC-2: second wake dropped while the first is in flight")
        check(w.err.getvalue().count("dropped-busy") == 1, "AC-2: dropped-busy logged once per episode")
        gate.set()
        check(_wait(lambda: len(runner.calls) == 2), "tautology[AC-2]: dropped wake retried after the runner returns")
        check(len(runner.calls) >= 2 and "carol" in runner.calls[1][-1] and "2 new" in runner.calls[1][-1],
              f"AC-2: retry content recomputed from the current unseen set (got {runner.calls[1:]})")


def test_wp44_ac3_heartbeat_not_delayed():
    runner = Runner(sleep=3.0)
    with watcher("codex", SESSION, runner=runner, hb=1) as w:
        _drop(w.root, w.agent, "bob", "slow")
        check(_wait(lambda: runner.started.is_set()), "AC-3: wake dispatched")
        stamps = set()
        deadline = time.monotonic() + 2.8
        while time.monotonic() < deadline:
            rec = read_agent_record(w.root, None, w.agent) or {}
            if rec.get("lastHeartbeat"):
                stamps.add(rec["lastHeartbeat"])
            time.sleep(0.2)
        check(len(stamps) >= 2, f"tautology[AC-3]: heartbeats keep flowing while the runner blocks (saw {len(stamps)})")


def test_wp44_ac4_no_session_no_wake():
    runner = Runner()
    with watcher("codex", None, runner=runner) as w:
        _drop(w.root, w.agent, "bob", "hi")
        time.sleep(1.5)
        check(runner.calls == [], "AC-4: no session -> zero runner calls")
        check(w.err.getvalue().count("wake disabled") == 1, "AC-4: one 'wake disabled' line")
        w.ident.session = SESSION
        w.ident.waker_generation += 1
        check(_wait(lambda: len(runner.calls) == 1), "AC-4: waker re-resolves on waker_generation and wakes")


def test_wp44_ac5_renudge_gate_and_claude_golden():
    old = channel_mod.REEMIT_BASE_SECONDS
    channel_mod.REEMIT_BASE_SECONDS = 0.3
    try:
        runner = Runner()
        with watcher("codex", SESSION, runner=runner) as w:
            _drop(w.root, w.agent, "bob", "hi")
            check(_wait(lambda: len(runner.calls) == 1), "AC-5: codex fresh wake")
            time.sleep(1.5)
            check(len(runner.calls) == 1, "tautology[AC-5]: no re-nudge on a durable-wake harness")
        with watcher(None, None) as w:
            _drop(w.root, w.agent, "bob", "hi")
            check(_wait(lambda: len(w.sent) >= 1), "AC-5: claude fresh wake")
            golden = {"jsonrpc": "2.0", "method": "notifications/claude/channel",
                      "params": {"content": "📬 1 new message(s) from bob.",
                                 "meta": {"count": "1", "agent": w.agent}}}
            check(w.sent and w.sent[0] == golden, f"AC-5: claude payload golden (got {w.sent[:1]})")
            check(_wait(lambda: len(w.sent) >= 2, timeout=3), "AC-5: claude still re-nudges")
    finally:
        channel_mod.REEMIT_BASE_SECONDS = old


def test_wp44_ac6_runner_failure_isolated():
    runner = Runner(raise_once=True)
    with watcher("codex", SESSION, runner=runner) as w:
        _drop(w.root, w.agent, "bob", "one")
        check(_wait(lambda: len(runner.calls) == 1), "AC-6: first wake attempted")
        check(_wait(lambda: "error=codex missing" in w.err.getvalue()), "AC-6: failure logged, not raised")
        _drop(w.root, w.agent, "dave", "two")
        check(_wait(lambda: len(runner.calls) == 2), "AC-6: watcher keeps running after a runner error")


def test_wp44_ac8_codex_exe_resolution():
    calls = []
    runner = lambda argv, **kw: calls.append(list(argv)) or types.SimpleNamespace(returncode=0)
    with env_vars(PATH=""):
        waker = channel_mod.CodexQueueWaker(SESSION, runner=runner)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            first, second = waker.wake("x", {}), waker.wake("y", {})
        check(first is False and second is False and calls == [],
              "tautology[AC-8]: no codex on PATH -> wake reports not dispatched, nothing runs")
        check(err.getvalue().count("codex CLI not on PATH") == 1, "AC-8: missing CLI logged once")
    waker = channel_mod.CodexQueueWaker(SESSION, runner=runner, exe=r"C:\x\codex.CMD")
    check(waker.wake("z", {}) is True, "AC-8: explicit exe dispatches")
    check(_wait(lambda: calls and calls[0][0] == r"C:\x\codex.CMD", timeout=3),
          f"AC-8: argv[0] is the resolved executable, never the bare name ({calls[:1]})")
    if os.name == "nt":
        import shutil
        resolved = shutil.which("codex")
        if resolved:
            check(resolved.lower().endswith((".cmd", ".exe", ".bat")),
                  "AC-8: on Windows shutil.which returns a launchable shim, which bare 'codex' is not")


def test_wp44_ac7_reaction_drop_not_retired():
    gate = threading.Event()
    runner = Runner(block=gate)
    with watcher("codex", SESSION, runner=runner, hb=1) as w:
        _drop(w.root, w.agent, "bob", "one")
        check(_wait(lambda: runner.started.is_set()), "AC-7: message wake in flight")
        rx = get_inboxes_dir(w.root, None).parent / "reactions.jsonl"
        with open(rx, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": f"rx-{time.time():.6f}", "target": "m1", "target_from": w.agent,
                                 "from": "erin", "emoji": "fire", "op": "add"}) + "\n")
        time.sleep(2.5)
        check(len(runner.calls) == 1, "AC-7: reaction wake dropped while busy")
        gate.set()
        check(_wait(lambda: any("reacted to your message" in c[-1] for c in runner.calls), timeout=6),
              f"tautology[AC-7]: dropped reaction wake retried later (got {runner.calls})")


LITERAL_BASELINE = {
    "__init__.py": 1, "avatars.py": 1, "channel.py": 18, "comms.py": 5, "dashboard.py": 0,
    "deliver.py": 1, "server.py": 9, "spawn.py": 30, "tools.py": 6,
}
SKILL_FORBIDDEN = ("claude code", "sendmessage", "/mcp", "~/.claude/debug", "--channels",
                   "$teammate-comms", "codex")


def test_wp45_ac1_manifest_parity():
    claude = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    codex = json.loads((REPO / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    check(claude["name"] == codex["name"] == "teammate-comms", "AC-1: manifest names agree")
    check(claude["version"] == codex["version"], "AC-1: manifest versions agree")
    check(f'version = "{codex["version"]}"' in pyproject, "AC-1: pyproject version matches manifests")
    import teammate_comms
    check(teammate_comms.__version__ == codex["version"],
          f"AC-1: package __version__ ({teammate_comms.__version__}) matches the manifests ({codex['version']})")
    for key in ("skills", "hooks"):
        for rel in codex.get(key, []):
            check(rel.startswith("./"), f"AC-1: codex manifest path must start with ./ ({rel})")
            check((REPO / rel).exists(), f"AC-1: codex manifest path exists ({rel})")
    check("mcpServers" not in codex and "channels" not in codex,
          "AC-1: codex manifest ships no MCP server or channel (hook-managed registration)")
    hooks = json.loads((REPO / "adapters" / "codex" / "hooks.json").read_text(encoding="utf-8"))
    handler = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    win = handler["commandWindows"]
    check('"' not in win and win.endswith("session-start.cmd"), "AC-1: commandWindows is one unquoted path")
    market = json.loads((REPO / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
    src = market["plugins"][0]["source"]
    check(src["source"] in ("local", "url", "git-subdir", "npm"), "AC-1: marketplace source tag is one Codex accepts")


def test_wp45_ac2_parity_completeness():
    parity = (REPO / "docs" / "PARITY.md").read_text(encoding="utf-8")
    rows = {ln.split("|")[1].strip() for ln in parity.splitlines() if ln.startswith("| ")}
    for tool in tools_mod.TOOL_DEFINITIONS:
        check(tool["name"] in rows, f"AC-2: PARITY.md lacks a row for tool {tool['name']}")
    for script in sorted((REPO / "hooks").glob("*.sh")):
        check(f"hooks/{script.name}" in rows, f"AC-2: PARITY.md lacks a row for hooks/{script.name}")
    for ln in parity.splitlines():
        if not ln.startswith("| ") or ln.startswith("| Surface") or ln.startswith("|---"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if any(c in ("partial", "waived") for c in cells[1:3]):
            check(bool(cells[3]), f"AC-2: non-full row needs a note ({cells[0]})")


def test_wp45_ac3_skill_lint():
    text = (REPO / "skills" / "teammate-comms" / "SKILL.md").read_text(encoding="utf-8")
    check("## Harness notes" in text, "AC-3: SKILL.md has a Harness notes section")
    body = text.split("## Harness notes")[0].lower()
    for word in SKILL_FORBIDDEN:
        check(word not in body, f"AC-3: harness literal {word!r} outside Harness notes")
    check("harness_session" in text, "AC-3: SKILL documents harness_session")


def test_wp45_ac4_literal_ratchet():
    import re
    pat = re.compile(r"codex|claude", re.IGNORECASE)
    for path in sorted((SRC / "teammate_comms").glob("*.py")):
        if path.name == "harness.py":
            continue
        n = len(pat.findall(path.read_text(encoding="utf-8")))
        base = LITERAL_BASELINE.get(path.name)
        check(base is not None, f"AC-4: no literal baseline for {path.name}")
        if base is not None:
            check(n <= base, f"AC-4: {path.name} has {n} harness literals, baseline {base} — "
                             f"move the fact into harness.py or raise the baseline deliberately")


def _bash():
    import shutil
    b = shutil.which("bash")
    if b and "system32" in b.lower():
        for cand in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if Path(cand).exists():
                return cand
        return None
    return b


def _posix(path):
    import subprocess
    bash = _bash()
    if os.name != "nt" or not bash:
        return str(path)
    r = subprocess.run([bash, "-c", f'cygpath -u "{path}"'], capture_output=True, text=True)
    return r.stdout.strip() or str(path)


def test_wp45_ac5_codex_hook_shell():
    import subprocess
    bash = _bash()
    if not bash:
        print("(bash absent — skipping) ", end="")
        return
    td, root = _make_root()
    with td:
        rig = root / "rig"
        (rig / "bin").mkdir(parents=True)
        data = rig / "data"
        posix_rig = _posix(rig)
        fake_codex = (f'#!/usr/bin/env bash\necho "CODEX: $*" >> "{posix_rig}/codex.log"\n'
                      f'case "$*" in "mcp get "*) if [ -f "{posix_rig}/mcp_get_json" ]; then '
                      f'cat "{posix_rig}/mcp_get_json"; exit 0; fi; exit 1;; esac\nexit 0\n')
        for name, body in (("codex", fake_codex),
                           ("uv", f'#!/usr/bin/env bash\necho "UV: $*" >> "{posix_rig}/uv.log"\nexit 0\n')):
            p = rig / "bin" / name
            p.write_text(body, encoding="utf-8", newline="\n")
            p.chmod(0o755)
        script = _posix(REPO / "adapters" / "codex" / "hooks" / "session-start.sh")
        env = dict(os.environ)
        env.update({"PATH": str(rig / "bin") + os.pathsep + env.get("PATH", ""),
                    "CLAUDE_PLUGIN_ROOT": str(REPO), "CLAUDE_PLUGIN_DATA": str(data)})
        env.pop("TEAMMATE_HOOK_NOTE", None)
        env.pop("UV_PROJECT_ENVIRONMENT", None)

        def run(payload):
            return subprocess.run([bash, script], input=payload, capture_output=True, text=True,
                                  env=env, timeout=120)

        r1 = run('{"session_id":"01a08231-8f03-7120-9cac-06f6570eb99d","cwd":"E:\\\\E Drive Projects\\\\x","source":"startup"}')
        check(r1.returncode == 0, f"AC-5: first run exits 0 ({r1.stderr[-300:]})")
        out = json.loads(r1.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        check("thread_id=01a08231-8f03-7120-9cac-06f6570eb99d" in ctx, "AC-5: note carries the thread id")
        check("project_dir=E:\\E Drive Projects\\x" in ctx, f"AC-5: note carries the unescaped cwd ({ctx[:120]})")
        check("restart Codex once" in ctx, "AC-5: first run carries the restart note")
        log = (rig / "codex.log").read_text(encoding="utf-8") if (rig / "codex.log").exists() else ""
        calls = [ln for ln in log.splitlines() if ln.startswith("CODEX: mcp add teammate-comms")]
        check(len(calls) == 1, f"AC-5: exactly one codex mcp add ({log!r})")
        check(calls and "--env TEAMMATE_HARNESS=codex" in calls[0] and "--project" in calls[0]
              and calls[0].rstrip().endswith("python -m teammate_comms.server"),
              f"AC-5: registration argv shape ({calls[:1]})")
        check((data / "codex-mcp.stamp").exists(), "AC-5: stamp written after registration")
        check(data.is_dir(), "AC-5: hook creates the plugin data dir")

        r2 = run('{"session_id":"0000-1111","cwd":"/home/x/we \\"q\\" dir","source":"resume"}')
        ctx2 = json.loads(r2.stdout)["hookSpecificOutput"]["additionalContext"]
        check('project_dir=/home/x/we "q" dir' in ctx2, f"AC-5: quoted POSIX cwd round-trips ({ctx2[:120]})")
        check("restart Codex once" not in ctx2, "AC-5: no restart note when the stamp matches")
        log = (rig / "codex.log").read_text(encoding="utf-8")
        check(log.count("mcp add") == 1, "tautology[AC-5]: stamp match spawns no second codex mcp add")

        decoy = ('{"note":"x \\"session_id\\":\\"decoy\\" \\"cwd\\":\\"/decoy\\"",'
                 '"session_id":"0000-real","cwd":"/real","source":"resume",'
                 '"tail":"\\"cwd\\":\\"/late\\""}')
        rd = run(decoy)
        ctxd = json.loads(rd.stdout)["hookSpecificOutput"]["additionalContext"]
        check("thread_id=0000-real project_dir=/real." in ctxd,
              f"AC-5: escaped decoy keys inside string values never win ({ctxd[:120]})")

        r3 = run('{"session_id":"0000-2222","cwd":"/tmp","source":"compact"}')
        check(r3.stdout.strip() == "{}", f"AC-5: compact source still self-filters to {{}} ({r3.stdout!r})")

        pretty = ('{\n  "name": "teammate-comms",\n  "env": {\n    "TEAMMATE_HARNESS": "codex",\n'
                  '    "TEAMMATE_REINCARNATE_ENABLED": "1",\n    "UV_PROJECT_ENVIRONMENT": "old"\n  },\n'
                  '  "cwd": "/somewhere"\n}\n')
        (rig / "mcp_get_json").write_text(pretty, encoding="utf-8", newline="\n")
        env["CLAUDE_PLUGIN_DATA"] = str(rig / "data2")
        run('{"session_id":"0000-3333","cwd":"/tmp","source":"startup"}')
        log = (rig / "codex.log").read_text(encoding="utf-8")
        adds = [ln for ln in log.splitlines() if ln.startswith("CODEX: mcp add")]
        check(len(adds) == 2 and "--env TEAMMATE_REINCARNATE_ENABLED=1" in adds[-1],
              f"tautology[AC-5]: re-registration preserves a user-added env key ({adds[-1:]})")
        check(adds[-1].count("--env TEAMMATE_HARNESS=") == 1 and adds[-1].count("--env UV_PROJECT_ENVIRONMENT=") == 1
              and "--env cwd" not in adds[-1] and "old" not in adds[-1],
              f"AC-5: managed keys passed once with fresh values, sibling fields untouched ({adds[-1]})")
        compact = '{\n  "name": "teammate-comms",\n  "env": {},\n  "cwd": "/somewhere"\n}\n'
        (rig / "mcp_get_json").write_text(compact, encoding="utf-8", newline="\n")
        env["CLAUDE_PLUGIN_DATA"] = str(rig / "data3")
        run('{"session_id":"0000-4444","cwd":"/tmp","source":"startup"}')
        adds = [ln for ln in (rig / "codex.log").read_text(encoding="utf-8").splitlines()
                if ln.startswith("CODEX: mcp add")]
        check(len(adds) == 3 and adds[-1].count("--env ") == 2 and "cwd" not in adds[-1],
              f"AC-5: a compact empty env block yields no extra args and never swallows cwd ({adds[-1]})")
        env["CLAUDE_PLUGIN_DATA"] = str(data)

        env_plain = dict(env)
        env_plain.pop("TEAMMATE_HOOK_NOTE", None)
        r4 = subprocess.run([bash, _posix(REPO / "hooks" / "session-start.sh")], input="",
                            capture_output=True, text=True, env=env_plain, timeout=120)
        check(r4.returncode == 0 and (r4.stdout.strip() == "{}" or "restart Claude Code" in r4.stdout),
              f"AC-5: claude path output unchanged ({r4.stdout[:120]!r})")


def test_wp46_ac1_codex_builder_argv():
    from teammate_comms import spawn as spawn_mod
    import shutil
    with env_vars(TEAMMATE_LAUNCH_ARGS=None, TEAMMATE_LAUNCH_ARGS_CODEX=None, PATH=""):
        argv = spawn_mod.build_command("codex", "hello world", "E:/proj dir")
    check(argv == ["codex", "--dangerously-bypass-approvals-and-sandbox",
                   "-C", "E:/proj dir", "hello world"], f"AC-1: codex argv ({argv})")
    check("-a" not in argv, "tautology[AC-1]: -a cannot be combined with the bypass flag (gate finding)")
    resolved = shutil.which("codex")
    if resolved:
        with env_vars(TEAMMATE_LAUNCH_ARGS_CODEX=None):
            argv = spawn_mod.build_command("codex", "p", "/d")
        check(argv[0] == resolved, f"tautology[AC-1]: argv[0] is the resolved codex shim, not the bare name ({argv[0]})")
    with env_vars(TEAMMATE_LAUNCH_ARGS="claude --channels x", TEAMMATE_LAUNCH_ARGS_CODEX=None):
        argv = spawn_mod.build_command("codex", "p", "/d")
        check(os.path.basename(argv[0]).lower().startswith("codex") and "--channels" not in argv,
              "tautology[AC-1]: a Claude launch override never leaks into the codex argv")
    with env_vars(TEAMMATE_LAUNCH_ARGS_CODEX="codex --profile fast"):
        argv = spawn_mod.build_command("codex", "p", "/d")
        check(argv == ["codex", "--profile", "fast", "p"], f"AC-1: codex override replaces the base line ({argv})")
    with env_vars(TEAMMATE_LAUNCH_ARGS=None):
        argv = spawn_mod.build_command("claude", "p")
        check(argv[0] == "claude" and argv[-1] == "p" and "--permission-mode" in argv,
              f"AC-1: claude builder unchanged ({argv})")
    try:
        spawn_mod.build_command("hermes", "p")
        check(False, "AC-1: unknown builder must raise")
    except Exception as e:
        check("No spawn builder" in str(e), "AC-1: unknown builder error names the gap")


def test_wp46_ac2_which_check_names_executable():
    from teammate_comms import spawn as spawn_mod
    real_popen = spawn_mod.subprocess.Popen

    def forbidden(*a, **k):
        raise AssertionError("Popen must not run when the executable is missing")
    spawn_mod.subprocess.Popen = forbidden
    try:
        with env_vars(PATH=""):
            try:
                spawn_mod.spawn_in_terminal(["codex", "p"], ".", dict(os.environ))
                check(False, "AC-2: missing codex must raise")
            except FileNotFoundError as e:
                check(str(e).startswith("codex CLI not on PATH"), f"AC-2: error names codex ({e})")
            except AssertionError as e:
                check(False, f"AC-2: {e}")
    finally:
        spawn_mod.subprocess.Popen = real_popen


def test_wp46_ac3_reincarnate_codex_prompt():
    from teammate_comms import spawn as spawn_mod
    td, root = _make_root()
    launched = []
    real_spawn = spawn_mod.spawn_in_terminal
    spawn_mod.spawn_in_terminal = lambda argv, cwd, env: launched.append((list(argv), str(cwd), dict(env))) or argv
    try:
        with td, env_vars(TEAMMATE_REINCARNATE_ENABLED="1", TEAMMATE_HARNESS=None,
                          TEAMMATE_LAUNCH_ARGS=None, TEAMMATE_LAUNCH_ARGS_CODEX=None):
            proj = root / "proj"
            proj.mkdir()
            ctx = _ctx_for("Lead", None, root)
            text, is_error = tools_mod.dispatch(
                "teammate_reincarnate",
                {"agent": "Codex1", "project_dir": str(proj), "harness": "codex"}, ctx)
            check(not is_error, f"AC-3: codex reincarnate dispatches ({text[:200]})")
            check(launched and os.path.basename(launched[-1][0][0]).lower().startswith("codex"),
                  f"AC-3: launches the codex CLI ({launched[-1][0][:1] if launched else launched})")
            prompt = launched[-1][0][-1] if launched else ""
            check("teammate_register(agent='Codex1'" in prompt and "harness_session=" in prompt
                  and str(proj) in prompt, f"AC-3: prompt carries the register call ({prompt[:160]})")
            check("Launched on Codex as '" in text, "AC-3: result names the harness")
            text, is_error = tools_mod.dispatch(
                "teammate_reincarnate",
                {"agent": "Claude1", "project_dir": str(proj)}, ctx)
            check(not is_error and launched[-1][0][0] == "claude", "AC-3: default harness spawns claude")
            check("plugin spec" in text, "AC-3: claude result keeps the plugin-spec note")
            text, is_error = tools_mod.dispatch(
                "teammate_reincarnate",
                {"agent": "X", "project_dir": str(proj), "harness": "hermes"}, ctx)
            check(is_error and "'harness' must be one of" in text, "AC-3: unknown harness rejected")
    finally:
        spawn_mod.spawn_in_terminal = real_spawn


@contextlib.contextmanager
def fake_home():
    td, home = _make_root()
    with td, env_vars(HOME=str(home), USERPROFILE=str(home), HOMEDRIVE=None, HOMEPATH=None,
                      TEAMMATE_COMMS_DIR=None, CLAUDE_CONFIG_DIR=None):
        yield home


STALE = "2020-01-01T00:00:00.000000"


def _plant_agent(root, name, heartbeat):
    write_agent_record(root, None, name, type="full", channel=True, lastHeartbeat=heartbeat,
                       pid=999999, host="elsewhere")


def test_wp47_ac1_resolution_order():
    from teammate_comms import comms as comms_mod
    with fake_home() as home:
        root, source = comms_mod.resolve_comms_root(None)
        check(root == home / ".teammate-comms" and "default" in source,
              f"AC-1: fresh machine resolves to ~/.teammate-comms ({root}, {source})")
        with env_vars(CLAUDE_CONFIG_DIR=str(home / "cfg")):
            root, _ = comms_mod.resolve_comms_root(None)
            check(root == home / ".teammate-comms",
                  "tautology[AC-1]: CLAUDE_CONFIG_DIR alone no longer selects the root")
        with env_vars(TEAMMATE_COMMS_DIR=str(home / "iso")):
            root, source = comms_mod.resolve_comms_root(None)
            check(root == home / "iso" and source == "TEAMMATE_COMMS_DIR", "AC-1: env override wins")
        (home / ".claude" / "TeammateComms" / "agents").mkdir(parents=True)
        root, source = comms_mod.resolve_comms_root(None)
        check(root == home / ".claude" and "deferred" in source,
              f"AC-1: pending legacy tree wins while the new root is empty ({root}, {source})")
        (home / ".teammate-comms" / "TeammateComms").mkdir(parents=True)
        root, _ = comms_mod.resolve_comms_root(None)
        check(root == home / ".teammate-comms", "AC-1: an existing new tree wins over a pending legacy one")


def test_wp47_ac2_migration_moves_when_idle():
    from teammate_comms import comms as comms_mod
    with fake_home() as home:
        legacy = home / ".claude"
        _plant_agent(legacy, "Old", STALE)
        (legacy / "TeammateComms" / "inboxes").mkdir(parents=True)
        (legacy / "TeammateComms" / "inboxes" / "Old_unread.json").write_text("[]", encoding="utf-8")
        status, detail = comms_mod.migrate_legacy_root()
        check(status == "migrated", f"AC-2: stale-only legacy tree migrates ({status}: {detail})")
        new_tree = home / ".teammate-comms" / "TeammateComms"
        check((new_tree / "agents" / "Old.json").exists() and (new_tree / "inboxes" / "Old_unread.json").exists(),
              "AC-2: records and inboxes arrive under the new root")
        marker = legacy / "TeammateComms" / "MIGRATED.json"
        check(marker.exists() and json.loads(marker.read_text(encoding="utf-8")).get("moved_to") == str(home / ".teammate-comms"),
              "AC-2: MIGRATED.json names the new root")
        check(sorted(p.name for p in (legacy / "TeammateComms").iterdir()) == ["MIGRATED.json"],
              "AC-2: the legacy tree holds only the marker afterwards")
        root, source = comms_mod.resolve_comms_root(None)
        check(root == home / ".teammate-comms" and "default" in source, "AC-2: resolution follows the move")
        status, _ = comms_mod.migrate_legacy_root()
        check(status == "skipped", f"AC-2: second run is a no-op ({status})")
        _plant_agent(legacy, "ReviveOld", comms_mod.now_timestamp())
        status, detail = comms_mod.migrate_legacy_root()
        check(status == "repopulated" and "agents" in detail,
              f"tautology[AC-2]: records written beside the marker are reported, not hidden ({status}: {detail})")
        rep = tools_mod._doctor_report(home / ".teammate-comms", None)
        check("RE-POPULATED" in str(rep.get("legacy_root", {}).get(str(legacy), "")),
              f"AC-2: doctor shouts about the re-populated legacy tree ({rep.get('legacy_root')})")


def test_wp47_ac3_migration_deferred_while_live():
    from teammate_comms import comms as comms_mod
    from teammate_comms.comms import now_timestamp
    with fake_home() as home:
        legacy = home / ".claude"
        _plant_agent(legacy, "Live", now_timestamp())
        status, detail = comms_mod.migrate_legacy_root()
        check(status == "deferred" and "Live" in detail,
              f"tautology[AC-3]: a fresh heartbeat under the legacy root defers the move ({status}: {detail})")
        check((legacy / "TeammateComms" / "agents" / "Live.json").exists()
              and not (home / ".teammate-comms" / "TeammateComms").exists(),
              "AC-3: nothing moved while deferred")
        root, source = comms_mod.resolve_comms_root(None)
        check(root == legacy and "deferred" in source, "AC-3: servers keep using the legacy root meanwhile")
        check(not (legacy / "TeammateComms.migrate.lock").exists(), "AC-3: migration lock released")
    with fake_home() as home:
        _plant_agent(home / ".claude", "Garbled", "not-a-timestamp")
        status, detail = comms_mod.migrate_legacy_root()
        check(status == "deferred" and "Garbled" in detail,
              f"tautology[AC-3]: an unparsable heartbeat blocks the move (fail closed) ({status})")
    with fake_home() as home:
        _plant_agent(home / ".claude", "NoBeat", None)
        status, _ = comms_mod.migrate_legacy_root()
        check(status == "migrated", f"AC-3: a record with no heartbeat never blocks ({status})")


def test_wp47_ac4_migration_edge_cases():
    from teammate_comms import comms as comms_mod
    with fake_home() as home:
        legacy = home / ".claude"
        _plant_agent(legacy, "Old", STALE)
        (home / ".teammate-comms" / "TeammateComms").mkdir(parents=True)
        status, _ = comms_mod.migrate_legacy_root()
        check(status == "both-exist" and (legacy / "TeammateComms" / "agents" / "Old.json").exists(),
              f"AC-4: both trees present -> nothing moved ({status})")
        rep = tools_mod._doctor_report(home / ".teammate-comms", None)
        check("BOTH roots exist" in str(rep.get("legacy_root", {}).get(str(legacy), "")),
              f"AC-4: doctor distinguishes the permanent both-exist state ({rep.get('legacy_root')})")
    with fake_home() as home:
        legacy = home / ".claude"
        _plant_agent(legacy, "Old", STALE)
        real_replace, real_copytree = comms_mod.os.replace, comms_mod.shutil.copytree

        def cross_volume(src, dst, *args, **kw):
            if Path(src) == legacy / "TeammateComms":
                raise OSError("EXDEV")
            return real_replace(src, dst, *args, **kw)

        def lossy_copytree(src, dst, *args, **kw):
            result = real_copytree(src, dst, *args, **kw)
            if Path(src) == legacy / "TeammateComms":
                (Path(dst) / "agents" / "Old.json").unlink()
            return result
        comms_mod.os.replace, comms_mod.shutil.copytree = cross_volume, lossy_copytree
        try:
            status, detail = comms_mod.migrate_legacy_root()
        finally:
            comms_mod.os.replace, comms_mod.shutil.copytree = real_replace, real_copytree
        check(status == "failed" and "verification" in detail,
              f"tautology[AC-4]: a lossy copy is detected before the source is deleted ({status}: {detail})")
        check((legacy / "TeammateComms" / "agents" / "Old.json").exists()
              and not (home / ".teammate-comms" / "TeammateComms").exists(),
              "AC-4: legacy tree kept, partial copy removed")
        comms_mod.os.replace = cross_volume
        try:
            status, _ = comms_mod.migrate_legacy_root()
        finally:
            comms_mod.os.replace = real_replace
        check(status == "migrated" and (home / ".teammate-comms" / "TeammateComms" / "agents" / "Old.json").exists(),
              f"AC-4: a verified cross-volume copy migrates ({status})")
    with fake_home() as home:
        _plant_agent(home / ".claude", "Old", STALE)
        with env_vars(TEAMMATE_COMMS_DIR=str(home / "iso")):
            status, _ = comms_mod.migrate_legacy_root()
            check(status == "skipped", f"AC-4: TEAMMATE_COMMS_DIR set -> migration skipped ({status})")
    with fake_home() as home:
        status, _ = comms_mod.migrate_legacy_root()
        check(status == "skipped", f"AC-4: nothing pending -> skipped ({status})")


def test_wp47_ac5_doctor_reports_legacy_state():
    from teammate_comms import comms as comms_mod
    with fake_home() as home:
        legacy = home / ".claude"
        _plant_agent(legacy, "Old", STALE)
        rep = tools_mod._doctor_report(legacy, None)
        check(str(rep.get("legacy_root", {}).get(str(legacy), "")).startswith("in use (this root)"),
              f"AC-5: doctor reports the legacy root in use ({rep.get('legacy_root')})")
        comms_mod.migrate_legacy_root()
        rep = tools_mod._doctor_report(home / ".teammate-comms", None)
        check(str(rep.get("legacy_root", {}).get(str(legacy), "")).startswith("migrated to"),
              f"AC-5: doctor reports the migration ({rep.get('legacy_root')})")


RENDER_FORBIDDEN = ("Claude Code", "Codex", "/teammate-comms", "$teammate-comms", "SendMessage",
                    "spawn_agent", "Agent tool", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT",
                    "/plugin update", "codex plugin")


def _render_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("render_harness", REPO / "tools" / "render_harness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_wp48_ac1_render_drift_and_tokens():
    import re
    rh = _render_module()
    problems = rh.render_all(check=True)
    check(not problems, f"AC-1: rendered files match their sources ({problems})")
    targets = rh.targets()
    check(len(targets) == 2, f"AC-1: one skill renders to two harness files ({len(targets)})")
    claude = harness_mod.HARNESSES[harness_mod.CLAUDE_CODE]
    codex = harness_mod.HARNESSES[harness_mod.CODEX]
    src = rh.SKILLS_SRC / "teammate-comms" / "SKILL.md"
    text = src.read_text(encoding="utf-8")
    block = re.compile(r"\{\{harness:([a-z-]+)\}\}.*?\{\{/harness\}\}", re.DOTALL)
    check(bool(block.search(text)), "AC-1: the source uses at least one harness block")
    out_c, out_x = rh.render_text(text, claude), rh.render_text(text, codex)
    check(out_c != out_x, "tautology[AC-1]: harness blocks change the output")
    check("{{" not in out_c and "{{" not in out_x, "AC-1: no leftover tokens")
    check("`$teammate-comms`" in out_x and "`/teammate-comms`" in out_c, "AC-1: invoke token renders per harness")
    stripped = block.sub("", text)
    for lit in RENDER_FORBIDDEN:
        check(lit not in stripped, f"AC-1: harness literal {lit!r} outside a harness block in the source")
    check(not re.search(r"haiku|sonnet", stripped, re.IGNORECASE), "AC-1: no model names in the source")
    desc = out_x.split("---")[1] if out_x.startswith("---") else ""
    check(0 < len(desc) < 1024, "AC-1: codex description within the skill-list budget")
    for name in ("{{bogus}}", "{{harness:codex}}{{harness:codex}}x{{/harness}}{{/harness}}", "x{{/harness}}"):
        try:
            rh.render_text(name, codex)
            check(False, f"AC-1: renderer must reject {name!r}")
        except rh.RenderError:
            pass
    rendered = sorted(p.parent.name for p in (REPO / "skills").glob("*/SKILL.md"))
    sources = sorted(p.parent.name for p in rh.SKILLS_SRC.glob("*/SKILL.md"))
    check(rendered == sources, f"AC-1: no orphan rendered skill ({rendered} vs {sources})")
    codex_manifest = json.loads((REPO / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check(codex_manifest["skills"] == ["./adapters/codex/skills/teammate-comms"],
          "AC-1: codex manifest points at the codex render")


def main():
    sections = [
        ("WP-43 AC-1: harness table", test_wp43_ac1_harness_table),
        ("WP-43 AC-2: register stores/clears session", test_wp43_ac2_register_stores_and_clears_session),
        ("WP-43 AC-3: project_dir arg", test_wp43_ac3_project_dir_arg),
        ("WP-43 AC-4: generation semantics", test_wp43_ac4_generation_semantics),
        ("WP-43 AC-5: render harness", test_wp43_ac5_render),
        ("WP-43 AC-6: dispatch non-str args", test_wp43_ac6_dispatch_non_str),
        ("WP-44 AC-1: codex fresh wake argv", test_wp44_ac1_codex_fresh_wake_argv),
        ("WP-44 AC-2: dropped wake not retired", test_wp44_ac2_dropped_wake_not_retired),
        ("WP-44 AC-3: heartbeat not delayed", test_wp44_ac3_heartbeat_not_delayed),
        ("WP-44 AC-4: no session, no wake; re-resolve", test_wp44_ac4_no_session_no_wake),
        ("WP-44 AC-5: re-nudge gate + claude golden", test_wp44_ac5_renudge_gate_and_claude_golden),
        ("WP-44 AC-6: runner failure isolated", test_wp44_ac6_runner_failure_isolated),
        ("WP-44 AC-7: reaction drop not retired", test_wp44_ac7_reaction_drop_not_retired),
        ("WP-44 AC-8: codex exe resolution", test_wp44_ac8_codex_exe_resolution),
        ("WP-45 AC-1: manifest parity", test_wp45_ac1_manifest_parity),
        ("WP-45 AC-2: PARITY.md completeness", test_wp45_ac2_parity_completeness),
        ("WP-45 AC-3: SKILL harness lint", test_wp45_ac3_skill_lint),
        ("WP-45 AC-4: literal ratchet", test_wp45_ac4_literal_ratchet),
        ("WP-45 AC-5: codex hook shell", test_wp45_ac5_codex_hook_shell),
        ("WP-46 AC-1: codex builder argv", test_wp46_ac1_codex_builder_argv),
        ("WP-46 AC-2: which check names executable", test_wp46_ac2_which_check_names_executable),
        ("WP-46 AC-3: reincarnate codex prompt", test_wp46_ac3_reincarnate_codex_prompt),
        ("WP-47 AC-1: root resolution order", test_wp47_ac1_resolution_order),
        ("WP-47 AC-2: migration moves when idle", test_wp47_ac2_migration_moves_when_idle),
        ("WP-47 AC-3: migration deferred while live", test_wp47_ac3_migration_deferred_while_live),
        ("WP-47 AC-4: migration edge cases", test_wp47_ac4_migration_edge_cases),
        ("WP-47 AC-5: doctor legacy state", test_wp47_ac5_doctor_reports_legacy_state),
        ("WP-48 AC-1: renderer drift + tokens", test_wp48_ac1_render_drift_and_tokens),
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
        print("ok" if len(failures) == n_before else f"FAIL ({len(failures) - n_before} new)")
    print()
    if failures:
        print(f"FAIL ({len(failures)} total failure(s)):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
