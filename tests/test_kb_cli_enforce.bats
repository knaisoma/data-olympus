#!/usr/bin/env bats
# bats tests for `kb enforce` subcommand.

setup_file() {
  REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  export REPO_ROOT
  export KB="${REPO_ROOT}/bin/kb"
  if [[ ! -x "$KB" ]]; then
    echo "kb CLI not found or not executable at: $KB" >&2
    return 1
  fi
}

@test "kb enforce status on empty settings reports not installed" {
  TMP="$(mktemp -d)"
  echo "{}" > "$TMP/settings.json"
  run "$KB" enforce status --settings "$TMP/settings.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"not installed"* ]]
  rm -rf "$TMP"
}

@test "kb enforce install then status reports installed" {
  TMP="$(mktemp -d)"
  echo "{}" > "$TMP/settings.json"
  run "$KB" enforce install --settings "$TMP/settings.json"
  [ "$status" -eq 0 ]
  run "$KB" enforce status --settings "$TMP/settings.json"
  [[ "$output" == *"installed"* ]]
  rm -rf "$TMP"
}

@test "kb enforce with no subcommand is a usage error" {
  run "$KB" enforce
  [ "$status" -eq 64 ]
}
