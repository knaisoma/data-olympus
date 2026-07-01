# tests/bats/compute-version.bats
setup() {
  REPO="$(mktemp -d)"
  SCRIPT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/compute-version.sh"
  cd "$REPO"
  git init -q
  git config user.email t@t.t
  git config user.name t
  git commit -q --allow-empty -m "feat: initial"
}

teardown() { rm -rf "$REPO"; }

@test "exact tag emits clean X.Y.Z" {
  git tag -a v0.1.0 -m "Release 0.1.0"
  run "$SCRIPT" version
  [ "$status" -eq 0 ]
  [ "$output" = "0.1.0" ]
}

@test "commit past tag embeds dev count and sha" {
  git tag -a v0.1.0 -m "Release 0.1.0"
  git commit -q --allow-empty -m "fix: later"
  run "$SCRIPT" version
  [ "$status" -eq 0 ]
  [[ "$output" == 0.1.0-dev.1+* ]]
}

@test "check passes on tagged commit" {
  git tag -a v0.1.0 -m "Release 0.1.0"
  run "$SCRIPT" check
  [ "$status" -eq 0 ]
}

@test "check passes (non-clean) on commit past tag" {
  git tag -a v0.1.0 -m "Release 0.1.0"
  git commit -q --allow-empty -m "fix: later"
  run "$SCRIPT" check
  [ "$status" -eq 0 ]
  [[ "$output" == *"invariant ok"* ]]
}

@test "no-tags repo emits 0.0.0-dev" {
  run "$SCRIPT" version
  [ "$status" -eq 0 ]
  [[ "$output" == 0.0.0-dev.* ]]
}
