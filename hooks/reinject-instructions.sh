#!/usr/bin/env bash
# SessionStart(compact) hook — re-inject the teammate-comms standing instructions.
#
# The server's INSTRUCTIONS reach the agent via the MCP initialize handshake, but it is
# undocumented whether those survive a context compaction. This hook (matcher: compact in
# hooks.json) re-emits them as additionalContext after a compact so the standing rules
# (incl. "update your status as you work") stay in force. Emits a SessionStart
# hookSpecificOutput JSON, or '{}' on any failure.
set -euo pipefail

# P5: mirror session-start.sh's stdin self-filter, inverted — this hook (matcher: compact)
# should fire ONLY on a compact source. If a "source" key is present and its value is NOT
# "compact", emit '{}' and exit 0. Defense-in-depth: the hooks.json matcher is the primary
# gate, which the audit flagged as unverified against any recorded Claude Code contract;
# absent/unknown source falls through and PROCEEDS (the matcher already gated the normal
# case). Only read stdin when it's NOT a tty so a manual run can never block on `cat`.
if [ ! -t 0 ]; then
    HOOK_INPUT="$(cat || true)"
    if printf '%s' "$HOOK_INPUT" | grep -q '"source"[[:space:]]*:[[:space:]]*"' \
       && ! printf '%s' "$HOOK_INPUT" | grep -q '"source"[[:space:]]*:[[:space:]]*"compact"'; then
        echo '{}'
        exit 0
    fi
fi

# Fail closed but VISIBLE: an unset CLAUDE_PLUGIN_ROOT under `set -u` would abort at the bare
# ref below before any JSON is emitted (a silent dead hook); emit '{}' and exit 0 instead.
if [ -z "${CLAUDE_PLUGIN_ROOT:-}" ]; then
    echo '{}'
    exit 0
fi
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT}"
# Use the SAME venv resolution as session-start.sh (${PLUGIN_ROOT}/.venv) so `uv run` reuses
# the venv the server already launches from — never builds a fresh one mid-compact. Point
# UV_PROJECT_ENVIRONMENT at it explicitly (don't rely on uv's default discovery).
VENV_DIR="${PLUGIN_ROOT}/.venv"

# --no-sync is safe: a compact only happens mid-session, after a startup SessionStart
# already synced the (zero-dep) venv. Capture-then-print so a non-zero exit under `set -e`
# still yields valid JSON ('{}') instead of a torn/empty stdout; 2>/dev/null keeps uv
# chatter out of the JSON stream.
OUT=$(UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
    uv run --no-sync --project "${PLUGIN_ROOT}" \
    python -m teammate_comms.instructions 2>/dev/null) || OUT=""

if [ -n "$OUT" ]; then
    echo "$OUT"
else
    echo '{}'
fi
exit 0
