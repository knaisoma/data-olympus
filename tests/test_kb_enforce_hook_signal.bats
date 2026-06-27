#!/usr/bin/env bats
# action_diff is sent on pre-tool; gate_degraded is emitted on a reachable-but-500 gate.

setup_file() {
  REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  export REPO_ROOT
  export FIXTURE_DIR="${BATS_TEST_FILENAME%/*}/cli-fixtures"
  export HOOK="${REPO_ROOT}/bin/kb-enforce-hook"
}

setup() {
  PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
  export PORT
  export KB_ENDPOINT="http://127.0.0.1:${PORT}"
  export KB_CAPTURE="$(mktemp)"
  KB_MOCK_CAPTURE_FILE="$KB_CAPTURE" python3 "${FIXTURE_DIR}/enforce-mock-server.py" "$PORT" &
  MOCK_PID=$!
  export MOCK_PID
  for _ in $(seq 1 30); do
    if curl --silent --max-time 0.2 "http://127.0.0.1:${PORT}/api/v1/compliance" >/dev/null 2>&1; then break; fi
    sleep 0.1
  done
}

teardown() { kill "$MOCK_PID" 2>/dev/null || true; wait "$MOCK_PID" 2>/dev/null || true; rm -f "$KB_CAPTURE"; }

@test "pre-tool sends action_diff from tool_input" {
  run bash -c 'echo "{\"session_id\":\"s\",\"cwd\":\"/tmp/p\",\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"/tmp/p/x.py\",\"content\":\"import flask\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 0 ] || [ "$status" -eq 2 ]
  # the captured gate body must carry the content as action_diff
  grep -q 'import flask' "$KB_CAPTURE"
}

@test "gate_degraded recorded on reachable HTTP 500 (boom path)" {
  run bash -c 'echo "{\"session_id\":\"s\",\"cwd\":\"/tmp/p\",\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"/tmp/p/boom.toml\"}}" | "'"$HOOK"'" pre-tool'
  # reachable-but-500 -> fail open (exit 0) AND a gate_degraded event POSTed
  [ "$status" -eq 0 ]
  grep -q 'gate_degraded' "$KB_CAPTURE"
}

@test "unreachable gate does NOT record gate_degraded and does not hang" {
  # Point at a dead port (kill the mock first). post_json connection failure must
  # route through fail-open WITHOUT attempting to record (phone is down) and WITHOUT
  # hanging on the record_degraded POST.
  kill "$MOCK_PID" 2>/dev/null || true
  wait "$MOCK_PID" 2>/dev/null || true
  CAP2="$(mktemp)"
  bash -c 'echo "{\"session_id\":\"s\",\"cwd\":\"/tmp/p\",\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"/tmp/p/boom.toml\"}}" | KB_MOCK_CAPTURE_FILE='"$CAP2"' "'"$HOOK"'" pre-tool' &
  hook_pid=$!
  ( sleep 20; kill "$hook_pid" 2>/dev/null ) &
  watchdog_pid=$!
  wait "$hook_pid"
  hook_status=$?
  kill "$watchdog_pid" 2>/dev/null || true
  wait "$watchdog_pid" 2>/dev/null || true
  [ "$hook_status" -eq 0 ]
  # No gate_degraded must be recorded when the server is unreachable.
  ! grep -q 'gate_degraded' "$CAP2" 2>/dev/null
  rm -f "$CAP2"
}
