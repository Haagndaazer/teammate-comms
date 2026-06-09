#!/usr/bin/env bash
# SessionStart(compact) hook — re-inject the teammate-comms standing instructions.
#
# The server's INSTRUCTIONS reach the agent via the MCP initialize handshake, but it is
# undocumented whether those survive a context compaction. This hook (matcher: compact in
# hooks.json) re-emits them as additionalContext after a compact so the standing rules
# (incl. "update your status as you work") stay in force. Emits a SessionStart
# hookSpecificOutput JSON, or '{}' on any failure.
set -euo pipefail

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
