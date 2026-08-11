#!/bin/sh
# scripts/gates.sh - project-owned quality-gates entry point.
#
# Sets up the CI-equivalent environment inside a target directory and then
# runs the four Data Olympus release gates, in CI order, stopping at the
# first failure:
#
#   1. uv run ruff check .
#   2. uv run mypy src
#   3. uv run pytest -q
#   4. bats -r tests
#
# See .rules/release-routine.md for the authoritative runbook this script
# implements.
#
# Usage:
#   bash scripts/gates.sh <target-directory>
#
# The target directory is a REQUIRED first argument, never an environment
# variable and never a cwd default. That is deliberate: it lets a single
# permission allow-rule (e.g. "bash */scripts/gates.sh *") match the exact
# invocation, which a "cd <dir> && ..." chain can never do because the
# command does not start with the allow-listed prefix.
#
# Do not pipe a gate's output through another command (e.g. `| tail`)
# before checking its exit status: that reports the pipeline's last
# command's exit status, not the gate's, and silently turns a failing gate
# into a "passed" run. Every gate below is run directly and its exit
# status is checked directly, with output captured to a file for bounded,
# non-lossy reporting.

set -eu

usage() {
  echo "Usage: $0 <target-directory>" >&2
}

if [ "$#" -lt 1 ]; then
  echo "ERROR: missing required argument <target-directory>." >&2
  usage
  exit 1
fi

target_dir="$1"

if [ ! -d "$target_dir" ]; then
  echo "ERROR: '$target_dir' is not a directory." >&2
  exit 1
fi

if [ ! -f "$target_dir/pyproject.toml" ]; then
  echo "ERROR: '$target_dir' does not contain a pyproject.toml; refusing to run quality gates outside a Python project root." >&2
  exit 1
fi

log_dir="$(mktemp -d "${TMPDIR:-/tmp}/data-olympus-gates.XXXXXX")"
trap 'rm -rf "$log_dir"' EXIT INT TERM HUP

banner() {
  printf '\n=== %s ===\n' "$1"
}

# run_step NAME LOG_FILE CMD...
#
# Runs CMD... with cwd set to "$target_dir", capturing combined output to
# LOG_FILE. On success, prints a bounded tail of the output (enough to
# read in a run log). On failure, prints the FULL captured output (never
# truncate a failing gate's output so far that the cause is lost), then
# returns the command's real exit status.
run_step() {
  step_name="$1"
  step_log="$2"
  shift 2
  banner "$step_name"
  if ( cd "$target_dir" && "$@" ) >"$step_log" 2>&1; then
    tail -n 40 "$step_log"
    return 0
  else
    # Capture the real exit status of the failing command as the FIRST
    # statement of the else branch. A bare "$?" read after "fi" (with no
    # else) is unreliable across shells: POSIX defines the exit status of
    # an "if" with no executed branch as 0, not the condition's status.
    step_status=$?
    cat "$step_log"
    printf -- '--- %s FAILED (exit %s) ---\n' "$step_name" "$step_status" >&2
    return "$step_status"
  fi
}

fail() {
  printf '\nSUMMARY: %s\n' "$1" >&2
  exit 1
}

# 1. Mandatory CI-equivalent environment setup. Not skippable: a bare
#    `uv run pytest` without the editable install fails
#    tests/test_server_cli.py and tests/test_smoke.py with
#    "No module named data_olympus.server" and a stale version assertion,
#    even on a commit where CI is green, because those tests spawn a
#    subprocess via sys.executable and read installed distribution
#    metadata. See .rules/release-routine.md.
run_step "ENV SETUP: uv venv --python 3.13" "$log_dir/00-venv.log" \
  uv venv --python 3.13 \
  || fail "environment setup failed (uv venv --python 3.13)"

run_step "ENV SETUP: uv pip install -e '.[dev]'" "$log_dir/01-pip-install.log" \
  uv pip install -e '.[dev]' \
  || fail "environment setup failed (uv pip install -e '.[dev]')"

# 2. The four gates, in CI order. Lint runs first because it runs first
#    in CI, so a lint failure does not hide every test result behind it.
run_step "GATE 1/4: uv run ruff check ." "$log_dir/10-ruff.log" \
  uv run ruff check . \
  || fail "gate FAILED: ruff (uv run ruff check .)"

run_step "GATE 2/4: uv run mypy src" "$log_dir/11-mypy.log" \
  uv run mypy src \
  || fail "gate FAILED: mypy (uv run mypy src)"

run_step "GATE 3/4: uv run pytest -q" "$log_dir/12-pytest.log" \
  uv run pytest -q \
  || fail "gate FAILED: pytest (uv run pytest -q)"

run_step "GATE 4/4: bats -r tests" "$log_dir/13-bats.log" \
  bats -r tests \
  || fail "gate FAILED: bats (bats -r tests)"

printf '\nSUMMARY: all four gates passed (ruff, mypy, pytest, bats).\n'
