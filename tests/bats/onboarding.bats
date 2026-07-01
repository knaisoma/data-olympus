#!/usr/bin/env bats
# bats tests for bin/kb onboarding subcommands. Spins up a mock REST server
# on a free port per test. Mock server returns:
#   state="onboarded" for workspace=example-project
#   state="absent"    for any other workspace

setup_file() {
  # This file lives at tests/bats/, two levels below the repo root, so the root
  # is dir/../.. (the original `../../..` resolved outside the repo; see the
  # note in tests/test_kb_cli_write.bats).
  REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../.." && pwd)"
  export REPO_ROOT
  export FIXTURE_DIR="${BATS_TEST_FILENAME%/*}/../cli-fixtures"
  export KB="${REPO_ROOT}/bin/kb"
}

setup() {
  PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
  export PORT
  export KB_ENDPOINT="http://127.0.0.1:${PORT}"
  python3 "${FIXTURE_DIR}/mock-server.py" "$PORT" "$FIXTURE_DIR" &
  MOCK_PID=$!
  export MOCK_PID
  for _ in $(seq 1 30); do
    if curl --silent --max-time 0.2 "http://127.0.0.1:${PORT}/api/v1/health" >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done
}

teardown() {
  kill "$MOCK_PID" 2>/dev/null || true
  wait "$MOCK_PID" 2>/dev/null || true
}

@test "kb onboarding-check is silent when workspace is onboarded" {
  run "$KB" onboarding-check --workspace example-project
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "kb onboard status returns json with state field" {
  run "$KB" onboard status example-project
  [ "$status" -eq 0 ]
  [[ "$output" == *'"state"'* ]]
  [[ "$output" == *'"onboarded"'* ]]
}

@test "kb onboard playbook prints project script" {
  run "$KB" onboard playbook --kind project --workspace foo
  [ "$status" -eq 0 ]
  [[ "$output" == *"Onboarding project: foo"* ]]
}
