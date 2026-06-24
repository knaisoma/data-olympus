#!/usr/bin/env bash
# Sourced by: hooks (agent-hooks/*) and bin/kb onboarding-check.
# Resolves CWD to (WORKSPACE, COMPONENT?, WORKSPACE_REMOTE_URL?, COMPONENT_REMOTE_URL?)
# via upward path traversal.

detect_workspace_and_component() {
    local start="${1:-$PWD}"
    local cwd
    cwd=$(cd "$start" && pwd -P) || return 1

    # Walk up until we find a directory under ~/kn-projects/.
    local workspace_root=""
    local cur="$cwd"
    while [ "$cur" != "/" ]; do
        if [ "$(dirname "$cur")" = "$HOME/kn-projects" ]; then
            workspace_root="$cur"
            break
        fi
        cur=$(dirname "$cur")
    done

    if [ -z "$workspace_root" ]; then
        return 1
    fi

    WORKSPACE=$(basename "$workspace_root")
    WORKSPACE_REMOTE_URL=""
    COMPONENT=""
    COMPONENT_REMOTE_URL=""

    if [ -d "$workspace_root/.git" ]; then
        WORKSPACE_REMOTE_URL=$(git -C "$workspace_root" remote get-url origin 2>/dev/null || true)
    fi

    if [ "$cwd" != "$workspace_root" ]; then
        local rel="${cwd#$workspace_root/}"
        local probe="$workspace_root"
        local seg
        local IFS_save="$IFS"
        IFS=/
        for seg in $rel; do
            IFS="$IFS_save"
            probe="$probe/$seg"
            if [ -d "$probe/.git" ]; then
                COMPONENT=$(basename "$probe")
                COMPONENT_REMOTE_URL=$(git -C "$probe" remote get-url origin 2>/dev/null || true)
                break
            fi
        done
        IFS="$IFS_save"
    fi

    export WORKSPACE COMPONENT WORKSPACE_REMOTE_URL COMPONENT_REMOTE_URL
    return 0
}
