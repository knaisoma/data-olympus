#!/usr/bin/env bats
setup_file() {
  REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  export REPO_ROOT
  export KB="${REPO_ROOT}/bin/kb"
}

@test "kb enforce report prints a friendly hint when data-olympus is not on PATH" {
  # Run with a PATH that lacks data-olympus
  run env PATH="/usr/bin:/bin" "$KB" enforce report --json
  [ "$status" -eq 127 ]
  [[ "$output" == *"data-olympus not found on PATH"* ]]
}
