#!/usr/bin/env bats
# bats tests for bin/kb-enforce-hook against a mock REST server.

setup_file() {
  # This test file lives directly under tests/, so the repo root is one level up.
  # (test_kb_cli.bats uses ../../.. for a different historical layout; do not copy
  # that here -- it would resolve to .claude/bin and miss bin/kb-enforce-hook.)
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

teardown() {
  kill "$MOCK_PID" 2>/dev/null || true
  wait "$MOCK_PID" 2>/dev/null || true
}

@test "user-prompt mode prints injected rules and exits 0" {
  run bash -c 'echo "{\"session_id\":\"s1\",\"cwd\":\"/tmp/proj\",\"prompt\":\"add a library\"}" | "'"$HOOK"'" user-prompt'
  [ "$status" -eq 0 ]
  [[ "$output" == *"GOVERNING RULES"* ]]
  # Assert a real rule was injected, not just the header (catches the
  # f-string SyntaxError that silently printed no rules).
  [[ "$output" == *"STD-U-002"* ]]
}

@test "pre-tool mode blocks (exit 2) when consult_required" {
  run bash -c 'echo "{\"session_id\":\"blockme\",\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/pyproject.toml\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 2 ]
  [[ "$output" == *"consult"* ]]
}

@test "pre-tool mode allows (exit 0) when verdict allow" {
  run bash -c 'echo "{\"session_id\":\"allowme\",\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/README.md\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 0 ]
}

@test "pre-tool fails open (exit 0) when endpoint unreachable" {
  KB_ENDPOINT="http://127.0.0.1:1" run bash -c 'echo "{\"session_id\":\"x\",\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/pyproject.toml\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 0 ]
  [[ "$output" == *"warn"* ]] || [[ "$output" == *"unreachable"* ]]
}

@test "pre-tool fails closed (exit 2) when unreachable and KB_ENFORCE_FAIL_MODE=closed" {
  KB_ENDPOINT="http://127.0.0.1:1" KB_ENFORCE_FAIL_MODE=closed run bash -c 'echo "{\"session_id\":\"x\",\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/pyproject.toml\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 2 ]
}

@test "pre-tool fails open (exit 0) on HTTP 500 with default fail-mode" {
  # action_path contains "boom" -> mock returns HTTP 500 (with verdict:allow body).
  # A status-blind hook would parse verdict=allow and exit 0 for the WRONG reason;
  # this asserts the degraded warning is emitted (the right reason).
  run bash -c 'echo "{\"session_id\":\"x\",\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/boom.toml\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 0 ]
  [[ "$output" == *"warn"* ]] || [[ "$output" == *"unavailable"* ]]
}

@test "pre-tool fails closed (exit 2) on HTTP 500 with KB_ENFORCE_FAIL_MODE=closed" {
  # The critical regression: a 500 must NOT be treated as allow. With fail-mode
  # closed it must block, proving the hook honors HTTP status, not just the body.
  KB_ENFORCE_FAIL_MODE=closed run bash -c 'echo "{\"session_id\":\"x\",\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/boom.toml\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 2 ]
}
