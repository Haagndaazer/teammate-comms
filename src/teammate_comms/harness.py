"""Harness policy table: every fact that differs between the agent harnesses this plugin
runs under, resolved once from TEAMMATE_HARNESS. Field names track vibe-cognition's
harness.py so the shared renderer stays a verbatim copy."""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

CLAUDE_CODE = "claude-code"
CODEX = "codex"
ENV_VAR = "TEAMMATE_HARNESS"
DEFAULT = CLAUDE_CODE

PLUGIN_NAME = "teammate-comms"
PLUGIN_ROOT_ENV = "CLAUDE_PLUGIN_ROOT"
PLUGIN_DATA_ENV = "CLAUDE_PLUGIN_DATA"
WHATS_NEW_PATH = ".claude-plugin/whats-new.json"
MARKETPLACE_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/Haagndaazer/colton-claude-plugins/main/{path}")
PLUGIN_URL_TEMPLATE = "https://raw.githubusercontent.com/Haagndaazer/teammate-comms/{sha}/{path}"

PROCESS_NAMES = {
    CLAUDE_CODE: ("claude.exe",),
    CODEX: ("codex.exe",),
}
PROCESS_PATH_EXCLUDES = ("anthropicclaude", "windowsapps")
TRUST_CONFIG_ENV = "CLAUDE_CONFIG_DIR"
TRUST_CONFIG_NAME = ".claude.json"
TRUST_KEY = "hasTrustDialogAccepted"
SESSION_ENV_PREFIXES = ("CLAUDE_CODE_",)
SESSION_ENV_NAMES = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CODEX_THREAD_ID", "WT_SESSION")


@dataclass(frozen=True)
class Harness:
    name: str
    display_name: str
    skill_prefix: str
    spawn_tool: str
    default_models: dict
    wake: str
    durable_wake: bool
    needs_session: bool
    session_hint: str
    launch_args_var: str
    spawn_builder: str
    spawn_prompt: str
    install_cta: str
    debug_hint: str
    manifest_path: str
    marketplace_manifest_path: str

    def skill_invoke(self, skill):
        return f"{self.skill_prefix}{skill}"


HARNESSES = {
    CLAUDE_CODE: Harness(
        name=CLAUDE_CODE,
        display_name="Claude Code",
        skill_prefix="/",
        spawn_tool="Agent tool",
        default_models={"small": "haiku", "mid": "sonnet"},
        wake="channel",
        durable_wake=False,
        needs_session=False,
        session_hint="",
        launch_args_var="TEAMMATE_LAUNCH_ARGS",
        spawn_builder="claude",
        spawn_prompt=("You are {agent}. Call teammate_inbox to drain any queued messages, "
                      "then await instructions."),
        install_cta="/plugin update teammate-comms@coltondyck",
        debug_hint="~/.claude/debug/<session-id>.txt",
        manifest_path=".claude-plugin/plugin.json",
        marketplace_manifest_path=".claude-plugin/marketplace.json",
    ),
    CODEX: Harness(
        name=CODEX,
        display_name="Codex",
        skill_prefix="$",
        spawn_tool="`spawn_agent` tool",
        default_models={"small": None, "mid": None},
        wake="codex-queue",
        durable_wake=True,
        needs_session=True,
        session_hint=(
            "Re-register with harness_session set to your thread id (from the session-start "
            "context or $CODEX_THREAD_ID) so teammates can wake you."
        ),
        launch_args_var="TEAMMATE_LAUNCH_ARGS_CODEX",
        spawn_builder="codex",
        spawn_prompt=("You are teammate {agent}. First call teammate_register(agent='{agent}', "
                      "harness_session=<the value of $CODEX_THREAD_ID in your shell>, "
                      "project_dir='{project_dir}'), then teammate_inbox to drain any queued "
                      "messages, then await instructions."),
        install_cta="codex plugin marketplace upgrade coltondyck (then restart Codex)",
        debug_hint="the Codex session log",
        manifest_path=".codex-plugin/plugin.json",
        marketplace_manifest_path=".agents/plugins/marketplace.json",
    ),
}

_warned = set()


def _folder_key(path):
    return str(path).replace(chr(92), "/").rstrip("/").lower()


def folder_trusted(name, project_dir, env=None, home=None):
    """Whether the harness would open ``project_dir`` without asking its trust question.

    True or False from the harness's own record; None when that cannot be told. Read only:
    trust is never granted from here.
    """
    if name != CLAUDE_CODE:
        return None
    env = os.environ if env is None else env
    home = Path(home) if home else Path.home()
    base = Path(env.get(TRUST_CONFIG_ENV) or home)
    try:
        data = json.loads((base / TRUST_CONFIG_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return None
    trusted = {_folder_key(k) for k, v in projects.items()
               if isinstance(v, dict) and v.get(TRUST_KEY) is True}
    folder = Path(project_dir)
    for cur in (folder, *folder.parents):
        if _folder_key(cur) == _folder_key(home):
            break
        if _folder_key(cur) in trusted:
            return True
        if (cur / ".git").exists():
            break
    return False


def current(env=None):
    """Resolve the running harness from the environment; unknown values fall back to DEFAULT."""
    env = os.environ if env is None else env
    raw = (env.get(ENV_VAR) or "").strip().lower()
    if not raw:
        return HARNESSES[DEFAULT]
    harness = HARNESSES.get(raw)
    if harness is None:
        if raw not in _warned:
            _warned.add(raw)
            print(f"[teammate-comms] unknown {ENV_VAR}={raw!r}; using {DEFAULT}",
                  file=sys.stderr, flush=True)
        return HARNESSES[DEFAULT]
    return harness


def by_name(name):
    """Look up a harness by name, or None."""
    if not isinstance(name, str):
        return None
    return HARNESSES.get(name.strip().lower())
