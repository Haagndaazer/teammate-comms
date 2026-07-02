#!/usr/bin/env bash
# SessionStart hook — pre-build the (zero-dep) venv so the MCP server can launch
# with `uv run --no-sync` without ever blocking the stdio handshake. (Identity is
# established at runtime via teammate_register, so an unset TEAMMATE_AGENT is the
# normal case and needs no warning — see the script tail.)
#
# Unlike vibe-cognition this does NOT write a per-project .mcp.json: teammate-comms
# declares its MCP server inline in plugin.json.
set -euo pipefail

# This entry is matcherless (fires on every SessionStart source). On a `compact` the venv
# already exists mid-session, so skip the build — self-filter on the hook's stdin
# {"source":...}, contract-independently (no reliance on hooks.json matcher syntax, which
# the audit flagged as unverified). Only read stdin when it's NOT a tty so a manual run can
# never block on `cat`; an unknown/absent source falls through to the normal build (safe).
if [ ! -t 0 ]; then
    HOOK_INPUT="$(cat || true)"
    if printf '%s' "$HOOK_INPUT" | grep -q '"source"[[:space:]]*:[[:space:]]*"compact"'; then
        echo '{}'
        exit 0
    fi
fi

# Fail closed but VISIBLE: an unset CLAUDE_PLUGIN_ROOT under `set -u` would otherwise abort
# at the bare ref below before any JSON is emitted (a silent dead hook). Emit '{}' instead.
if [ -z "${CLAUDE_PLUGIN_ROOT:-}" ]; then
    echo '{}'
    exit 0
fi
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
    emit_context "WARNING: the teammate-comms plugin requires 'uv' (Python package manager) but it was not found on PATH. Install it: https://docs.astral.sh/uv/getting-started/installation/ — after installing, restart Claude Code once and check with /mcp."
    exit 0
fi

# ── Step 2: conditional venv build (stamped on pyproject.toml + uv.lock + the avatars flag) ──
# P2: the avatars flag is part of the hash INPUT, not just a branch on the sync args — toggling
# TEAMMATE_AVATARS_ENABLED must invalidate the stamp and trigger a re-sync on its own; otherwise
# enabling it does nothing until pyproject.toml/uv.lock happen to change for an unrelated reason.
AVATARS_FLAG="${TEAMMATE_AVATARS_ENABLED:-}"
if command -v sha256sum &>/dev/null; then
    HASH=$( { cat "${PLUGIN_ROOT}/pyproject.toml" "${PLUGIN_ROOT}/uv.lock" 2>/dev/null; printf '%s' "$AVATARS_FLAG"; } | sha256sum | cut -d' ' -f1)
elif command -v shasum &>/dev/null; then
    HASH=$( { cat "${PLUGIN_ROOT}/pyproject.toml" "${PLUGIN_ROOT}/uv.lock" 2>/dev/null; printf '%s' "$AVATARS_FLAG"; } | shasum -a 256 | cut -d' ' -f1)
else
    HASH="no-hash-tool"
fi

# H3: a first install can't have the venv built before the FIRST MCP spawn tries to use it —
# capture "no stamp at all yet" BEFORE the sync attempt below, so a successful first sync can
# tell the agent to restart once (the prior fix was prose-only in README/DESIGN, invisible
# in-session; missing uv/bash otherwise present as the identical silent "no teammate_* tools").
FIRST_INSTALL=0
if [ ! -f "$STAMP" ]; then
    FIRST_INSTALL=1
fi

if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$HASH" ]; then
    # Stamp ONLY on a successful sync. A failed sync must NOT write the stamp — else the
    # half-built venv is recorded as done, the next session's hash matches and SKIPS the sync,
    # and the server fails to launch with no diagnostic. No stamp → next session retries
    # (self-healing). The build stays best-effort: a failure here never blocks the handshake.
    SYNC_ARGS=(--project "${PLUGIN_ROOT}" --no-dev)
    if [ "$AVATARS_FLAG" = "1" ]; then
        SYNC_ARGS+=(--extra images)
    fi
    if UV_PROJECT_ENVIRONMENT="${VENV_DIR}" uv sync "${SYNC_ARGS[@]}" 2>/dev/null; then
        mkdir -p "${VENV_DIR}"
        echo "$HASH" > "$STAMP"
        if [ "$FIRST_INSTALL" = "1" ]; then
            emit_context "teammate-comms just built its environment for the first time. If the teammate_* tools are not available in this session, restart Claude Code once (check with /mcp)."
            exit 0
        fi
    fi
fi

# Identity is established at runtime via the teammate_register tool, so an unset
# TEAMMATE_AGENT is the normal case — nothing to warn about. (Setting it is still
# supported: the server auto-registers from it if present.)
echo '{}'
exit 0
