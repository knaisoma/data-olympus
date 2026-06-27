#!/usr/bin/env bats
# Verifies `kb enforce report` delegates to the package CLI `data-olympus report`
# (Task 4 of the enforcement slice-3 plan) rather than falling through to
# bin/_kb_enforce.py (which only knows install|uninstall|status|doctor).
#
# NOTE on the help assertion: the plan drafted an assertion against the string
# "governed changes lacking a consultation". That string is the *parent*
# subparser help (`data-olympus --help`), not part of `data-olympus report
# --help`. Since this task must not modify any Python, we instead assert on
# `--fail-on-unverified`, an option unique to `data-olympus report`'s own help,
# which proves the route reached the package CLI's report subcommand exactly.
#
# NOTE on PATH: production bin/kb must `exec data-olympus` (a bare command, no
# `uv`). In a bare `bats` run the package console-script lives at
# .venv/bin/data-olympus and is not on PATH, so this test prepends that dir to
# PATH. This keeps production bin/kb free of any uv/venv dependency.

setup_file() {
  REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  export REPO_ROOT
  export KB="${REPO_ROOT}/bin/kb"
  if [ -x "${REPO_ROOT}/.venv/bin/data-olympus" ]; then
    PATH="${REPO_ROOT}/.venv/bin:${PATH}"
    export PATH
  fi
}

@test "kb enforce report delegates to data-olympus report (--help reaches it)" {
  run "$KB" enforce report --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"--fail-on-unverified"* ]]
}

@test "kb enforce with no subcommand errors with usage" {
  run "$KB" enforce
  [ "$status" -eq 64 ]
  [[ "$output" == *"report"* ]]
}
