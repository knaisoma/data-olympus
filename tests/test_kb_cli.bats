#!/usr/bin/env bats
# bats tests for bin/kb. Spins up a mock REST server on a free port per test.

setup_file() {
  # tests/ sits ONE level below the repo root in this repo (see note in
  # test_kb_cli_write.bats); the original `../../..` resolved to $HOME here.
  REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  export REPO_ROOT
  export FIXTURE_DIR="${BATS_TEST_FILENAME%/*}/cli-fixtures"
  export KB="${REPO_ROOT}/bin/kb"
}

setup() {
  # Bind-to-0 + readback to avoid port collisions in CI / parallel test runs.
  # Also wait for the mock to actually accept connections before returning.
  PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
  export PORT
  export KB_ENDPOINT="http://127.0.0.1:${PORT}"
  python3 "${FIXTURE_DIR}/mock-server.py" "$PORT" "$FIXTURE_DIR" &
  MOCK_PID=$!
  export MOCK_PID
  # Wait for server to accept connections (up to ~3s)
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

@test "kb health returns JSON with kb_commit" {
  run "$KB" health
  [ "$status" -eq 0 ]
  [[ "$output" == *'"kb_commit"'* ]]
  [[ "$output" == *'"degraded"'* ]]
}

@test "kb get STD-U-007 -o md prints raw markdown" {
  run "$KB" get STD-U-007 -o md
  [ "$status" -eq 0 ]
  [[ "$output" == *"PROBLEM / WHY / BETTER APPROACH / BENEFITS"* ]]
  [[ "$output" != *'"content_markdown"'* ]]  # md mode, not json
}

@test "kb get STD-U-007 default format is JSON with id field" {
  run "$KB" get STD-U-007
  [ "$status" -eq 0 ]
  [[ "$output" == *'"id"'* ]]
  [[ "$output" == *'"STD-U-007"'* ]]
}

@test "kb get STD-MISSING returns 404 to stderr" {
  run "$KB" get STD-MISSING
  [ "$status" -eq 1 ]
  [[ "$output" == *'"not_found"'* ]]
}

@test "kb list T1 foundation -o plain prints one line per entry" {
  run "$KB" list T1 foundation -o plain
  [ "$status" -eq 0 ]
  # Two entries expected
  line_count=$(echo "$output" | grep -c "STD-U-")
  [ "$line_count" -eq 2 ]
}

@test "kb list with missing tier returns usage error" {
  run "$KB" list
  [ "$status" -eq 64 ]
}

@test "kb search worktree -o plain prints id<TAB>path" {
  run "$KB" search worktree -o plain
  [ "$status" -eq 0 ]
  [[ "$output" == *"STD-U-505"* ]]
}

@test "--no-stale exit 1 when endpoint unreachable" {
  kill "$MOCK_PID"
  wait "$MOCK_PID" 2>/dev/null || true
  unset MOCK_PID
  run "$KB" health --no-stale
  [ "$status" -eq 1 ]
}

@test "default mode falls back to local grep when endpoint unreachable" {
  kill "$MOCK_PID"
  wait "$MOCK_PID" 2>/dev/null || true
  unset MOCK_PID
  export KB_LOCAL_PATH="$REPO_ROOT"
  run "$KB" health
  [ "$status" -eq 0 ]
  [[ "$output" == *'"degraded": true'* ]]
}

@test "KB_NO_STALE env var equivalent to --no-stale" {
  kill "$MOCK_PID"
  wait "$MOCK_PID" 2>/dev/null || true
  unset MOCK_PID
  KB_NO_STALE=1 run "$KB" health
  [ "$status" -eq 1 ]
}

@test "--no-stale exit 2 when REST returns HTTP 200 with degraded:true body" {
  # Spawn an alternate mock that ALWAYS returns 200 + degraded:true on /api/v1/health,
  # regardless of query params. This lets us test the body-inspection path distinctly
  # from the 503-status path.
  ALT_PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
  python3 -c "
import http.server, socketserver, json
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            'kb_commit':'abc','index_built_at':1.0,'total_rules':1,
            'last_git_pull_at':1.0,'last_git_push_at':None,
            'staleness_seconds':1.0,'degraded':True,'db_size_bytes':0,
            'pending_count':0,'push_queue_size':0,
            'last_index_build_status':'ok','last_index_error':None,
            'last_index_error_at':None,'last_index_conflicts':[]
        }).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*a,**k): pass
with socketserver.TCPServer(('127.0.0.1', $ALT_PORT), H) as s:
    s.serve_forever()
" &
  ALT_PID=$!
  # Readiness probe instead of fixed sleep
  for _ in $(seq 1 30); do
    if curl --silent --max-time 0.2 "http://127.0.0.1:${ALT_PORT}/api/v1/health" >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done
  export KB_ENDPOINT="http://127.0.0.1:${ALT_PORT}"
  run "$KB" health --no-stale
  kill "$ALT_PID" 2>/dev/null || true
  wait "$ALT_PID" 2>/dev/null || true
  [ "$status" -eq 2 ]
}

@test "--no-stale exit 2 when REST returns 503 with degraded:true body" {
  # Use the mode=503 mock variant. Override KB_ENDPOINT to a path that returns 503 by appending the query.
  # Since bin/kb appends paths to KB_ENDPOINT, set KB_ENDPOINT to include the route + mode param as-is.
  # The mock honors ?mode=503 on /api/v1/health regardless. The simplest exercise: assert that 503 + degraded:true
  # triggers exit 2.
  # We accomplish this by setting KB_ENDPOINT to a base URL whose /api/v1/health path on the mock will
  # be reached with mode=503 query. Easiest: rewrite the URL in the mock to always serve 503 here.
  # Implementation choice: extend mock-server to accept an env that overrides /api/v1/health behavior.
  # For now, we cover this via a Python in-test mock override.
  ALT2_PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
  python3 -c "
import http.server, socketserver, threading, json
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'degraded': True, 'reason': 'index rebuilding'}).encode()
        self.send_response(503)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a, **k): pass
import sys
with socketserver.TCPServer(('127.0.0.1', $ALT2_PORT), H) as s:
    s.serve_forever()
" &
  ALT_PID=$!
  # Readiness probe (replaces sleep 0.3)
  for _ in $(seq 1 30); do
    if curl --silent --max-time 0.2 "http://127.0.0.1:${ALT2_PORT}/api/v1/health" >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done
  export KB_ENDPOINT="http://127.0.0.1:${ALT2_PORT}"
  run "$KB" health --no-stale
  kill "$ALT_PID" 2>/dev/null || true
  wait "$ALT_PID" 2>/dev/null || true
  [ "$status" -eq 2 ]
}
