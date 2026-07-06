"""Launch a new Claude Code teammate in a terminal window (for ``teammate_reincarnate``).

Pure command/env builders (unit-tested without spawning) + a cross-platform detached
terminal launcher. Design rules:

- **List-form exec only.** The argv is built as a LIST and passed to Popen as a list —
  never concatenated into a shell string — so the ``prompt`` (the only free-text input)
  is a single argv element and cannot inject shell metacharacters. No string escaper is
  needed because no launcher path builds a shell string.
- **Child stdio = DEVNULL.** A launcher writing to the parent's stdout would corrupt the
  MCP server's JSON-RPC stream; the child never inherits stdout/stderr/stdin.
- **Detached / survives parent exit.** Windows: ``wt.exe`` (its own top-level process) or
  a new console; POSIX: ``start_new_session``.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .comms import CommsError

# Lazily-available on Windows only; imported at module level (not inside the function) so a
# test can monkeypatch `spawn.winreg` with a fake registry (W2 AC-1's injection seam).
try:
    import winreg
except ImportError:
    winreg = None

# The custom teammate-comms channel is not on Anthropic's built-in allowlist, so by default
# it must be loaded with --dangerously-load-development-channels (which triggers a one-time
# trust prompt the spawned, DEVNULL-stdio child can't answer). BUT if the operator has placed
# a managed-settings file allowlisting this plugin, the channel is trusted and loads with the
# plain --channels flag and NO prompt — so we auto-detect that and prefer it (see
# channel_allowlisted). $TEAMMATE_LAUNCH_ARGS overrides both, verbatim.
PLUGIN_NAME = "teammate-comms"
# Fallback marketplace name when nothing else resolves it (W1) — the ORIGINAL marketplace this
# plugin shipped from, kept only as a last resort so an unconfigured fork still gets a plausible
# (if wrong) spec rather than an exception.
_FALLBACK_MARKETPLACE = "coltondyck"


def resolve_marketplace():
    """Resolve the plugin marketplace name for THIS install (W1), in order:
    1. ``$TEAMMATE_PLUGIN_MARKETPLACE`` — explicit override, always wins.
    2. Best-effort derivation from ``$CLAUDE_PLUGIN_ROOT`` (the plugin cache path conventionally
       contains a ``marketplaces/<name>/...`` segment). Parsed DEFENSIVELY: any doubt falls
       through to (3) rather than guessing.
    3. The fallback marketplace this plugin originally shipped from.

    A hardcoded marketplace makes every spawn on a fork/rehost load a nonexistent plugin ref,
    and the DEVNULL child makes that failure indistinguishable from success — this makes the
    marketplace configurable and self-detecting instead.
    """
    override = os.environ.get("TEAMMATE_PLUGIN_MARKETPLACE")
    if override and override.strip():
        return override.strip()
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        try:
            parts = Path(plugin_root).parts
            idx = parts.index("marketplaces")
            candidate = parts[idx + 1]
            if candidate:
                return candidate
        except (ValueError, IndexError):
            pass  # doesn't match the known cache layout — fall through, never guess
    return _FALLBACK_MARKETPLACE


def plugin_spec():
    """The ``plugin:<name>@<marketplace>`` spec for THIS install (resolved at call time)."""
    return f"plugin:{PLUGIN_NAME}@{resolve_marketplace()}"


_GATE_ENV_VAR = "TEAMMATE_REINCARNATE_ENABLED"


def _gate_durably_set():
    """Detect whether the reincarnate gate is set DURABLY (registry/user env), not just for
    this process (W2 — incident c1fa517c047d: the gate leaked machine-wide via a `setx` demo,
    no code mitigation ever landed). A true session scope is impossible for an inherited env
    var; the implementable mitigation is DETECTION, not enforcement (chosen policy: warn, not
    refuse — a user who consciously chose durable enablement shouldn't be broken by it).

    Windows: read ``HKCU\\Environment`` (per-user) and the machine-wide Session Manager key via
    ``winreg``. Every call is guarded — a missing key, permission error, or winreg being
    unavailable all mean False, never a raise (this is a courtesy warning, not a security
    control). POSIX: always None (undetectable — grepping shell profiles is out of scope).
    """
    if os.name != "nt" or winreg is None:
        return None
    hives = [
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ]
    for hive, subkey in hives:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, _GATE_ENV_VAR)
                if value:
                    return True
        except OSError:
            continue
        except Exception:
            continue  # never let a registry quirk raise out of a courtesy check
    return False


def dangerous_launch_args():
    return f"claude --dangerously-load-development-channels {plugin_spec()}"


def allowlisted_launch_args():
    return f"claude --channels {plugin_spec()}"


def managed_settings_paths():
    """Candidate managed-settings.json paths Claude Code reads (highest-precedence), per OS.

    Returns a LIST (first match wins in channel_allowlisted). Windows lists both the
    Program Files location (verified working) and the conventionally-documented ProgramData
    one; macOS/Linux use the documented enterprise locations.
    """
    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pd = os.environ.get("ProgramData", r"C:\ProgramData")
        return [
            os.path.join(pf, "ClaudeCode", "managed-settings.json"),
            os.path.join(pd, "ClaudeCode", "managed-settings.json"),
        ]
    if sys.platform == "darwin":
        return ["/Library/Application Support/ClaudeCode/managed-settings.json"]
    return ["/etc/claude-code/managed-settings.json"]


# H2: the managed-settings.json SHAPE this module parses (channelsEnabled +
# allowedChannelPlugins[].{plugin,marketplace}) is a schema GUESS, verified against exactly
# ONE Claude Code build, with no recorded expiry — a future CC release could change the
# schema silently and this parser would just never match (fail-safe toward the dangerous
# flag, per channel_allowlisted's docstring, but still worth naming the assumption where it
# acts). If channel loading breaks after a CC upgrade, check this vintage first;
# TEAMMATE_LAUNCH_ARGS is the escape hatch that bypasses this detection entirely.
MANAGED_SETTINGS_VERIFIED = "Claude Code 2.1.161 / Windows"


def channel_allowlisted(plugin=PLUGIN_NAME, marketplace=None, settings_paths=None):
    """True iff a managed-settings file marks this channel trusted, so it loads with the
    plain --channels flag (no dangerous flag, no startup prompt).

    Trusted = ``channelsEnabled`` truthy AND an ``allowedChannelPlugins`` entry matching this
    plugin+marketplace. ``marketplace`` defaults to ``resolve_marketplace()`` (W1) — resolved
    at CALL time, not a stale module-level constant, so a fork's env override is honored. We
    REQUIRE ``channelsEnabled`` deliberately (do not relax it): if we guess wrong and fall back
    to the dangerous flag, the channel still loads (maybe a prompt); if we wrongly returned
    True, the child would launch with --channels but no dangerous flag and the channel might
    not load at all — the worse failure. So we fail toward the safe flag.

    Best-effort: a missing/unreadable/malformed file (incl. PermissionError reading a
    Program Files file as non-admin) is skipped → False. utf-8-sig tolerates a Notepad BOM.
    The parsed schema is verified only against ``MANAGED_SETTINGS_VERIFIED`` (H2) — see that
    constant's comment.
    """
    if marketplace is None:
        marketplace = resolve_marketplace()
    for path in (settings_paths if settings_paths is not None else managed_settings_paths()):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):  # OSError covers PermissionError; ValueError covers JSONDecodeError
            continue
        if not isinstance(data, dict) or not data.get("channelsEnabled"):
            continue
        for entry in data.get("allowedChannelPlugins") or []:
            if (isinstance(entry, dict)
                    and entry.get("plugin") == plugin
                    and entry.get("marketplace") == marketplace):
                return True
    return False


def build_claude_command(prompt, extra_args=None, settings_paths=None):
    """Build the ``claude`` argv as a LIST (never a shell string).

    Base = shlex-split of ``$TEAMMATE_LAUNCH_ARGS`` if set (verbatim override), else the
    allowlisted ``--channels`` line when a managed-settings file trusts this channel, else
    the ``--dangerously-load-development-channels`` line (both built against the RESOLVED
    marketplace — W1). Then ``--permission-mode bypassPermissions``, then ``extra_args``,
    then the ``prompt`` as a single trailing element. (No ``--name``: that flag is print-only.)

    W6: ``posix=(os.name != "nt")`` — shlex.split defaults to POSIX quoting rules, which treat
    backslash as an escape character and silently corrupt an unquoted Windows path
    (``C:\\Users\\name`` → ``C:Usersname``) in TEAMMATE_LAUNCH_ARGS, in a Windows-first module.
    Contract on Windows: LAUNCH_ARGS values are whitespace-split and quotes are PRESERVED
    verbatim (not stripped) — acceptable; the alternative (corrupted paths) is worse.
    """
    base = os.environ.get("TEAMMATE_LAUNCH_ARGS")
    if not base:
        if channel_allowlisted(settings_paths=settings_paths):
            base = allowlisted_launch_args()
            # H2: name the schema's verification vintage + the escape hatch right where the
            # assumption acts, so a future CC schema change is diagnosable from the debug log
            # instead of a mysterious channel-loading failure.
            print(f"[teammate-comms] managed-settings allowlist matched (schema verified "
                  f"against {MANAGED_SETTINGS_VERIFIED}); set TEAMMATE_LAUNCH_ARGS to override.",
                  file=sys.stderr, flush=True)
        else:
            base = dangerous_launch_args()
    argv = shlex.split(base, posix=(os.name != "nt"))
    argv += ["--permission-mode", "bypassPermissions"]
    if extra_args:
        argv += list(extra_args)
    if prompt:
        argv.append(prompt)  # single element — metacharacters can't inject
    return argv


def build_child_env(base, agent, project_dir, team=None, comms_dir=None, spawned_by=None):
    """Build the child env dict for the spawned instance.

    Sets ``TEAMMATE_AGENT`` (→ auto-register as that name) and ``CLAUDE_PROJECT_DIR``
    (→ fills the ``project`` field; the server reads the env var, not cwd). Optionally
    ``TEAMMATE_TEAM`` / ``TEAMMATE_COMMS_DIR``. ``spawned_by`` stamps the provenance breadcrumb
    (F-5). Raises ``CommsError`` if the two handoff vars aren't set (a real raise, not an
    ``assert`` — asserts are a no-op under ``-O``).
    """
    env = dict(base)
    env["TEAMMATE_AGENT"] = agent
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if team:
        env["TEAMMATE_TEAM"] = team
    if comms_dir:
        env["TEAMMATE_COMMS_DIR"] = str(comms_dir)
    # Provenance breadcrumb (F-5): stamp WHO spawned this child so its register record knows its
    # origin. SET (or POP) UNCONDITIONALLY so a stale value can NEVER be inherited from the
    # parent's own env — a grand-child carries its IMMEDIATE parent, never the grandparent. Same
    # never-inherit discipline as the reincarnate gate stripped just below (the F-1 lesson).
    if spawned_by:
        env["TEAMMATE_SPAWNED_BY"] = spawned_by
    else:
        env.pop("TEAMMATE_SPAWNED_BY", None)
    # WP-36 gate finding (BLOCKER): a reincarnated child launches via a fresh console/wt.exe,
    # never inside the PARENT's WezTerm pane — so WEZTERM_PANE/WEZTERM_UNIX_SOCKET inherited
    # verbatim from `base` would register the child with its MANAGER's pane binding, and the
    # broker would then type /compact + CR into the manager's own pane. Same never-inherit
    # discipline as TEAMMATE_SPAWNED_BY above: popped unconditionally, never carried down.
    env.pop("WEZTERM_PANE", None)
    env.pop("WEZTERM_UNIX_SOCKET", None)
    # Strip the reincarnate GATE so a spawned child can't itself re-spawn unless its operator
    # explicitly opts back in (the reincarnate gate is opt-in-default-off by design, F-1). This
    # is the gate ONLY — TEAMMATE_LAUNCH_ARGS is a launch override the child SHOULD inherit so
    # it spawns the same (e.g. allowlisted --channels), so it is deliberately NOT stripped.
    env.pop("TEAMMATE_REINCARNATE_ENABLED", None)
    if not (env.get("TEAMMATE_AGENT") and env.get("CLAUDE_PROJECT_DIR")):
        raise CommsError("build_child_env requires a non-empty agent and project_dir.")
    return env


# W5: Popen handles were never collected, so completed spawn children accumulated as zombies
# on POSIX for the life of the server (nothing ever waited on them). _children tracks every
# handle spawn_in_terminal opens; _reap_children drops finished ones each call.
_children = []
_sigchld_ignored = False


def _reap_children():
    """Drop finished child handles from ``_children`` (W5). Best-effort: a handle that can't
    be polled (e.g. a test double standing in for Popen) is dropped rather than raising —
    reaping is opportunistic cleanup, never load-bearing for the spawn itself."""
    still_running = []
    for p in _children:
        try:
            if p.poll() is None:
                still_running.append(p)
        except Exception:
            pass
    _children[:] = still_running


def _ignore_sigchld_once():
    """POSIX only, once per process: ignore SIGCHLD so a spawned child's exit needs no reaping
    in the first place (belt-and-suspenders with ``_reap_children``). Guarded — signal handlers
    are only settable from the main thread; spawn is dispatched from the server's main stdin
    loop, but this is wrapped in try/except anyway so a threading/platform quirk can never
    break a spawn."""
    global _sigchld_ignored
    if _sigchld_ignored or os.name == "nt":
        return
    _sigchld_ignored = True
    try:
        import signal
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    except Exception:
        pass


def spawn_in_terminal(argv, cwd, env):
    """Open a new terminal window running ``argv`` in ``cwd`` with ``env``, detached, with
    child stdio = DEVNULL. Returns the launched argv for the status message. Raises
    ``FileNotFoundError`` if ``claude`` isn't on PATH (checked BEFORE any Popen — W1) or if no
    terminal launcher succeeds.
    """
    _reap_children()
    _ignore_sigchld_once()
    if shutil.which("claude") is None:
        raise FileNotFoundError(
            "claude CLI not on PATH — the spawned teammate cannot launch. Install it or fix "
            "PATH before retrying teammate_reincarnate."
        )
    dn = subprocess.DEVNULL
    cwd = str(cwd)
    if os.name == "nt":
        # Primary: Windows Terminal — `-d <cwd> -- <argv>` execs claude directly (no shell
        # re-parse) and wt is its own top-level process, so the child outlives the server.
        try:
            wt = ["wt.exe", "-d", cwd, "--"] + argv
            p = subprocess.Popen(wt, env=env, stdin=dn, stdout=dn, stderr=dn)
            _children.append(p)
            return wt
        except FileNotFoundError:
            pass
        # Fallback: a fresh interactive console (NOT DETACHED_PROCESS — interactive claude
        # needs a console). CREATE_BREAKAWAY_FROM_JOB guards against a kill-on-close job.
        CREATE_NEW_CONSOLE = 0x00000010
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=dn, stdout=dn, stderr=dn,
                             creationflags=CREATE_NEW_CONSOLE | CREATE_BREAKAWAY_FROM_JOB)
        _children.append(p)
        return argv
    # POSIX best-effort: try terminal emulators, each exec'ing argv directly (list-form).
    candidates = [
        ["x-terminal-emulator", "-e"] + argv,
        ["gnome-terminal", "--"] + argv,
        ["konsole", "-e"] + argv,
        ["xterm", "-e"] + argv,
    ]
    last = None
    for cand in candidates:
        try:
            p = subprocess.Popen(cand, cwd=cwd, env=env, stdin=dn, stdout=dn, stderr=dn,
                                 start_new_session=True)
            _children.append(p)
            return cand
        except FileNotFoundError as e:
            last = e
            continue
    raise FileNotFoundError(
        "no terminal launcher found (tried wt.exe / x-terminal-emulator / gnome-terminal "
        f"/ konsole / xterm): {last}")
