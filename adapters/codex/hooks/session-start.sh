#!/usr/bin/env bash
# Codex SessionStart wrapper: registers the MCP server with Codex (user-level, so the
# server inherits the project cwd), hands the agent its thread id + cwd, then runs the
# shared hook.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT is not set (Codex sets it for plugin hooks)}"
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:?CLAUDE_PLUGIN_DATA is not set (Codex sets it for plugin hooks)}"
mkdir -p "$PLUGIN_DATA"

HOOK_INPUT=""
if [ ! -t 0 ]; then
    HOOK_INPUT="$(cat || true)"
fi

export TEAMMATE_HARNESS=codex

if command -v cygpath >/dev/null 2>&1; then
    ROOT_NATIVE=$(cygpath -m "$PLUGIN_ROOT")
    DATA_NATIVE=$(cygpath -m "$PLUGIN_DATA")
else
    ROOT_NATIVE="$PLUGIN_ROOT"
    DATA_NATIVE="$PLUGIN_DATA"
fi
VENV_DIR="${DATA_NATIVE}/.venv"
export UV_PROJECT_ENVIRONMENT="$VENV_DIR"

SERVER_NAME="teammate-comms"
DESIRED="v1|uv run --no-sync --project ${ROOT_NATIVE} python -m teammate_comms.server|UV_PROJECT_ENVIRONMENT=${VENV_DIR}|TEAMMATE_HARNESS=codex"
STAMP="${PLUGIN_DATA}/codex-mcp.stamp"
LOCK="${PLUGIN_DATA}/codex-mcp.lock"
MANUAL_CMD="codex mcp add ${SERVER_NAME} --env UV_PROJECT_ENVIRONMENT=${VENV_DIR} --env TEAMMATE_HARNESS=codex -- uv run --no-sync --project \"${ROOT_NATIVE}\" python -m teammate_comms.server"

_bc() { echo "[teammate-comms codex hook] pid=$$ $1 t=$(date +%s)" >&2; }

_stamp_matches() { [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$DESIRED" ]; }

MANAGED_ENV="UV_PROJECT_ENVIRONMENT TEAMMATE_HARNESS"

_preserved_env_args() {
    local json line key
    json=$(codex mcp get "$SERVER_NAME" --json 2>/dev/null) || return 0
    printf '%s\n' "$json" \
        | sed -n '/"env": {}/d; /"env": {/,/^[[:space:]]*}/p' \
        | sed -n 's/^[[:space:]]*"\([^"]*\)": "\(.*\)",\{0,1\}[[:space:]]*$/\1=\2/p' \
        | sed 's/\\\\/\\/g; s/\\"/"/g' \
        | while IFS= read -r line; do
            key="${line%%=*}"
            case " $MANAGED_ENV " in
                *" $key "*) ;;
                *) printf '%s\n' "$line" ;;
            esac
        done
}

_register() {
    if ! command -v codex >/dev/null 2>&1; then
        NOTE="teammate-comms: the 'codex' CLI was not found on PATH from the session-start hook, so its MCP server is not registered yet. Run this once, then restart Codex: ${MANUAL_CMD}"
        return 0
    fi
    _bc "register_start"
    local extra_args=()
    local line
    while IFS= read -r line; do
        [ -n "$line" ] && extra_args+=(--env "$line")
    done < <(_preserved_env_args)
    if codex mcp add "$SERVER_NAME" \
        --env "UV_PROJECT_ENVIRONMENT=${VENV_DIR}" \
        --env "TEAMMATE_HARNESS=codex" \
        "${extra_args[@]+"${extra_args[@]}"}" \
        -- uv run --no-sync --project "$ROOT_NATIVE" python -m teammate_comms.server >/dev/null 2>&1; then
        printf '%s' "$DESIRED" > "$STAMP"
        NOTE="teammate-comms registered its MCP server with Codex (user-level entry '${SERVER_NAME}'). INSTRUCTION: tell the user to restart Codex once to activate it; the teammate_* tools appear from the next session on."
        _bc "register_done_ok preserved_env=${#extra_args[@]}"
    else
        NOTE="teammate-comms: 'codex mcp add' failed while registering the MCP server. INSTRUCTION: tell the user to run this once and restart Codex: ${MANUAL_CMD}"
        _bc "register_done_fail"
    fi
}

NOTE=""
if ! _stamp_matches; then
    acquired=false
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        if mkdir "$LOCK" 2>/dev/null; then acquired=true; break; fi
        if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +2 2>/dev/null)" ]; then rmdir "$LOCK" 2>/dev/null || true; continue; fi
        sleep 0.5
    done
    if [ "$acquired" = true ]; then
        trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
        if ! _stamp_matches; then
            _register
        fi
        rmdir "$LOCK" 2>/dev/null || true
        trap - EXIT
    else
        _bc "register_skipped_lock_busy"
    fi
else
    _bc "register_skipped_stamp_match"
fi

_json_field() {
    printf '%s' "$HOOK_INPUT" \
        | sed -n -E 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"((\\.|[^"\\])*)".*/\1/p' \
        | head -n 1 \
        | sed -e 's/\\"/"/g' -e 's/\\\\/\\/g'
}

THREAD_ID="$(_json_field session_id)"
CWD_VAL="$(_json_field cwd)"
HANDOFF=""
if [ -n "$THREAD_ID" ]; then
    HANDOFF="teammate-comms: harness=codex thread_id=${THREAD_ID} project_dir=${CWD_VAL}. When registering, pass harness_session=${THREAD_ID} and project_dir=${CWD_VAL}."
fi
export TEAMMATE_HOOK_NOTE="${HANDOFF}${HANDOFF:+${NOTE:+ }}${NOTE}"

printf '%s' "$HOOK_INPUT" | bash "${PLUGIN_ROOT}/hooks/session-start.sh"
exit $?
