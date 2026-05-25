#!/usr/bin/env bash
# SessionStart hook — pre-build the (zero-dep) venv so the MCP server can launch
# with `uv run --no-sync` without ever blocking the stdio handshake, and remind
# the user to set TEAMMATE_AGENT if it is missing.
#
# Unlike vibe-cognition this does NOT write a per-project .mcp.json: teammate-comms
# declares its MCP server inline in plugin.json.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT}"
VENV_DIR="${PLUGIN_ROOT}/.venv"
STAMP="${VENV_DIR}/.uv-sync-stamp"

emit_context() {
    # $1 = additionalContext string (already JSON-escaped by the caller's heredoc)
    cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "$1"
  }
}
EOF
}

# ── Step 1: require uv ───────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    emit_context "WARNING: the teammate-comms plugin requires 'uv' (Python package manager) but it was not found on PATH. Install it: https://docs.astral.sh/uv/getting-started/installation/"
    exit 0
fi

# ── Step 2: conditional venv build (stamped on pyproject.toml + uv.lock) ──
if command -v sha256sum &>/dev/null; then
    HASH=$(cat "${PLUGIN_ROOT}/pyproject.toml" "${PLUGIN_ROOT}/uv.lock" 2>/dev/null | sha256sum | cut -d' ' -f1)
elif command -v shasum &>/dev/null; then
    HASH=$(cat "${PLUGIN_ROOT}/pyproject.toml" "${PLUGIN_ROOT}/uv.lock" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
else
    HASH="no-hash-tool"
fi

if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$HASH" ]; then
    UV_PROJECT_ENVIRONMENT="${VENV_DIR}" uv sync --project "${PLUGIN_ROOT}" --no-dev 2>/dev/null || true
    mkdir -p "${VENV_DIR}"
    echo "$HASH" > "$STAMP"
fi

# ── Step 3: identity reminder ────────────────────────────────────────
if [ -z "${TEAMMATE_AGENT:-}" ]; then
    emit_context "teammate-comms is installed but TEAMMATE_AGENT is not set, so its MCP channel will not connect. To enable agent-to-agent wake, set the identity in your shell BEFORE launching: PowerShell \`\$env:TEAMMATE_AGENT='YourName'\`; bash \`export TEAMMATE_AGENT=YourName\`. Channels also require launching with --dangerously-load-development-channels plugin:teammate-comms@colton-comms."
    exit 0
fi

echo '{}'
exit 0
