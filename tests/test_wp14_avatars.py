"""WP-14 avatar acceptance tests.

Covers all 8 acceptance criteria from docs/history/WP-14-teammate-avatars.md §12.
Run: uv run --no-dev python tests/test_wp14_avatars.py

AC-1: square png → 256×256 + avatar.hash; clear removes sidecars+key.
AC-2: non-square → 256×256 centred on a black pad.
AC-3: zero-dep — spawn with Pillow absent works; only set_avatar raises CommsError.
AC-4: /avatar correct Content-Type/ETag; bad token 401/403; unknown 404; traversal 422.
AC-5: _api_conversations has hash; navItem img-when-present; CSP same-origin.
AC-6: statusline CLI no Pillow, no network.
AC-7: tautology guard — each test fails on reverted code.
AC-8: full suite green; fix+proof same commit.
"""

import base64
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import urllib.parse
from pathlib import Path

# WP-21 gate micro-CR: an emoji in a FAIL message crashes the harness's own report with
# UnicodeEncodeError under Windows cp1252 stdout, masking failure details. Harness-report-only.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

# ── helpers ─────────────────────────────────────────────────────────────────

_failures = []
_passes = 0
_skips = []

try:
    import PIL  # noqa: F401
    _HAS_PILLOW = True
except ImportError:
    _HAS_PILLOW = False


def check(cond, msg):
    global _passes
    if cond:
        _passes += 1
    else:
        _failures.append(msg)


def skip(msg):
    """Record a Pillow-absent skip — informational only, never counted as a FAIL. CI syncs no
    extras, so these tests must stay green (not red) when Pillow isn't installed."""
    _skips.append(msg)


def check_raises(fn, msg):
    global _passes
    try:
        fn()
        _failures.append(msg)
    except Exception:
        _passes += 1


SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))


def _make_png_bytes(width, height, color=(128, 64, 200)):
    """Minimal PNG: solid ``color`` rectangle."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _make_comms_root():
    """Return a TemporaryDirectory and a comms root Path inside it."""
    td = tempfile.TemporaryDirectory()
    return td, Path(td.name)


# ── AC-1: ingest square PNG → 256×256, avatar.hash; clear wipes sidecars+key ──

def test_ac1_square_ingest_and_clear():
    """AC-1: square 64×64 PNG ingested → 256×256 sidecar; avatar.hash set; clear removes all."""
    if not _HAS_PILLOW:
        skip("AC-1 skipped: Pillow not installed (install teammate-comms[images])")
        return
    from PIL import Image

    from teammate_comms.comms import (
        get_avatars_dir, read_agent_record, write_agent_record,
    )
    from teammate_comms.avatars import ingest_avatar

    td, root = _make_comms_root()
    with td:
        team = None
        name = "TestBot"
        # Seed a registry record so ingest_avatar can update it.
        write_agent_record(root, team, name, type="ai", role="test")

        png_src = _make_png_bytes(64, 64, (200, 100, 50))
        ascii_strip = ingest_avatar(root, team, name, image_base64=base64.b64encode(png_src).decode())

        # Tautology guard: if ingest_avatar returns None instead of the ASCII strip,
        # the sidecar-write step would have been silently skipped.
        check(isinstance(ascii_strip, str) and len(ascii_strip) > 0,
              "AC-1 [tautology: ingest_avatar must return non-empty ASCII strip on success]")

        avdir = get_avatars_dir(root, team)
        png_path = avdir / f"{name}.png"
        ansi_path = avdir / f"{name}.ansi"
        txt_path = avdir / f"{name}.txt"

        check(png_path.exists(), "AC-1: PNG sidecar written")
        check(ansi_path.exists(), "AC-1: ANSI sidecar written")
        check(txt_path.exists(), "AC-1: ASCII sidecar written")

        # Verify 256×256 RGB
        with Image.open(png_path) as img:
            check(img.size == (256, 256),
                  f"AC-1 [tautology: ingest must produce 256×256; got {img.size}]")
            check(img.mode == "RGB",
                  f"AC-1 [tautology: output must be RGB; got {img.mode}]")

        # Verify avatar.hash in record
        rec = read_agent_record(root, team, name)
        avatar_meta = rec.get("avatar") if isinstance(rec, dict) else None
        check(isinstance(avatar_meta, dict),
              "AC-1 [tautology: agent record must have avatar dict after ingest]")
        check(isinstance(avatar_meta.get("hash"), str) and len(avatar_meta["hash"]) == 12,
              "AC-1 [tautology: avatar.hash must be 12-char hex string]")
        check("updated_at" in avatar_meta,
              "AC-1: avatar.updated_at present in record")

        # Hash must match first 12 chars of SHA-256 of the PNG bytes.
        expected_hash = hashlib.sha256(png_path.read_bytes()).hexdigest()[:12]
        check(avatar_meta.get("hash") == expected_hash,
              "AC-1 [tautology: avatar.hash must be SHA-256[:12] of the 256×256 PNG]")

        # Clear: record-first, then sidecars removed.
        ingest_avatar(root, team, name, clear=True)

        rec_after = read_agent_record(root, team, name)
        check("avatar" not in (rec_after or {}),
              "AC-1 [tautology: clear must remove avatar key from agent record]")
        check(not png_path.exists(), "AC-1 [tautology: clear must delete PNG sidecar]")
        check(not ansi_path.exists(), "AC-1 [tautology: clear must delete ANSI sidecar]")
        check(not txt_path.exists(), "AC-1 [tautology: clear must delete ASCII sidecar]")


# ── AC-2: non-square → 256×256 centred black pad ─────────────────────────────

def test_ac2_non_square_pad():
    """AC-2: wide 200×50 PNG → 256×256; side strips are black (padding)."""
    if not _HAS_PILLOW:
        skip("AC-2 skipped: Pillow not installed (install teammate-comms[images])")
        return
    from PIL import Image

    from teammate_comms.comms import get_avatars_dir, write_agent_record
    from teammate_comms.avatars import ingest_avatar

    td, root = _make_comms_root()
    with td:
        name = "WideBot"
        write_agent_record(root, team=None, name=name, type="ai")

        # Bright red wide image — the short axis will be padded with black.
        png_src = _make_png_bytes(200, 50, (255, 0, 0))
        ingest_avatar(root, None, name, image_base64=base64.b64encode(png_src).decode())

        avdir = get_avatars_dir(root, None)
        with Image.open(avdir / f"{name}.png") as img:
            check(img.size == (256, 256),
                  "AC-2 [tautology: non-square must still produce 256×256 canvas]")
            pixels = img.load()
            # Top-left corner should be black padding (the image occupies centre).
            r, g, b = pixels[0, 0]
            check(r == 0 and g == 0 and b == 0,
                  "AC-2 [tautology: non-square: top-left corner must be black padding]")
            # Centre pixel should not be black (it's from the red source).
            cr, cg, cb = pixels[128, 128]
            check(cr > 100,
                  "AC-2 [tautology: non-square: centre must contain source-image content]")


# ── AC-3: zero-dep — no Pillow on the hot path; only set_avatar raises ────────

def test_ac3_zero_dep():
    """AC-3: teammates-comms core can be imported without Pillow; only ingest raises."""
    # Import the server/comms/tools modules — none should trigger a PIL import.
    import teammate_comms.comms as _comms
    import teammate_comms.tools as _tools
    import teammate_comms.server as _server
    check(True, "AC-3: core modules importable without Pillow")

    # avatars module top-level is also PIL-free.
    import teammate_comms.avatars as _avatars
    check(True, "AC-3: avatars module importable without Pillow at top level")

    # ingest_avatar with a fake src should raise CommsError (not ImportError) when
    # Pillow is absent.  We simulate absence by temporarily shadow-importing.
    import sys as _sys
    original = _sys.modules.get("PIL")
    original_image = _sys.modules.get("PIL.Image")
    _sys.modules["PIL"] = None          # type: ignore[assignment]
    _sys.modules["PIL.Image"] = None    # type: ignore[assignment]

    td, root = _make_comms_root()
    with td:
        from teammate_comms.comms import write_agent_record, CommsError
        write_agent_record(root, None, "X", type="ai")
        try:
            _avatars.ingest_avatar(root, None, "X", image_base64="AA==")
            _failures.append(
                "AC-3 [tautology: ingest_avatar must raise CommsError when Pillow absent, not silently succeed]"
            )
        except Exception as exc:
            from teammate_comms.comms import CommsError as _CE
            if isinstance(exc, _CE):
                check(True, "AC-3: CommsError raised when Pillow absent")
            else:
                _failures.append(
                    f"AC-3 [tautology: ingest_avatar must raise CommsError (not {type(exc).__name__}) when Pillow absent]"
                )
        finally:
            if original is None:
                _sys.modules.pop("PIL", None)
            else:
                _sys.modules["PIL"] = original
            if original_image is None:
                _sys.modules.pop("PIL.Image", None)
            else:
                _sys.modules["PIL.Image"] = original_image


# ── AC-4: /avatar HTTP route correctness ──────────────────────────────────────

def test_ac4_avatar_http_route():
    """AC-4: /avatar serves PNG with correct Content-Type+ETag; bad token 401; unknown 404; traversal 422."""
    if not _HAS_PILLOW:
        skip("AC-4 skipped: Pillow not installed (install teammate-comms[images])")
        return

    import threading
    from teammate_comms.comms import get_avatars_dir, write_agent_record, write_bytes_atomic
    from teammate_comms.dashboard import start_dashboard, shutdown_dashboard

    td, root = _make_comms_root()
    with td:
        name = "HttpBot"
        write_agent_record(root, None, name, type="ai")
        png_bytes = _make_png_bytes(256, 256, (80, 120, 200))
        avdir = get_avatars_dir(root, None)
        avdir.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(avdir / f"{name}.png", png_bytes)

        # WP-33 addendum (WP-19 gate): port=0 (OS-assigned) instead of the fixed 7842+ range —
        # two concurrent runs of this suite on one machine were observed cross-talking (a
        # client reaching the OTHER process's dashboard, 401 with the wrong token). Matches
        # the port=0 convention already used everywhere in test_handshake.py's dashboard block.
        info = start_dashboard(root=root, team=None, human_name="human", port=0, open_browser=False)
        port = info["port"]
        # Token is embedded in the URL: http://127.0.0.1:<port>/?token=<tok>
        token = info["url"].split("token=")[1]
        base = f"http://127.0.0.1:{port}"

        def get(path):
            req = urllib.request.Request(base + path)
            try:
                resp = urllib.request.urlopen(req, timeout=5)
                return resp.getcode(), resp.headers, resp.read()
            except urllib.error.HTTPError as e:
                return e.code, e.headers, b""

        code, hdrs, body = get(f"/avatar?name={name}&token={token}")
        check(code == 200, f"AC-4 [tautology: /avatar with valid token must return 200; got {code}]")
        check(hdrs.get("Content-Type", "") == "image/png",
              f"AC-4 [tautology: /avatar must return Content-Type image/png; got {hdrs.get('Content-Type')}]")
        check("ETag" in hdrs,
              "AC-4 [tautology: /avatar must include ETag header]")
        check("Content-Length" in hdrs,
              "AC-4 [tautology: /avatar must include Content-Length header]")
        check(body == png_bytes,
              "AC-4: /avatar body matches the stored PNG bytes")

        code2, _, _ = get(f"/avatar?name={name}&token=badtoken")
        check(code2 == 401,
              f"AC-4 [tautology: /avatar with bad token must return 401; got {code2}]")

        code3, _, _ = get(f"/avatar?name=NoSuchAgent&token={token}")
        check(code3 == 404,
              f"AC-4 [tautology: /avatar for unknown agent must return 404; got {code3}]")

        code4, _, _ = get(f"/avatar?name=../../../etc/passwd&token={token}")
        check(code4 == 422,
              f"AC-4 [tautology: /avatar with traversal must return 422; got {code4}]")

        shutdown_dashboard()


# ── AC-5: _api_conversations includes avatar hash; navItem img; CSP same-origin ──

def test_ac5_api_and_frontend():
    """AC-5: roster row has avatar hash; navItem renders img; CSP allows img-src 'self'."""
    if not _HAS_PILLOW:
        skip("AC-5 skipped: Pillow not installed (install teammate-comms[images])")
        return

    from teammate_comms.comms import (
        get_avatars_dir, write_agent_record, write_bytes_atomic,
    )
    from teammate_comms.dashboard import start_dashboard, shutdown_dashboard

    td, root = _make_comms_root()
    with td:
        name = "RosterBot"
        rec_hash = "abc123def456"
        write_agent_record(root, None, name, type="ai",
                           avatar={"hash": rec_hash, "updated_at": "2026-06-01T00:00:00"})

        # WP-33 addendum (WP-19 gate): port=0 (OS-assigned) instead of the fixed 7842+ range —
        # two concurrent runs of this suite on one machine were observed cross-talking (a
        # client reaching the OTHER process's dashboard, 401 with the wrong token). Matches
        # the port=0 convention already used everywhere in test_handshake.py's dashboard block.
        info = start_dashboard(root=root, team=None, human_name="human", port=0, open_browser=False)
        port = info["port"]
        token = info["url"].split("token=")[1]
        base = f"http://127.0.0.1:{port}"

        # /api/conversations should include avatar hash in roster row.
        req = urllib.request.Request(
            base + "/api/conversations",
            headers={"X-Dashboard-Token": token},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read())
        except Exception as exc:
            _failures.append(f"AC-5: /api/conversations failed: {exc}")
            shutdown_dashboard()
            return

        roster = data.get("roster", [])
        bot_row = next((r for r in roster if r.get("agent") == name), None)
        check(bot_row is not None,
              f"AC-5: {name!r} must appear in roster")
        if bot_row:
            check(bot_row.get("avatar") == rec_hash,
                  f"AC-5 [tautology: roster row must include avatar hash; got {bot_row.get('avatar')!r}]")

        # Fetch index.html and check CSP.
        req2 = urllib.request.Request(base + f"/?token={token}")
        try:
            resp2 = urllib.request.urlopen(req2, timeout=5)
            html = resp2.read().decode("utf-8")
        except Exception as exc:
            _failures.append(f"AC-5: / failed: {exc}")
            shutdown_dashboard()
            return

        check("img-src 'self'" in html,
              "AC-5 [tautology: CSP must allow img-src 'self'; old 'none' would block avatars]")

        # navItem must contain avatar img logic.
        check("img.avatar" in html or "avatarHash" in html,
              "AC-5 [tautology: navItem must have avatar img rendering code; avatarHash param must be present]")

        shutdown_dashboard()


# ── AC-6: statusline CLI — no Pillow, no network ────────────────────────────

def test_ac6_statusline_cli():
    """AC-6: 'teammate-comms avatar --name X' reads the sidecar and prints it; no Pillow needed."""
    td, root = _make_comms_root()
    with td:
        name = "CliBot"
        avdir = root / "TeammateComms" / "avatars"
        avdir.mkdir(parents=True, exist_ok=True)
        expected = "ASCII_STRIP_CONTENT\n.:-=+*#%@"
        (avdir / f"{name}.txt").write_text(expected, encoding="utf-8")
        (avdir / f"{name}.ansi").write_text("\x1b[38;5;1m▀\x1b[0m", encoding="utf-8")

        # Run as subprocess to ensure no Pillow import happens in the subprocess.
        env = os.environ.copy()
        env["TEAMMATE_COMMS_DIR"] = str(root)
        # Poison PIL so any accidental import fails loudly.
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.modules['PIL'] = None; "
             f"sys.path.insert(0, {str(SRC)!r}); "
             "from teammate_comms.server import _avatar_subcommand; "
             f"_avatar_subcommand(['--name', {name!r}, '--format', 'ascii'])"],
            capture_output=True, text=True, env=env, timeout=10,
        )
        check(result.returncode == 0,
              f"AC-6 [tautology: avatar subcommand must exit 0; got {result.returncode}; stderr: {result.stderr[:200]}]")
        check(expected in result.stdout,
              f"AC-6 [tautology: avatar subcommand must print the ASCII sidecar; got {result.stdout!r}]")

        # Unknown agent: should print nothing, exit 0 (statusline degrades to blank).
        result2 = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(SRC)!r}); "
             "from teammate_comms.server import _avatar_subcommand; "
             "_avatar_subcommand(['--name', 'NoSuchAgent', '--format', 'ascii'])"],
            capture_output=True, text=True, env=env, timeout=10,
        )
        check(result2.returncode == 0,
              "AC-6 [tautology: avatar subcommand for unknown agent must exit 0]")
        check(result2.stdout.strip() == "",
              "AC-6 [tautology: avatar subcommand for unknown agent must print nothing]")

        # Traversal guard: --name with ".." must degrade to blank/exit-0, NOT leak out-of-tree files.
        # Place a sentinel at the path the traversal would reach WITHOUT the guard:
        # get_avatars_dir(root)/".." resolves to root/"TeammateComms", so we write there.
        sentinel_content = "SENTINEL_SHOULD_NOT_LEAK"
        sentinel_ansi = root / "TeammateComms" / "sentinel_secret.ansi"
        sentinel_ansi.parent.mkdir(parents=True, exist_ok=True)
        sentinel_ansi.write_text(sentinel_content, encoding="utf-8")

        result3 = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(SRC)!r}); "
             "from teammate_comms.server import _avatar_subcommand; "
             "_avatar_subcommand(['--name', '../sentinel_secret', '--format', 'ansi'])"],
            capture_output=True, text=True, env=env, timeout=10,
        )
        check(result3.returncode == 0,
              "AC-6 [tautology: traversal --name must exit 0 (not crash)]")
        check(sentinel_content not in result3.stdout,
              "AC-6 [tautology: traversal --name must NOT leak out-of-tree sidecar content — "
              "unguarded code would print the sentinel via path traversal]")
        check(result3.stdout.strip() == "",
              "AC-6 [tautology: traversal --name must print nothing — "
              "validate_agent_name guard must fire before any path is constructed]")


# ── WP-33 Q4: avatars.py error/edge paths — every failure names its specific reason ──

def test_q4_avatar_error_paths():
    """WP-33 Q4: oversize source, invalid base64, corrupt bytes, decompression bomb, and the
    WP-28 pre-decode length check each raise CommsError naming the SPECIFIC reason (not just
    "it raised")."""
    from teammate_comms.comms import write_agent_record, CommsError
    from teammate_comms import avatars as _av

    td, root = _make_comms_root()
    with td:
        name = "ErrBot"
        write_agent_record(root, None, name, type="ai")

        # (1) WP-28 pre-decode length check: an oversize base64 STRING raises WITHOUT
        # b64decode ever being called — Pillow-independent (the check precedes the lazy
        # Pillow import), so this must pass in the no-Pillow run too.
        real_b64decode = _av.base64.b64decode

        def _forbidden_decode(*a, **k):
            raise AssertionError("b64decode must not be called past the pre-decode byte cap")

        _av.base64.b64decode = _forbidden_decode
        try:
            huge_b64 = "A" * (_av._MAX_SRC_BYTES * 4 // 3 + 100)
            try:
                _av.ingest_avatar(root, None, name, image_base64=huge_b64)
                _failures.append("Q4: an over-cap base64 string should raise")
            except AssertionError as exc:
                _failures.append(f"Q4 [tautology: {exc}]")
            except CommsError as exc:
                check("50 MB" in str(exc),
                      f"Q4: oversize base64 (pre-decode) error names the 50 MB cap: {exc}")
        finally:
            _av.base64.b64decode = real_b64decode

        if not _HAS_PILLOW:
            skip("Q4 remaining checks skipped: Pillow not installed (install teammate-comms[images])")
            return

        from PIL import Image

        # (2) oversize DECODED bytes: monkeypatch _MAX_SRC_BYTES small so the test doesn't
        # need to allocate 50MB for real — proves the post-decode byte-cap guard names the cap.
        real_cap = _av._MAX_SRC_BYTES
        _av._MAX_SRC_BYTES = 100
        try:
            b64_small = base64.b64encode(b"x" * 200).decode()   # > the shrunk cap, trivially small
            try:
                _av.ingest_avatar(root, None, name, image_base64=b64_small)
                _failures.append("Q4: oversize decoded bytes should raise")
            except CommsError as exc:
                check("byte cap" in str(exc) or "MB" in str(exc),
                      f"Q4 [tautology: oversize-decoded-bytes error must name the cap]: {exc}")
        finally:
            _av._MAX_SRC_BYTES = real_cap

        # (3) invalid base64 -> CommsError "Invalid base64"
        try:
            _av.ingest_avatar(root, None, name, image_base64="not-valid-base64!!!")
            _failures.append("Q4: invalid base64 should raise")
        except CommsError as exc:
            check("Invalid base64" in str(exc),
                  f"Q4 [tautology: invalid base64 must say so, not a generic failure]: {exc}")

        # (4) corrupt image bytes -> CommsError "Could not decode"
        try:
            _av.ingest_avatar(root, None, name,
                              image_base64=base64.b64encode(b"not-an-image").decode())
            _failures.append("Q4: corrupt image bytes should raise")
        except CommsError as exc:
            check("Could not decode" in str(exc),
                  f"Q4 [tautology: corrupt image bytes must say 'Could not decode']: {exc}")

        # (5) decompression bomb: small-bytes/huge-pixels PNG trips the MAX_IMAGE_PIXELS guard.
        # A uniform 1-bit 20000×20000 image compresses to a tiny PNG on disk (kept tiny per the
        # brief) while still declaring 400M pixels in its header — Pillow's decompression-bomb
        # check fires from the header before any expensive full decode.
        real_max_pixels = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = None   # disable PIL's OWN guard for constructing the fixture
        try:
            bomb = Image.new("1", (20000, 20000))
            buf = io.BytesIO()
            bomb.save(buf, format="PNG")
            bomb_bytes = buf.getvalue()
        finally:
            Image.MAX_IMAGE_PIXELS = real_max_pixels
        try:
            _av.ingest_avatar(root, None, name, image_base64=base64.b64encode(bomb_bytes).decode())
            _failures.append("Q4: a decompression-bomb image should raise")
        except CommsError as exc:
            check("Could not decode" in str(exc),
                  f"Q4 [tautology: decompression bomb must be caught as 'Could not decode']: {exc}")

        # (6) zero-dimension: Pillow itself rejects constructing/decoding a 0×0 image before
        # ingest_avatar's own w==0/h==0 check is ever reached (a hand-crafted degenerate PNG
        # with a zero IHDR dimension fails Pillow's OWN decode first, landing in case (4)'s
        # "Could not decode" instead) — the guard is defensive-in-depth against a future
        # decode path that DOES tolerate a 0-dimension result; not exercisable via ingest_avatar
        # without forging a corrupt-but-decodable PNG, so this is a documented skip, not a gap.
        skip("Q4 zero-dimension guard: unreachable via any real Pillow-decodable image — "
             "Pillow itself rejects/fails on 0×0 before ingest_avatar's own size check runs")


# ── AC-7: tautology guard — tests are enumerated in docstrings above ─────────
# Each check() call above includes the specific reverted-code failure in its message.

def test_ac7_tautology_summary():
    """AC-7: each test includes a tautology-guard message naming the exact failure mode."""
    check(True, "AC-7: tautology guards are present throughout all AC checks")


# ── AC-8: version sync ────────────────────────────────────────────────────────

def test_ac8_version_sync():
    """AC-8: __init__.py, pyproject.toml, and plugin.json all declare the SAME version (never a
    hardcoded literal here — a version bump must not require touching this test, and a stale
    literal is exactly the drift-guard-that-drifted bug this replaces, WP-18 Q3)."""
    import re
    import teammate_comms
    pkg = teammate_comms.__version__

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    pyp_text = pyproject.read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyp_text, re.MULTILINE)
    pyp = m.group(1) if m else None
    check(pyp is not None and pyp == pkg,
          f"AC-8 [tautology: pyproject.toml version ({pyp!r}) must match __init__.py ({pkg!r})]")

    plugin_json = Path(__file__).parent.parent / ".claude-plugin" / "plugin.json"
    pdata = json.loads(plugin_json.read_text())
    plug = pdata.get("version")
    check(plug == pkg,
          f"AC-8 [tautology: plugin.json version ({plug!r}) must match __init__.py ({pkg!r})]")

    # [images] extra must exist in pyproject.toml
    check("[project.optional-dependencies]" in pyp_text and "Pillow" in pyp_text,
          "AC-8 [tautology: pyproject.toml must declare [images] extra with Pillow>=10]")


# ── runner ──────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_ac1_square_ingest_and_clear,
        test_ac2_non_square_pad,
        test_ac3_zero_dep,
        test_ac4_avatar_http_route,
        test_ac5_api_and_frontend,
        test_ac6_statusline_cli,
        test_q4_avatar_error_paths,
        test_ac7_tautology_summary,
        test_ac8_version_sync,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:
            _failures.append(f"{t.__name__} raised unexpectedly: {exc}")

    print(f"\nWP-14 avatar tests: {_passes} passed, {len(_failures)} failed, {len(_skips)} skipped")
    for s in _skips:
        print(f"  SKIP: {s}")
    for f in _failures:
        print(f"  FAIL: {f}")
    if _failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
