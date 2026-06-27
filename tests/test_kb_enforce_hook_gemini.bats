#!/usr/bin/env bats
# Gemini dialect output contract for bin/kb-enforce-hook.

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
  python3 "${FIXTURE_DIR}/enforce-mock-server.py" "$PORT" &
  MOCK_PID=$!
  export MOCK_PID
  for _ in $(seq 1 30); do
    if curl --silent --max-time 0.2 "http://127.0.0.1:${PORT}/api/v1/compliance" >/dev/null 2>&1; then break; fi
    sleep 0.1
  done
}

teardown() { kill "$MOCK_PID" 2>/dev/null || true; wait "$MOCK_PID" 2>/dev/null || true; }

@test "gemini pre-tool deny is JSON on stdout with exit 0 (not exit 2)" {
  run bash -c 'echo "{\"session_id\":\"blockme\",\"cwd\":\"/tmp/p\",\"tool_name\":\"write_file\",\"tool_input\":{\"file_path\":\"/tmp/p/pyproject.toml\"}}" | "'"$HOOK"'" pre-tool --dialect gemini'
  [ "$status" -eq 0 ]
  [[ "$output" == *'"decision"'* ]]
  [[ "$output" == *'"deny"'* ]]
}

@test "gemini pre-tool allow emits no deny" {
  run bash -c 'echo "{\"session_id\":\"allowme\",\"cwd\":\"/tmp/p\",\"tool_name\":\"write_file\",\"tool_input\":{\"file_path\":\"/tmp/p/README.md\"}}" | "'"$HOOK"'" pre-tool --dialect gemini'
  [ "$status" -eq 0 ]
  [[ "$output" != *'"deny"'* ]]
}

@test "gemini user-prompt emits additionalContext JSON" {
  run bash -c 'echo "{\"session_id\":\"s1\",\"cwd\":\"/tmp/p\",\"prompt\":\"add a library\"}" | "'"$HOOK"'" user-prompt --dialect gemini'
  [ "$status" -eq 0 ]
  [[ "$output" == *'"additionalContext"'* ]]
  [[ "$output" == *'STD-U-002'* ]]
}

@test "gemini pre-tool fail-closed on HTTP 500 still blocks via JSON deny" {
  run bash -c 'echo "{\"session_id\":\"x\",\"cwd\":\"/tmp/p\",\"tool_name\":\"write_file\",\"tool_input\":{\"file_path\":\"/tmp/p/boom.toml\"}}" | KB_ENFORCE_FAIL_MODE=closed "'"$HOOK"'" pre-tool --dialect gemini'
  [ "$status" -eq 0 ]
  [[ "$output" == *'"deny"'* ]]
}
