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
VENV_DIR="${UV_PROJECT_ENVIRONMENT:-${PLUGIN_ROOT}/.venv}"
STAMP="${VENV_DIR}/.uv-sync-stamp"

_json_escape() {
    printf '%s' "$1" | tr -d '\r\n' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g'
}

emit_context() {
    # $1 = additionalContext string (already JSON-escaped by the caller's heredoc);
    # a harness note in TEAMMATE_HOOK_NOTE is escaped here and prefixed to every exit.
    local text="$1"
    if [ -n "${TEAMMATE_HOOK_NOTE:-}" ]; then
        local note
        note="$(_json_escape "$TEAMMATE_HOOK_NOTE")"
        if [ -n "$text" ]; then text="${note} ${text}"; else text="$note"; fi
    fi
    cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "$text"
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
FIRST_NOTE=""
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
            FIRST_NOTE="teammate-comms just built its environment for the first time. If the teammate_* tools are not available in this session, restart Claude Code once (check with /mcp)."
        fi
    fi
fi

# ── Step 3: what changed since the last session, and whether a newer version is pinned ──
# Each module prints nothing or ONE line that is already JSON-escaped. A failure of either is
# an empty note, never a dead hook. Skipped without a plugin data dir and under Codex (its
# context budget is small and its marketplace does not list this plugin yet).
WHATSNEW_NOTE=""
UPDATE_NOTE=""
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-}"
if [ -n "$PLUGIN_DATA" ] && [ "${TEAMMATE_HARNESS:-}" != "codex" ]; then
    PLUGIN_ROOT_NATIVE="$PLUGIN_ROOT"
    PLUGIN_DATA_NATIVE="$PLUGIN_DATA"
    if command -v cygpath &>/dev/null; then
        PLUGIN_ROOT_NATIVE=$(cygpath -m "$PLUGIN_ROOT" 2>/dev/null) || PLUGIN_ROOT_NATIVE="$PLUGIN_ROOT"
        PLUGIN_DATA_NATIVE=$(cygpath -m "$PLUGIN_DATA" 2>/dev/null) || PLUGIN_DATA_NATIVE="$PLUGIN_DATA"
    fi
    _notice() {
        UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
            CLAUDE_PLUGIN_ROOT="${PLUGIN_ROOT_NATIVE}" \
            CLAUDE_PLUGIN_DATA="${PLUGIN_DATA_NATIVE}" \
            uv run --no-sync --project "${PLUGIN_ROOT}" python -m "$1" </dev/null 2>/dev/null \
            | tr -d '\r\n'
    }
    _is_off() {
        case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
            off|0|false|no) return 0 ;;
        esac
        return 1
    }

    if ! _is_off "${TEAMMATE_WHATS_NEW:-}"; then
        SEEN_VERSION=""
        if [ -f "${PLUGIN_DATA}/whats-new-seen" ]; then
            SEEN_VERSION=$(cat "${PLUGIN_DATA}/whats-new-seen" 2>/dev/null) || SEEN_VERSION=""
        fi
        INSTALLED_VERSION=$(grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' \
            "${PLUGIN_ROOT}/.claude-plugin/plugin.json" 2>/dev/null \
            | head -1 | sed -E 's/.*"([^"]*)"[[:space:]]*$/\1/') || INSTALLED_VERSION=""
        if [ -z "$SEEN_VERSION" ] || [ "$SEEN_VERSION" != "$INSTALLED_VERSION" ]; then
            WHATSNEW_NOTE=$(_notice teammate_comms.whats_new) || WHATSNEW_NOTE=""
        fi
    fi

    if ! _is_off "${TEAMMATE_UPDATE_NUDGE:-}"; then
        FIND_BIN="/usr/bin/find"
        if [ ! -x "$FIND_BIN" ]; then
            FIND_BIN="find"
        fi
        STAMP_FRESH=false
        if [ -f "${PLUGIN_DATA}/update-check.json" ]; then
            if [ -n "$("$FIND_BIN" "${PLUGIN_DATA}/update-check.json" -mtime -1 2>/dev/null)" ]; then
                STAMP_FRESH=true
            fi
        fi
        if [ "$STAMP_FRESH" = false ]; then
            UPDATE_NOTE=$(_notice teammate_comms.update_check) || UPDATE_NOTE=""
        fi
    fi
fi

# Identity is established at runtime via the teammate_register tool, so an unset
# TEAMMATE_AGENT is the normal case — nothing to warn about.
TEXT=""
for PART in "$FIRST_NOTE" "$WHATSNEW_NOTE" "$UPDATE_NOTE"; do
    if [ -n "$PART" ]; then
        if [ -n "$TEXT" ]; then TEXT="${TEXT}\n\n${PART}"; else TEXT="$PART"; fi
    fi
done
if [ -n "$TEXT" ] || [ -n "${TEAMMATE_HOOK_NOTE:-}" ]; then
    emit_context "$TEXT"
else
    echo '{}'
fi
exit 0
