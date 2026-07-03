#!/usr/bin/env bash
set -euo pipefail
# This script is for the demo flow ONLY: it copies example-bundle/ into a
# throwaway directory, git-inits it, and serves it. It is destructive by
# design (it wipes and recreates KB_DIR on every run), so it must never
# delete a directory it did not create itself. To serve your own bundle,
# do not pass it as $1 here: invoke data-olympus-mcp directly with
# KB_MAIN_PATH pointed at your bundle (see docs/quickstart.md section 6 /
# docs/adoption.md section 5).
DEFAULT_KB_DIR="/tmp/data-olympus-demo-kb"
KB_DIR="${1:-$DEFAULT_KB_DIR}"
DB="${2:-/tmp/data-olympus-demo.db}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
MARKER=".data-olympus-demo-kb"

if [ -e "$KB_DIR" ] && [ "$KB_DIR" != "$DEFAULT_KB_DIR" ] && [ ! -e "$KB_DIR/$MARKER" ]; then
  echo "error: refusing to delete '$KB_DIR': it already exists and was not" >&2
  echo "created by this script (no $MARKER marker found)." >&2
  echo >&2
  echo "This script always wipes and recreates its target directory, so it" >&2
  echo "will not touch a path that looks like your own data." >&2
  echo >&2
  echo "To serve your own bundle, run the MCP server directly instead:" >&2
  echo >&2
  echo "  KB_MAIN_PATH=$KB_DIR \\" >&2
  echo "    KB_INDEX_PATH=/tmp/your-kb.db \\" >&2
  echo "    KB_REMOTE_URL=\"\" \\" >&2
  echo "    uv run data-olympus-mcp" >&2
  echo >&2
  echo "See docs/quickstart.md section 6 for the full invocation." >&2
  exit 1
fi

rm -rf "$KB_DIR" "$DB"
cp -r "$HERE/example-bundle" "$KB_DIR"
touch "$KB_DIR/$MARKER"
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
