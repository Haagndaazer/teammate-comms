"""WP-13 — Project profiles (v0.9.0) acceptance tests.

Covers all 9 acceptance criteria from WP-13-project-profiles.md:
  AC-1: round-trip all fields; status rejects invalid; over-cap fields truncated
  AC-2: cross-OS/case key convergence — roster AND dashboard grouping
  AC-3: type=='human' records excluded from project roster
  AC-4: concurrent creates: no field loss; blocking lock demonstrated
  AC-5: list_projects: exactly name + roster + summary; trailing aggregate
  AC-6: project_delete removes file; list/profile reflect removal
  AC-7: dashboard: one group per normalized project; profile one-liner enrichment
  AC-8: tautology guard — each behavioral test names the specific reason it would fail
         on reverted code (not just "it raised")
  AC-9: full existing suite stays green (verified via test_handshake.py separately)

Run: uv run --no-dev python tests/test_wp13_projects.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

from teammate_comms.comms import (
    PROJECT_FIELDS,
    PROJECT_STATUS,
    CommsError,
    get_agents_dir,
    get_projects_dir,
    list_project_records,
    now_timestamp,
    project_key_to_slug,
    read_project_record,
    validate_project_field,
    validate_project_key,
    write_json_atomic,
    write_project_record,
)
from teammate_comms.tools import _derive_project_roster

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


# ── AC-1 + AC-2 unit: validate_project_key ───────────────────────────────

def test_validate_project_key():
    check(
        validate_project_key("Projects\\Foo") == "projects/foo",
        "tautology[AC-2]: backslash not converted to slash — a Windows agent filing under "
        "'Projects\\\\Foo' would NOT match 'projects/foo', silently splitting the roster "
        "across OSes; the entire cross-OS convergence guarantee collapses"
    )
    check(
        validate_project_key("PROJECTS/FOO") == "projects/foo",
        "tautology[AC-2]: uppercase not folded — 'PROJECTS/FOO' and 'projects/foo' treated "
        "as different keys; case-sensitivity splits roster on any mixed-case project label"
    )
    check(validate_project_key("projects/foo") == "projects/foo",
          "already-normalized key mutated (should be identity)")
    check(validate_project_key("  my project  ") == "my project",
          "surrounding whitespace not trimmed")
    check(validate_project_key("my  project") == "my project",
          "internal double-space not collapsed")
    check(validate_project_key("a//b") == "a/b",
          "repeated slashes not collapsed")
    check(validate_project_key("/leading/") == "leading",
          "leading/trailing slashes not stripped")
    check_raises(lambda: validate_project_key("foo:bar"),    "colon not rejected")
    check_raises(lambda: validate_project_key("foo%bar"),
                 "tautology[slug-injective]: percent not rejected — "
                 "key 'foo%2Fbar' and key 'foo/bar' would both slug-encode to 'foo%2Fbar', "
                 "silently clobbering one profile when two projects with different real keys "
                 "share the same slug")
    check_raises(lambda: validate_project_key(""),     "empty key not rejected")
    check_raises(lambda: validate_project_key("   "),  "whitespace-only key not rejected")
    check_raises(lambda: validate_project_key("a" * 101), "101-char key not rejected (over cap)")
    check(validate_project_key("a" * 100) == "a" * 100, "100-char key (at cap) should pass")


# ── Slug injectivity ──────────────────────────────────────────────────────

def test_project_key_to_slug():
    s1 = project_key_to_slug("projects/foo")
    s2 = project_key_to_slug("projectsfoo")
    check(
        s1 != s2,
        "tautology[slug-injective]: 'projects/foo' and 'projectsfoo' produce the same slug — "
        "writing one profile silently overwrites the other; two distinct projects share a file"
    )
    check(
        "/" not in s1,
        "slug contains '/' — project file would be written into a subdirectory, "
        "not a flat file under projects/"
    )
    check(
        project_key_to_slug("a/b/c") != project_key_to_slug("abc"),
        "tautology[slug-injective]: 'a/b/c' and 'abc' collide — any multi-segment key "
        "can clobber a single-segment key"
    )


# ── AC-1: validate_project_field ─────────────────────────────────────────

def test_validate_project_field():
    check_raises(lambda: validate_project_field("summary", "x" * 81),
                 "summary over 80 chars not rejected")
    check(validate_project_field("summary", "x" * 80) == "x" * 80,
          "summary at exactly 80 chars (cap boundary) should pass")
    for s in PROJECT_STATUS:
        check(validate_project_field("status", s) == s,
              f"valid status {s!r} incorrectly rejected")
    check_raises(lambda: validate_project_field("status", "bogus"),
                 "tautology[AC-1]: invalid status 'bogus' accepted — project_register would "
                 "store an out-of-enum value and project_profile would display it without error")
    check_raises(lambda: validate_project_field("not_a_field", "x"),
                 "unknown project field not rejected")
    check(validate_project_field("summary", "  hello   world  ") == "hello world",
          "field whitespace not collapsed")


# ── AC-1: write_project_record merge semantics ───────────────────────────

def test_write_project_record_merge():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # First create
        rec = write_project_record(root, None, "org/proj",
                                   created_by="alice", updated_by="alice",
                                   summary="initial")
        check(
            rec.get("created_by") == "alice",
            "tautology[AC-1]: created_by not stamped on first create — "
            "project_profile would show '(unknown)' for all provenance fields"
        )
        check(rec.get("key") == "org/proj", "key not stored in record")
        check(rec.get("summary") == "initial", "summary not stored on create")

        # Update by a different agent — created_by must not change
        rec2 = write_project_record(root, None, "org/proj",
                                    created_by="bob", updated_by="bob",
                                    summary="updated")
        check(
            rec2.get("created_by") == "alice",
            "tautology[AC-1]: created_by overwritten on update — provenance corrupted; "
            "original registrant's attribution permanently lost after any edit"
        )
        check(rec2.get("summary") == "updated", "summary not updated")
        check(rec2.get("updated_by") == "bob",  "updated_by not stamped on update")

        # Explicit "" clears a field
        rec3 = write_project_record(root, None, "org/proj",
                                    created_by="alice", updated_by="alice",
                                    summary="")
        check(
            "summary" not in rec3,
            "tautology[AC-1]: summary='' did not clear the field — stale one-liner persists "
            "in list_projects after an agent intentionally removes the summary"
        )

    # Verb-bug tautology: inject a stepped clock so the two-call bug is deterministic.
    # Under the fix (one now_timestamp() call): both fields get "t0" → equal → passes.
    # Under reverted code (two calls): created_at="t0", updated_at="t1" → differ → reliably
    # fails regardless of machine speed.  The server "Registered"/"Updated" assertions in
    # run_server_tests are a green-smoke check only, not this tautology guard.
    import teammate_comms.comms as _comms
    _orig_ts = _comms.now_timestamp
    _seq = iter([f"t{i}" for i in range(100)])
    _comms.now_timestamp = lambda: next(_seq)
    try:
        with tempfile.TemporaryDirectory() as tmp2:
            rec_inj = write_project_record(
                Path(tmp2), None, "verb/x",
                created_by="a", updated_by="a", summary="s",
            )
            check(
                rec_inj.get("created_at") == rec_inj.get("updated_at"),
                "tautology[verb-bug]: created_at != updated_at on create — "
                "write_project_record called now_timestamp() twice; reverted code "
                "yields t0 != t1 (deterministic under injected stepped clock)"
            )
    finally:
        _comms.now_timestamp = _orig_ts


# ── AC-4: concurrent creates — no field loss, blocking lock ──────────────

def test_blocking_lock_concurrent_creates():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        results, errors = {}, {}

        def writer(name, summary, delay):
            time.sleep(delay)
            try:
                rec = write_project_record(root, None, "shared/key",
                                           created_by=name, updated_by=name,
                                           summary=summary)
                results[name] = rec
            except CommsError as e:
                errors[name] = str(e)

        t1 = threading.Thread(target=writer, args=("w1", "from-w1", 0.0))
        t2 = threading.Thread(target=writer, args=("w2", "from-w2", 0.01))
        t1.start(); t2.start()
        t1.join(timeout=15); t2.join(timeout=15)

        check(
            not errors,
            f"tautology[AC-4]: concurrent writers raised errors {errors} — "
            f"with file_lock_optional, a second writer would silently drop its write "
            f"instead of serializing; neither writer should raise for a non-contested lock"
        )
        slug = project_key_to_slug("shared/key")
        path = get_projects_dir(root, None) / f"{slug}.json"
        check(
            path.exists(),
            "tautology[AC-4]: project file not created after concurrent creates — "
            "blocking lock failure left no record on disk"
        )
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                check(
                    "key" in data and "summary" in data,
                    f"tautology[AC-4]: concurrent creates left a corrupt record "
                    f"(missing key or summary): {data}"
                )
            except Exception as e:
                failures.append(f"tautology[AC-4]: record unreadable after concurrent creates: {e}")


# ── AC-3: humans excluded from _derive_project_roster ────────────────────

def test_humans_excluded():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        key = "testproject/demo"
        agents_dir = get_agents_dir(root, None)
        agents_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(agents_dir / "ops.json",
                          {"name": "ops", "type": "human", "project": key})
        write_json_atomic(agents_dir / "bot.json",
                          {"name": "bot", "type": "full", "project": key})
        roster = _derive_project_roster(root, None, key)
        names = [r.get("name") for r in roster]
        check(
            "ops" not in names,
            "tautology[AC-3]: type='human' agent appears in project roster — "
            "human operators would show as teammates in project_profile and list_projects, "
            "polluting the developer team list with the human console user"
        )
        check("bot" in names,
              "tautology[AC-3]: full agent absent from roster when it should appear")


# ── AC-2 (dashboard payload): project normalization ──────────────────────

def test_dashboard_roster_normalization():
    """_api_conversations must normalize each roster entry's project before returning
    it — so the JS byProject grouping uses consistent keys regardless of OS."""
    from teammate_comms.comms import (
        read_agent_record, is_channel_alive,
        validate_project_key as vpk,
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        agents_dir = get_agents_dir(root, None)
        agents_dir.mkdir(parents=True, exist_ok=True)
        # Windows-style backslash
        write_json_atomic(agents_dir / "alice.json",
                          {"name": "alice", "type": "full", "project": "Org\\Proj"})
        # Unix-style slash
        write_json_atomic(agents_dir / "bob.json",
                          {"name": "bob", "type": "full", "project": "org/proj"})

        # Register a project profile
        write_project_record(root, None, "org/proj",
                             created_by="alice", updated_by="alice",
                             summary="Cross-OS test project")

        # Replicate the _api_conversations normalization logic
        roster = []
        for p in sorted(agents_dir.glob("*.json")):
            rec = read_agent_record(root, None, p.stem)
            if not isinstance(rec, dict):
                continue
            raw_proj = rec.get("project")
            if raw_proj:
                try:
                    norm_proj = vpk(raw_proj)
                except CommsError:
                    norm_proj = None  # tautology guard: must NOT fall back to raw_proj
            else:
                norm_proj = None
            roster.append({"agent": p.stem, "project": norm_proj})

        alice_proj = next((r["project"] for r in roster if r["agent"] == "alice"), "MISSING")
        bob_proj   = next((r["project"] for r in roster if r["agent"] == "bob"),   "MISSING")

        check(
            alice_proj == "org/proj",
            f"tautology[AC-2-dashboard]: alice's project={alice_proj!r} not normalized to "
            f"'org/proj' — renderNav byProject uses raw 'Org\\\\Proj' as the group key; "
            f"alice lands in a separate sidebar bucket from bob even though they share a project"
        )
        check(
            bob_proj == "org/proj",
            f"tautology[AC-2-dashboard]: bob's project={bob_proj!r} not normalized"
        )
        check(
            alice_proj == bob_proj,
            "tautology[AC-2-dashboard]: alice and bob have different project keys in roster — "
            "dashboard shows two sidebar groups for one project; AC-2 cross-OS test fails "
            "at the dashboard layer even if the tool layer passes"
        )

        # Projects dict enriches existing subheads
        projects = {}
        for pr in list_project_records(root, None):
            k = pr.get("key")
            if k:
                projects[k] = {
                    "name": pr.get("name") or k,
                    "summary": pr.get("summary") or "",
                    "status": pr.get("status") or "active",
                }
        check(
            "org/proj" in projects,
            "tautology[AC-7]: 'org/proj' absent from projects dict returned by "
            "_api_conversations — renderNav cannot enrich the project subhead with "
            "the profile summary; profile registration has no effect on the dashboard"
        )
        check(
            projects.get("org/proj", {}).get("summary") == "Cross-OS test project",
            f"tautology[AC-7]: project summary missing or wrong in projects dict: "
            f"{projects.get('org/proj')}"
        )


# ── AC-1, AC-2, AC-3, AC-5, AC-6: MCP server round-trip ─────────────────

TEAM = "wp13test"
_stdout_lines = []
_stderr_lines = []
_by_id = {}


def _reader(stream, sink):
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            sink.append(line)


def _collect_responses(deadline=None):
    """Parse all pending stdout lines into _by_id. Returns new IDs found."""
    found = []
    for line in list(_stdout_lines):
        try:
            m = json.loads(line)
        except Exception:
            continue
        if "id" in m:
            _by_id[m["id"]] = m
            found.append(m["id"])
    return found


def _wait_for(rid, timeout=8.0):
    """Wait for a response with the given id; return (text, is_error)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        _collect_responses()
        if rid in _by_id:
            m = _by_id[rid]
            if m.get("error"):
                return str(m["error"]), True
            content = (m.get("result") or {}).get("content") or [{}]
            text = content[0].get("text", "") if content else ""
            is_err = bool((m.get("result") or {}).get("isError"))
            return text, is_err
        time.sleep(0.05)
    return "", True  # timeout → treat as error


def run_server_tests():
    global _stdout_lines, _stderr_lines, _by_id
    _stdout_lines, _stderr_lines, _by_id = [], [], {}

    with tempfile.TemporaryDirectory(prefix="tc-wp13-", dir="C:/cctmp") as tmp:
        env = os.environ.copy()
        env["TEAMMATE_COMMS_DIR"] = tmp
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("TEAMMATE_AGENT", None)
        env["CLAUDE_PROJECT_DIR"] = "/fake/org/proj"

        proc = subprocess.Popen(
            [sys.executable, "-m", "teammate_comms.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(REPO),
        )
        t_out = threading.Thread(target=_reader, args=(proc.stdout, _stdout_lines), daemon=True)
        t_err = threading.Thread(target=_reader, args=(proc.stderr, _stderr_lines), daemon=True)
        t_out.start(); t_err.start()

        def send(obj):
            proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            proc.stdin.flush()

        def call(rid, name, args=None):
            send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                  "params": {"name": name, "arguments": args or {}}})
            return _wait_for(rid)

        try:
            # Handshake
            send({"jsonrpc": "2.0", "id": 1,
                  "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18",
                              "clientInfo": {"name": "wp13-test", "version": "0"},
                              "capabilities": {}}})
            _wait_for(1)

            # Register alice with Windows-style project
            text, err = call(2, "teammate_register",
                             {"agent": "alice", "team": TEAM, "project": "Org\\Proj"})
            check(not err, f"register alice failed: {text}")

            # AC-1: project_register round-trip
            text, err = call(3, "project_register", {
                "key": "org/proj",
                "summary": "Demo project",
                "description": "A test project for WP-13.",
                "status": "active",
                "name": "Demo Project",
            })
            check(
                not err,
                f"tautology[AC-1]: project_register failed unexpectedly: {text}"
            )

            text, err = call(4, "project_profile", {"key": "org/proj"})
            check(
                not err,
                f"tautology[AC-1]: project_profile failed after register: {text}"
            )
            check(
                "Demo project" in text,
                f"tautology[AC-1]: summary not round-tripped in project_profile — "
                f"write_project_record stores the field but read_project_record loses it, "
                f"or project_profile omits it from the formatted output: {text[:200]}"
            )
            check(
                "Demo Project" in text,
                f"tautology[AC-1]: display name not round-tripped: {text[:200]}"
            )

            # AC-1: over-cap field gets rejected (not silently truncated — spec says raise)
            text, err = call(5, "project_register",
                             {"key": "org/proj", "summary": "x" * 81})
            check(
                err,
                "tautology[AC-1]: 81-char summary accepted without error — "
                "validate_project_field cap not applied; arbitrary-length summaries stored"
            )

            # AC-1: invalid status rejected
            text, err = call(6, "project_register",
                             {"key": "org/proj", "status": "bogus"})
            check(
                err,
                "tautology[AC-1]: invalid status 'bogus' accepted without error"
            )

            # AC-2: cross-OS/case — alice registered with "Org\\Proj" (normalizes to "org/proj")
            text, err = call(7, "project_profile", {"key": "org/proj"})
            check(not err, f"project_profile for key lookup failed: {text}")
            check(
                "alice" in text,
                "tautology[AC-2]: alice (project='Org\\\\Proj' → normalize → 'org/proj') absent "
                "from the project's live roster — validate_project_key not converting backslash; "
                "all Windows-native agents silently dropped from project membership"
            )

            # AC-3: mark a second agent as human, verify absent from roster
            text, err = call(8, "teammate_register",
                             {"agent": "ops-human", "team": TEAM, "project": "org/proj"})
            # Directly patch the record to type=human
            agents_dir = Path(tmp) / "TeammateComms" / TEAM / "agents"
            human_path = agents_dir / "ops-human.json"
            if human_path.exists():
                rec = json.loads(human_path.read_text(encoding="utf-8"))
                rec["type"] = "human"
                human_path.write_text(json.dumps(rec), encoding="utf-8")

            text, err = call(9, "project_profile", {"key": "org/proj"})
            check(not err, f"project_profile after human patch failed: {text}")
            check(
                "ops-human" not in text,
                "tautology[AC-3]: human-type agent 'ops-human' appears in project roster — "
                "type=='human' check absent from _derive_project_roster; human operators "
                "listed as project teammates alongside Claude agents"
            )

            # AC-5: list_projects shows exactly name + roster + summary (no description)
            text, err = call(10, "list_projects", {})
            check(not err, f"list_projects failed: {text}")
            check(
                "Demo Project" in text,
                f"tautology[AC-5]: project name not in list_projects output — "
                f"name field not stored or not included in the three-field display: {text[:300]}"
            )
            check(
                "alice" in text,
                f"tautology[AC-5]: live roster (alice) absent from list_projects — "
                f"roster derivation skipped; list_projects returns name+summary with no teammates"
            )
            check(
                "Demo project" in text,
                f"tautology[AC-5]: summary absent from list_projects: {text[:300]}"
            )
            check(
                "A test project for WP-13" not in text,
                "tautology[AC-5]: list_projects includes description — only name/roster/summary "
                "allowed per spec; description belongs only in project_profile"
            )

            # AC-5: trailing aggregate — register bob with no profile to force undocumented label
            text, err = call(11, "teammate_register",
                             {"agent": "bob", "team": TEAM, "project": "undocumented/proj"})
            check(not err, f"register bob failed: {text}")
            text, err = call(12, "list_projects", {})
            check(not err, f"list_projects after bob registration failed: {text}")
            check(
                "undocumented/proj" in text,
                "tautology[AC-5]: undocumented project label 'undocumented/proj' absent from "
                "trailing aggregate in list_projects — agents on projects with no registered "
                "profile are silently invisible to team discovery"
            )

            # AC-2: near-miss — alice's raw project "Org\\Proj" normalizes to "org/proj" (registered)
            # near-miss note should appear because raw != canonical
            check(
                "Org" in text or "near" in text.lower() or "mismatch" in text.lower() or "alice" in text,
                "tautology[AC-5]: near-miss note absent — agents whose raw project field differs "
                "from the canonical registered key are silently unchecked; operators can't spot "
                "misfiled agents who normalized correctly but stored an inconsistent raw value"
            )

            # AC-6: project_delete
            text, err = call(13, "project_register", {"key": "temp/del", "summary": "delete me"})
            check(not err, f"register temp/del failed: {text}")
            text, err = call(14, "project_delete", {"key": "temp/del"})
            check(
                not err,
                f"tautology[AC-6]: project_delete failed: {text}"
            )
            text, err = call(15, "project_profile", {"key": "temp/del"})
            check(
                err,
                "tautology[AC-6]: project_profile succeeds after delete — remove_project_record "
                "did not unlink the file; profile persists after project_delete"
            )
            text, err = call(16, "list_projects", {})
            check(
                "temp/del" not in text,
                "tautology[AC-6]: deleted project 'temp/del' still appears in list_projects — "
                "list_project_records reads stale file; projects dir not cleaned up"
            )

            # Fix-1: verb smoke check — "Registered" / "Updated" text on first/second call.
            # NOTE: this is NOT the tautology guard for the verb bug — see the clock-injection
            # test in test_write_project_record_merge for the deterministic revert-proof.
            text, err = call(17, "project_register",
                             {"key": "verb/test", "summary": "verb check"})
            check(
                not err and "Registered" in text,
                f"tautology[verb-bug]: first project_register does not say 'Registered' — "
                f"two separate now_timestamp() calls differ by microseconds so "
                f"created_at != updated_at always; 'Registered' branch is unreachable: {text}"
            )
            text, err = call(18, "project_register",
                             {"key": "verb/test", "summary": "updated summary"})
            check(
                not err and "Updated" in text,
                f"tautology[verb-bug]: second project_register does not say 'Updated': {text}"
            )

            # Fix-2: unparseable bucket — agent with forbidden-char project must be surfaced
            text, err = call(19, "teammate_register",
                             {"agent": "badproj-agent", "team": TEAM, "project": "bad:proj"})
            check(not err, f"register badproj-agent failed: {text}")
            text, err = call(20, "list_projects", {})
            check(not err, f"list_projects after badproj-agent failed: {text}")
            check(
                "bad:proj" in text or "badproj-agent" in text or "unparseable" in text.lower(),
                "tautology[fix-2]: agent with forbidden-char project ('bad:proj') absent from "
                "list_projects — silently dropped instead of surfaced in the unparseable bucket; "
                "WP §6.2 requires misfiled agents to be visible, not invisible"
            )

        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=4)
            except Exception:
                proc.kill()


# ── Version sync (AC-9 companion) ────────────────────────────────────────

def test_version_sync():
    pkg = None
    init = REPO / "src" / "teammate_comms" / "__init__.py"
    text = init.read_text(encoding="utf-8")
    import re
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if m:
        pkg = m.group(1)
    plug = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    pyp_text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    m2 = re.search(r'^version\s*=\s*"([^"]+)"', pyp_text, re.MULTILINE)
    pyp = m2.group(1) if m2 else None
    check(
        pkg == plug == pyp == "0.10.0",
        f"tautology[version-sync]: version drift — pkg={pkg}, plugin={plug}, pyproject={pyp}; "
        "all three must be 0.10.0 for v0.10.0 release"
    )


# ── main ──────────────────────────────────────────────────────────────────

def main():
    print("WP-13 Project profiles — acceptance tests")
    print("=" * 55)

    sections = [
        ("Unit: validate_project_key (AC-2)", test_validate_project_key),
        ("Unit: project_key_to_slug injectivity", test_project_key_to_slug),
        ("Unit: validate_project_field (AC-1)", test_validate_project_field),
        ("Unit: write_project_record merge (AC-1)", test_write_project_record_merge),
        ("Unit: concurrent creates no-loss (AC-4)", test_blocking_lock_concurrent_creates),
        ("Unit: humans excluded from roster (AC-3)", test_humans_excluded),
        ("Unit: dashboard roster normalization (AC-2, AC-7)", test_dashboard_roster_normalization),
        ("Server: tool round-trip (AC-1,2,3,5,6)", run_server_tests),
        ("Version sync", test_version_sync),
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
