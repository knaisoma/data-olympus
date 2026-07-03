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

@test "user-prompt --agent codex threads agent_identity=codex to the consult body" {
  # Regression guard for the audit-attribution bug: the hardcoded "claude-code"
  # in the consult body meant every agent was recorded as claude-code. We capture
  # the raw consult request body via the mock's KB_MOCK_CAPTURE_FILE hook and
  # assert the threaded --agent value reaches agent_identity.
  CAP="${BATS_TEST_TMPDIR}/consult-body.json"
  # Restart the mock with capture enabled (setup()'s instance has no capture file).
  kill "$MOCK_PID" 2>/dev/null || true
  wait "$MOCK_PID" 2>/dev/null || true
  KB_MOCK_CAPTURE_FILE="$CAP" python3 "${FIXTURE_DIR}/enforce-mock-server.py" "$PORT" &
  MOCK_PID=$!
  for _ in $(seq 1 30); do
    if curl --silent --max-time 0.2 "http://127.0.0.1:${PORT}/api/v1/compliance" >/dev/null 2>&1; then break; fi
    sleep 0.1
  done

  run bash -c 'echo "{\"session_id\":\"s1\",\"cwd\":\"/tmp/proj\",\"prompt\":\"add a library\"}" | "'"$HOOK"'" user-prompt --agent codex'
  [ "$status" -eq 0 ]
  [ -f "$CAP" ]
  run python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["agent_identity"])' "$CAP"
  [ "$output" = "codex" ]
}

@test "user-prompt with a trailing --agent (no value) does not hang and exits 0" {
  # set -u + a bad `shift 2` on a trailing flag would hang/error; the guarded
  # `shift; shift || true` must tolerate a value-less trailing --agent. We bound
  # the run with a portable background watchdog (macOS has no coreutils timeout).
  bash -c 'echo "{\"session_id\":\"s1\",\"cwd\":\"/tmp/proj\",\"prompt\":\"add a library\"}" | "'"$HOOK"'" user-prompt --agent' &
  hook_pid=$!
  ( sleep 10; kill "$hook_pid" 2>/dev/null ) &
  watchdog_pid=$!
  wait "$hook_pid"
  hook_status=$?
  kill "$watchdog_pid" 2>/dev/null || true
  wait "$watchdog_pid" 2>/dev/null || true
  [ "$hook_status" -eq 0 ]
}

@test "pre-tool mode blocks (exit 2) when consult_required" {
  run bash -c 'echo "{\"session_id\":\"blockme\",\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/pyproject.toml\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 2 ]
  [[ "$output" == *"consult"* ]]
}

@test "pre-tool deny message is actionable: session id + workspace + copy-pasteable kb_consult" {
  # The deny must echo the session id (the one param the agent cannot guess) and
  # the workspace key inside a copy-pasteable kb_consult(...) call.
  run bash -c 'echo "{\"session_id\":\"blockme\",\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/pyproject.toml\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 2 ]
  [[ "$output" == *"kb_consult(workspace="* ]]
  [[ "$output" == *"source_session='blockme'"* ]]
}

@test "pre-tool with a null session_id emits empty (never the literal None)" {
  # json_field must render JSON null as "" so the deny/consult key is empty, not
  # the literal string "None".
  run bash -c 'echo "{\"session_id\":null,\"cwd\":\"/tmp/proj\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"/tmp/proj/pyproject.toml\"}}" | "'"$HOOK"'" pre-tool'
  [ "$status" -eq 2 ]
  [[ "$output" != *"None"* ]]
  [[ "$output" == *"source_session=''"* ]]
}

@test "user-prompt consult body carries trigger=prompt_hook" {
  # The installer-driven auto-consult must be marked prompt_hook so it is audited
  # but never clears the gate.
  CAP="${BATS_TEST_TMPDIR}/consult-body.json"
  kill "$MOCK_PID" 2>/dev/null || true
  wait "$MOCK_PID" 2>/dev/null || true
  KB_MOCK_CAPTURE_FILE="$CAP" python3 "${FIXTURE_DIR}/enforce-mock-server.py" "$PORT" &
  MOCK_PID=$!
  for _ in $(seq 1 30); do
    if curl --silent --max-time 0.2 "http://127.0.0.1:${PORT}/api/v1/compliance" >/dev/null 2>&1; then break; fi
    sleep 0.1
  done

  run bash -c 'echo "{\"session_id\":\"s1\",\"cwd\":\"/tmp/proj\",\"prompt\":\"add a library\"}" | "'"$HOOK"'" user-prompt'
  [ "$status" -eq 0 ]
  [ -f "$CAP" ]
  run python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["trigger"])' "$CAP"
  [ "$output" = "prompt_hook" ]
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

@test "resolve-workspace yields the SAME label from the main checkout and a linked worktree" {
  # Regression: the workspace key must be worktree-invariant so one consult
  # clears both the pre-tool and pre-commit gates. Detector is bypassed here
  # (repo is not under KB_WORKSPACES_ROOT), exercising the git worktree path.
  local root="${BATS_TEST_TMPDIR}/wsroot"
  mkdir -p "$root/mainrepo"
  git -C "$root/mainrepo" init -q --initial-branch=main
  git -C "$root/mainrepo" -c user.email=t@e.com -c user.name=t commit -q --allow-empty -m init
  git -C "$root/mainrepo" worktree add -q -b wt "$root/linked"

  run "$HOOK" resolve-workspace "$root/mainrepo"
  [ "$status" -eq 0 ]
  [ "$output" = "mainrepo" ]

  run "$HOOK" resolve-workspace "$root/linked"
  [ "$status" -eq 0 ]
  [ "$output" = "mainrepo" ]
}

@test "resolve-workspace prefers the git main worktree even when KB_WORKSPACES_ROOT resolves the linked worktree" {
  # Blocker regression: git resolution must run BEFORE the detector. With
  # KB_WORKSPACES_ROOT pointed at the linked worktree's parent, the detector
  # would return the worktree basename (linked2), disagreeing with `report`'s
  # git-based default. Git-first keeps both gates on the main repo key.
  local root="${BATS_TEST_TMPDIR}/wsroot2"
  mkdir -p "$root/mainrepo" "$root/wts"
  git -C "$root/mainrepo" init -q --initial-branch=main
  git -C "$root/mainrepo" -c user.email=t@e.com -c user.name=t commit -q --allow-empty -m init
  git -C "$root/mainrepo" worktree add -q -b wt2 "$root/wts/linked2"

  run env KB_WORKSPACES_ROOT="$root/wts" "$HOOK" resolve-workspace "$root/wts/linked2"
  [ "$status" -eq 0 ]
  [ "$output" = "mainrepo" ]
}

@test "resolve-workspace skips the bare record for a worktree attached to a bare repo" {
  # A bare repo's porcelain lists the bare git dir first (marked `bare`); the key
  # must be the real checkout basename (work), not the bare repo name (origin.git).
  local root="${BATS_TEST_TMPDIR}/bareroot"
  mkdir -p "$root"
  git -C "$root" init -q --initial-branch=main src
  git -C "$root/src" -c user.email=t@e.com -c user.name=t commit -q --allow-empty -m init
  git -C "$root" clone -q --bare src origin.git
  git -C "$root/origin.git" worktree add -q "$root/work" main

  run "$HOOK" resolve-workspace "$root/work"
  [ "$status" -eq 0 ]
  [ "$output" = "work" ]
}

@test "resolve-workspace does not hang without stdin and falls back outside git" {
  # No stdin redirect: the mode must not block on cat. A non-git dir falls back
  # to the raw path (never empty), so the gate always has a concrete key.
  run "$HOOK" resolve-workspace "${BATS_TEST_TMPDIR}"
  [ "$status" -eq 0 ]
  [ "$output" = "${BATS_TEST_TMPDIR}" ]
}
