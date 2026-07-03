#!/bin/sh
# Runs as root. Handles SSH key + (optional) /kb-main bootstrap, then drops to uid 65534.
set -e

# --- Git author/committer identity (scope item 10) ---------------------------
# The shipped image sets no git user, so a commit inside a fresh container fails
# with "Please tell me who you are". Export a default identity (operator-
# overridable via KB_GIT_AUTHOR_NAME / KB_GIT_AUTHOR_EMAIL) so the artifact can
# commit out of the box. The GIT_* vars are inherited by every git subprocess the
# server spawns (tools_write, push queue). See docs/serving.md.
GIT_NAME="${KB_GIT_AUTHOR_NAME:-data-olympus-mcp}"
GIT_EMAIL="${KB_GIT_AUTHOR_EMAIL:-data-olympus-mcp@localhost}"
export GIT_AUTHOR_NAME="$GIT_NAME"
export GIT_AUTHOR_EMAIL="$GIT_EMAIL"
export GIT_COMMITTER_NAME="$GIT_NAME"
export GIT_COMMITTER_EMAIL="$GIT_EMAIL"

# --- Writable scratch under a read-only root filesystem ----------------------
# When the container runs with readOnlyRootFilesystem, /tmp is an emptyDir mount
# that starts empty (masking the image's pre-created /tmp/uv-cache). Recreate the
# uv cache dir owned by the runtime user so `uv run` can write there.
mkdir -p "${UV_CACHE_DIR:-/tmp/uv-cache}"
chown 65534:65534 "${UV_CACHE_DIR:-/tmp/uv-cache}" || true

# --- SSH key for git push/pull -----------------------------------------------
# Mounted via the K8s Secret -> /etc/git-key-mount (read-only). We copy to
# /tmp/git-key so we can chown + chmod for the non-root runtime user.
if [ -f /etc/git-key-mount ]; then
    cp /etc/git-key-mount /tmp/git-key
    chown 65534:65534 /tmp/git-key
    chmod 0400 /tmp/git-key
    export GIT_SSH_COMMAND="ssh -i /tmp/git-key -o UserKnownHostsFile=/etc/ssh/known_hosts -o StrictHostKeyChecking=yes"
fi

# --- Bootstrap /kb-main on first boot ----------------------------------------
# On a fresh PVC the /kb-main mount is empty (possibly with a 'lost+found'
# directory created by the filesystem). The MCP refuses to start under those
# conditions (no git repo to refresh / search). We clone here once, fix
# ownership for the non-root user, and leave the directory ready.
#
# Idempotent: skipped on subsequent pod restarts because /kb-main is non-empty.
# Safe: only attempts the clone when KB_GIT_REMOTE_URL is set. If the clone
# fails (network, key, etc.) the volume stays empty and the MCP reports a
# degraded health state so the operator notices.

if [ -n "${KB_GIT_REMOTE_URL:-}" ] && [ -d /kb-main ]; then
    found_real_files=$(find /kb-main -mindepth 1 -maxdepth 1 \
        ! -name 'lost+found' -print -quit 2>/dev/null || true)
    if [ -z "$found_real_files" ]; then
        echo "[entrypoint] /kb-main is empty; cloning ${KB_GIT_REMOTE_URL}"
        if git clone "$KB_GIT_REMOTE_URL" /kb-main; then
            chown -R 65534:65534 /kb-main
            echo "[entrypoint] bootstrap complete"
        else
            echo "[entrypoint] WARNING: clone failed; pod will start degraded"
        fi
    fi
fi

exec gosu 65534:65534 "$@"
