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
import subprocess
import sys

from .comms import CommsError

# The custom teammate-comms channel is not on Anthropic's built-in allowlist, so by default
# it must be loaded with --dangerously-load-development-channels (which triggers a one-time
# trust prompt the spawned, DEVNULL-stdio child can't answer). BUT if the operator has placed
# a managed-settings file allowlisting this plugin, the channel is trusted and loads with the
# plain --channels flag and NO prompt — so we auto-detect that and prefer it (see
# channel_allowlisted). $TEAMMATE_LAUNCH_ARGS overrides both, verbatim.
PLUGIN_NAME = "teammate-comms"
MARKETPLACE = "coltondyck"
PLUGIN_SPEC = f"plugin:{PLUGIN_NAME}@{MARKETPLACE}"
DANGEROUS_LAUNCH_ARGS = f"claude --dangerously-load-development-channels {PLUGIN_SPEC}"
ALLOWLISTED_LAUNCH_ARGS = f"claude --channels {PLUGIN_SPEC}"


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


def channel_allowlisted(plugin=PLUGIN_NAME, marketplace=MARKETPLACE, settings_paths=None):
    """True iff a managed-settings file marks this channel trusted, so it loads with the
    plain --channels flag (no dangerous flag, no startup prompt).

    Trusted = ``channelsEnabled`` truthy AND an ``allowedChannelPlugins`` entry matching this
    plugin+marketplace. We REQUIRE ``channelsEnabled`` deliberately (do not relax it): if we
    guess wrong and fall back to the dangerous flag, the channel still loads (maybe a prompt);
    if we wrongly returned True, the child would launch with --channels but no dangerous flag
    and the channel might not load at all — the worse failure. So we fail toward the safe flag.

    Best-effort: a missing/unreadable/malformed file (incl. PermissionError reading a
    Program Files file as non-admin) is skipped → False. utf-8-sig tolerates a Notepad BOM.
    """
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
    the ``--dangerously-load-development-channels`` line. Then ``--permission-mode
    bypassPermissions``, then ``extra_args``, then the ``prompt`` as a single trailing
    element. (No ``--name``: that flag is print-only.)
    """
    base = os.environ.get("TEAMMATE_LAUNCH_ARGS")
    if not base:
        base = (ALLOWLISTED_LAUNCH_ARGS if channel_allowlisted(settings_paths=settings_paths)
                else DANGEROUS_LAUNCH_ARGS)
    argv = shlex.split(base)
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
    # Strip the reincarnate GATE so a spawned child can't itself re-spawn unless its operator
    # explicitly opts back in (the reincarnate gate is opt-in-default-off by design, F-1). This
    # is the gate ONLY — TEAMMATE_LAUNCH_ARGS is a launch override the child SHOULD inherit so
    # it spawns the same (e.g. allowlisted --channels), so it is deliberately NOT stripped.
    env.pop("TEAMMATE_REINCARNATE_ENABLED", None)
    if not (env.get("TEAMMATE_AGENT") and env.get("CLAUDE_PROJECT_DIR")):
        raise CommsError("build_child_env requires a non-empty agent and project_dir.")
    return env


def spawn_in_terminal(argv, cwd, env):
    """Open a new terminal window running ``argv`` in ``cwd`` with ``env``, detached, with
    child stdio = DEVNULL. Returns the launched argv for the status message. Raises
    ``FileNotFoundError`` if no launcher succeeds (e.g. ``claude``/terminal not on PATH).
    """
    dn = subprocess.DEVNULL
    cwd = str(cwd)
    if os.name == "nt":
        # Primary: Windows Terminal — `-d <cwd> -- <argv>` execs claude directly (no shell
        # re-parse) and wt is its own top-level process, so the child outlives the server.
        try:
            wt = ["wt.exe", "-d", cwd, "--"] + argv
            subprocess.Popen(wt, env=env, stdin=dn, stdout=dn, stderr=dn)
            return wt
        except FileNotFoundError:
            pass
        # Fallback: a fresh interactive console (NOT DETACHED_PROCESS — interactive claude
        # needs a console). CREATE_BREAKAWAY_FROM_JOB guards against a kill-on-close job.
        CREATE_NEW_CONSOLE = 0x00000010
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        subprocess.Popen(argv, cwd=cwd, env=env, stdin=dn, stdout=dn, stderr=dn,
                         creationflags=CREATE_NEW_CONSOLE | CREATE_BREAKAWAY_FROM_JOB)
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
            subprocess.Popen(cand, cwd=cwd, env=env, stdin=dn, stdout=dn, stderr=dn,
                             start_new_session=True)
            return cand
        except FileNotFoundError as e:
            last = e
            continue
    raise FileNotFoundError(
        "no terminal launcher found (tried wt.exe / x-terminal-emulator / gnome-terminal "
        f"/ konsole / xterm): {last}")
