#!/usr/bin/env bats
# bats tests for the write subcommands of bin/kb. Spins up the
# same mock REST server used by test_kb_cli.bats but exercises the POST
# routes (propose/resolve) and the new GET routes (pending/audit).

setup_file() {
  # tests/ sits ONE level below the repo root, so the root is the parent of the
  # bats file's directory.
  REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  export REPO_ROOT
  export FIXTURE_DIR="${BATS_TEST_FILENAME%/*}/cli-fixtures"
  export KB="${REPO_ROOT}/bin/kb"
}

setup() {
  # Bind-to-0 + readback to avoid port collisions in CI / parallel test runs.
  PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
  export PORT
  export KB_ENDPOINT="http://127.0.0.1:${PORT}"
  python3 "${FIXTURE_DIR}/mock-server.py" "$PORT" "$FIXTURE_DIR" &
  MOCK_PID=$!
  export MOCK_PID
  # Wait for server to accept connections (up to ~3s).
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

@test "kb propose memory non-interactive prints pending id when confidence low" {
  run "$KB" propose memory "test body" --confidence 0.4 --non-interactive
  [ "$status" -eq 0 ]
  [[ "$output" == *"Pending"* ]]
  [[ "$output" == *"pending-xyz"* ]]
}

@test "kb propose memory high-confidence prints committed sha" {
  run "$KB" propose memory "test body" --confidence 0.95 --non-interactive
  [ "$status" -eq 0 ]
  [[ "$output" == *"Committed"* ]]
  [[ "$output" == *"abc1234"* ]]
}

@test "kb propose edit reads from file" {
  tmpfile=$(mktemp -t kb-bats-edit.XXXXXX)
  echo "edited content" > "$tmpfile"
  run "$KB" propose edit "universal/foundation/STD-U-001.md" \
    --postimage-file "$tmpfile" --confidence 0.95 --non-interactive
  rm -f "$tmpfile"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Committed"* ]]
}

@test "kb propose edit missing postimage-file errors" {
  run "$KB" propose edit "universal/foundation/STD-U-001.md" --non-interactive
  [ "$status" -eq 64 ]
}

@test "kb propose edit sends base_commit (strict mock rejects absent OR wrong value)" {
  # The mock 400s with rejected_missing_base_commit when base_commit is absent,
  # and rejected_wrong_base_commit when it != the health commit "abc1234". A
  # 'Committed' result therefore proves the CLI fetched /api/v1/health and sent
  # that exact commit as base_commit (guards both presence AND value).
  tmpfile=$(mktemp -t kb-bats-edit.XXXXXX)
  echo "edited content" > "$tmpfile"
  run "$KB" propose edit "universal/foundation/STD-U-001.md" \
    --postimage-file "$tmpfile" --confidence 0.95 --non-interactive
  rm -f "$tmpfile"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Committed"* ]]
  [[ "$output" != *"rejected_missing_base_commit"* ]]
}

@test "kb propose edit surfaces a non-JSON server error cleanly (no jq crash)" {
  # Sentinel target makes the mock return a plain-text HTTP 500. The CLI must
  # print a clean 'server error' diagnostic and exit non-zero, NOT abort by
  # piping the non-JSON body into jq (the original 'jq: parse error' failure).
  tmpfile=$(mktemp -t kb-bats-edit.XXXXXX)
  echo "edited content" > "$tmpfile"
  run "$KB" propose edit "trigger/server-error.md" \
    --postimage-file "$tmpfile" --confidence 0.95 --non-interactive
  rm -f "$tmpfile"
  [ "$status" -ne 0 ]
  [[ "$output" == *"server error"* ]]
  [[ "$output" != *"parse error"* ]]
}

@test "kb resolve approve" {
  run "$KB" resolve "pending-test-id" --decision approve
  [ "$status" -eq 0 ]
  [[ "$output" == *"committed"* ]]
}

@test "kb resolve reject" {
  run "$KB" resolve "pending-test-id" --decision reject
  [ "$status" -eq 0 ]
  [[ "$output" == *"rejected"* ]]
}

@test "kb resolve missing decision errors" {
  run "$KB" resolve "pending-test-id"
  [ "$status" -eq 64 ]
}

@test "kb resolve --override-secret-scan sends the flag through to REST" {
  run "$KB" resolve "pending-test-id" --decision approve --override-secret-scan
  [ "$status" -eq 0 ]
  [[ "$output" == *"committed"* ]]
  [[ "$output" == *"secret_scan_override_seen"* ]]
}

@test "kb resolve approve without --override-secret-scan omits the override" {
  run "$KB" resolve "pending-test-id" --decision approve
  [ "$status" -eq 0 ]
  [[ "$output" == *"committed"* ]]
  [[ "$output" != *"secret_scan_override_seen"* ]]
}

@test "kb resolve --edit-text with --override-secret-scan sends both" {
  run "$KB" resolve "pending-test-id" --decision approve --edit-text "fixed body" \
    --override-secret-scan
  [ "$status" -eq 0 ]
  [[ "$output" == *"committed"* ]]
  [[ "$output" == *"secret_scan_override_seen"* ]]
}

@test "kb pending lists entries" {
  run "$KB" pending
  [ "$status" -eq 0 ]
  [[ "$output" == *"pending_id"* ]] || [[ "$output" == *"memory/inbox"* ]]
}

@test "kb pending plain output is tab-delimited" {
  run "$KB" pending -o plain
  [ "$status" -eq 0 ]
  [[ "$output" == *"p1"* ]]
  [[ "$output" == *"memory"* ]]
}

@test "kb audit lists events" {
  run "$KB" audit
  [ "$status" -eq 0 ]
  [[ "$output" == *"propose_memory"* ]] || [[ "$output" == *"committed"* ]]
}

@test "kb audit plain output includes event_type" {
  run "$KB" audit -o plain
  [ "$status" -eq 0 ]
  [[ "$output" == *"propose_memory"* ]]
  [[ "$output" == *"committed"* ]]
}

@test "kb session-recap prints the per-session tally" {
  run "$KB" session-recap my-session
  [ "$status" -eq 0 ]
  [[ "$output" == *"my-session"* ]]
  [[ "$output" == *"committed"* ]]
  [[ "$output" == *"demoted_to_pending"* ]]
}

@test "kb session-recap plain output is a one-line summary" {
  run "$KB" session-recap my-session -o plain
  [ "$status" -eq 0 ]
  [[ "$output" == *"committed: 2"* ]]
  [[ "$output" == *"demoted_to_pending: 1"* ]]
  [[ "$output" == *"rejected: 0"* ]]
}

@test "kb session-recap without SOURCE_SESSION returns usage error" {
  run "$KB" session-recap
  [ "$status" -eq 64 ]
}

@test "kb propose with no args returns usage error" {
  run "$KB" propose
  [ "$status" -eq 64 ]
}

@test "kb propose memory missing text returns usage error" {
  run "$KB" propose memory --confidence 0.9 --non-interactive
  [ "$status" -eq 64 ]
}
