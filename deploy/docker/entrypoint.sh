#!/bin/sh
# Runs as the NON-ROOT runtime uid (65534). No privilege drop (gosu is gone).
#
# In k8s the deploy-key permission fix and the first-boot /kb-main clone are done
# by a non-root initContainer (see deploy/k8s/statefulset.yaml) before this runs,
# so here we only prepare the SSH known_hosts + git identity and exec the server.
# For a plain `docker compose` run (no initContainer) this script also performs
# the key copy and the clone itself, still entirely as non-root.
set -e

# --- Git author/committer identity -------------------------------------------
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
# uv cache dir; we already run as the runtime user, so no chown is needed.
mkdir -p "${UV_CACHE_DIR:-/tmp/uv-cache}"

# --- known_hosts (user-writable copy) ----------------------------------------
# The image bakes /etc/ssh/known_hosts, but /etc is not writable to the non-root
# user under a read-only root FS, so we cannot append a runtime host there.
# Maintain a user-writable copy under /tmp and point SSH at it. If
# KB_SSH_KEYSCAN_HOST names a host not already trusted, add it at boot (lets a
# non-GitHub remote work without a rebuild).
KNOWN_HOSTS=/tmp/known_hosts
if [ -f /state/known_hosts ]; then
    # k8s: the initContainer already scanned KB_SSH_KEYSCAN_HOST into /state.
    cp /state/known_hosts "$KNOWN_HOSTS"
elif [ -f /etc/ssh/known_hosts ]; then
    cp /etc/ssh/known_hosts "$KNOWN_HOSTS"
else
    : > "$KNOWN_HOSTS"
fi
if [ -n "${KB_SSH_KEYSCAN_HOST:-}" ] && ! grep -q "^${KB_SSH_KEYSCAN_HOST} " "$KNOWN_HOSTS" 2>/dev/null; then
    ssh-keyscan -t ed25519,rsa "${KB_SSH_KEYSCAN_HOST}" >> "$KNOWN_HOSTS" 2>/dev/null || \
        echo "[entrypoint] WARNING: ssh-keyscan for ${KB_SSH_KEYSCAN_HOST} failed"
fi
chmod 0644 "$KNOWN_HOSTS"

# --- SSH key for git push/pull -----------------------------------------------
# In k8s the initContainer copies the deploy key to /state/git-key (0400, owned
# by the runtime user) so it is ready before we start. For a bare `docker compose`
# run the Secret is instead mounted read-only at /etc/git-key-mount; copy it to a
# user-writable path so ssh accepts the perms. Prefer the pre-staged key.
KEY_PATH="${KB_GIT_KEY_PATH:-/tmp/git-key}"
if [ -f /state/git-key ]; then
    KEY_PATH=/state/git-key
elif [ -f /etc/git-key-mount ]; then
    cp /etc/git-key-mount "$KEY_PATH"
    chmod 0400 "$KEY_PATH"
fi
if [ -f "$KEY_PATH" ]; then
    export GIT_SSH_COMMAND="ssh -i $KEY_PATH -o UserKnownHostsFile=$KNOWN_HOSTS -o StrictHostKeyChecking=yes"
fi

# --- Bootstrap /kb-main on first boot (docker compose path only) -------------
# In k8s the initContainer already cloned /kb-main. For a bare docker run there is
# no initContainer, so do it here. Idempotent: skipped when /kb-main is non-empty.
# Safe: only clones when KB_GIT_REMOTE_URL is set. On clone failure the volume
# stays empty and the MCP reports degraded so the operator notices.
if [ -n "${KB_GIT_REMOTE_URL:-}" ] && [ -d /kb-main ]; then
    found_real_files=$(find /kb-main -mindepth 1 -maxdepth 1 \
        ! -name 'lost+found' -print -quit 2>/dev/null || true)
    if [ -z "$found_real_files" ]; then
        echo "[entrypoint] /kb-main is empty; cloning ${KB_GIT_REMOTE_URL}"
        if git clone "$KB_GIT_REMOTE_URL" /kb-main; then
            echo "[entrypoint] bootstrap complete"
        else
            echo "[entrypoint] WARNING: clone failed; pod will start degraded"
        fi
    fi
fi

exec "$@"
