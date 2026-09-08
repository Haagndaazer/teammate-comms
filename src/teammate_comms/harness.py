"""Harness table: every harness-specific fact lives here, keyed by ``TEAMMATE_HARNESS``."""

import os
import sys
from dataclasses import dataclass

ENV_VAR = "TEAMMATE_HARNESS"
DEFAULT = "claude-code"


@dataclass(frozen=True)
class Harness:
    name: str
    display: str
    wake: str
    durable_wake: bool
    needs_session: bool
    session_hint: str
    skill_invoke: str
    launch_args_var: str
    spawn_builder: str
    install_cta: str
    debug_hint: str


HARNESSES = {
    "claude-code": Harness(
        name="claude-code",
        display="Claude Code",
        wake="channel",
        durable_wake=False,
        needs_session=False,
        session_hint="",
        skill_invoke="/teammate-comms",
        launch_args_var="TEAMMATE_LAUNCH_ARGS",
        spawn_builder="claude",
        install_cta="/plugin update teammate-comms@coltondyck",
        debug_hint="~/.claude/debug/<session-id>.txt",
    ),
    "codex": Harness(
        name="codex",
        display="Codex",
        wake="codex-queue",
        durable_wake=True,
        needs_session=True,
        session_hint=(
            "Re-register with harness_session set to your thread id (from the session-start "
            "context or $CODEX_THREAD_ID) so teammates can wake you."
        ),
        skill_invoke="$teammate-comms",
        launch_args_var="TEAMMATE_LAUNCH_ARGS_CODEX",
        spawn_builder="codex",
        install_cta="codex plugin marketplace upgrade coltondyck (then restart Codex)",
        debug_hint="the Codex session log",
    ),
}

_warned = set()


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
