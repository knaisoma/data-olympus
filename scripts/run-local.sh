#!/usr/bin/env bash
set -euo pipefail
# Prepare a runnable KB: the server expects KB_MAIN_PATH to be a git repo.
KB_DIR="${1:-/tmp/data-olympus-demo-kb}"
DB="${2:-/tmp/data-olympus-demo.db}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf "$KB_DIR" "$DB"
cp -r "$HERE/example-bundle" "$KB_DIR"
git -C "$KB_DIR" init -q
git -C "$KB_DIR" add -A
git -C "$KB_DIR" -c user.email=demo@local -c user.name=demo commit -qm "init demo kb"
echo "Serving $KB_DIR on http://localhost:${KB_HTTP_PORT:-8080}"
KB_MAIN_PATH="$KB_DIR" KB_INDEX_PATH="$DB" KB_REMOTE_URL="" \
  KB_HTTP_PORT="${KB_HTTP_PORT:-8080}" \
  KB_PENDING_ROOT="/tmp/data-olympus-demo-pending" \
  KB_PUSH_QUEUE_ROOT="/tmp/data-olympus-demo-push-queue" \
  KB_WORKTREE_ROOT="/tmp/data-olympus-demo-worktrees" \
  KB_AUDIT_LOG_PATH="/tmp/data-olympus-demo-audit.log" \
  exec uv run data-olympus-mcp
