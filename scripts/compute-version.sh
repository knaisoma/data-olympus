#!/usr/bin/env bash
# STD-U-810 §5 version computation and §5.1 invariant check.
set -euo pipefail

compute() {
  local dirty=""
  if git describe --tags --long --match 'v*' >/dev/null 2>&1; then
    local raw sha rest count tag base
    raw=$(git describe --tags --long --match 'v*' --dirty)
    case "$raw" in *-dirty) dirty=".dirty"; raw=${raw%-dirty};; esac
    sha=${raw##*-g}
    rest=${raw%-g*}          # v0.1.1-37
    count=${rest##*-}        # 37
    tag=${rest%-*}           # v0.1.1
    base=${tag#v}            # 0.1.1
    if [ "$count" = "0" ] && [ -z "$dirty" ]; then
      echo "$base"
    else
      echo "${base}-dev.${count}+${sha}${dirty}"
    fi
  else
    local sha count
    sha=$(git rev-parse --short HEAD)
    count=$(git rev-list --count HEAD)
    git diff --quiet || dirty=".dirty"
    echo "0.0.0-dev.${count}+${sha}${dirty}"
  fi
}

check() {
  local v on_tag="no"
  v=$(compute)
  if git describe --tags --exact-match --match 'v*' >/dev/null 2>&1; then
    on_tag="yes"
  fi
  if [ "$on_tag" = "no" ] && [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "INVARIANT VIOLATION: non-tag commit emitted clean version $v" >&2
    exit 1
  fi
  echo "invariant ok: $v"
}

case "${1:-version}" in
  version) compute ;;
  check) check ;;
  *) echo "usage: $0 [version|check]" >&2; exit 2 ;;
esac
