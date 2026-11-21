#!/bin/sh
# Runtime smoke test of the built container image. Run it on the HOST that
# built the image, with the image reference as the only argument:
#
#   sh tests/image/runtime_smoke.sh data-olympus:git-protocols
#
# What it proves (deploy/docker/Dockerfile):
#   - the image's interpreter, and the interpreter of the application virtual
#     environment the CMD runs, are Python 3.15;
#   - the container runs as the non-root uid 65534;
#   - started with its default entrypoint and CMD on the bundled example bundle
#     (KB_MAIN_PATH=/example-bundle is the image default), the server builds its
#     index and /readyz on port 8080 answers 200 within a bounded wait.
#
# The container runs without a network, with every capability dropped and
# no-new-privileges set; nothing here needs privileges. The image has no curl,
# so the readiness probe is a Python one-liner run inside the container.
set -eu

image=${1:?usage: runtime_smoke.sh <image>}
expected_python=3.15
name="do-runtime-smoke-$$"
attempts=${RUNTIME_SMOKE_ATTEMPTS:-60}

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

run_once() {
    entrypoint=$1
    shift
    docker run --rm --network none --cap-drop ALL \
        --security-opt no-new-privileges --entrypoint "$entrypoint" "$image" "$@"
}

version=$(run_once python --version 2>&1 | tr -d '\r' || true)
case "$version" in
    "Python $expected_python".*) pass "image python reports $version" ;;
    *) fail "image python reports '$version', expected Python $expected_python.x" ;;
esac

venv_version=$(run_once /app/.venv/bin/python -c \
    'import sys; print("{}.{}".format(*sys.version_info[:2]))' 2>&1 | tr -d '\r' || true)
[ "$venv_version" = "$expected_python" ] \
    || fail "application venv runs Python '$venv_version', expected $expected_python"
pass "application venv runs Python $venv_version"

uid=$(run_once id -u 2>&1 | tr -d '\r' || true)
[ "$uid" = "65534" ] || fail "container runs as uid '$uid', expected 65534"
pass "container runs as uid 65534"

# Default entrypoint and CMD, no overrides of the application configuration.
docker run -d --name "$name" --network none --cap-drop ALL \
    --security-opt no-new-privileges "$image" >/dev/null

probe='import sys, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8080/readyz", timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)'

i=0
while :; do
    i=$((i + 1))
    running=$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo false)
    if [ "$running" != "true" ]; then
        docker logs "$name" >&2 || true
        fail "container exited before it became ready"
    fi
    if docker exec "$name" python -c "$probe" >/dev/null 2>&1; then
        break
    fi
    if [ "$i" -ge "$attempts" ]; then
        docker logs "$name" >&2 || true
        fail "/readyz did not answer 200 after $attempts attempts"
    fi
    sleep 2
done
pass "/readyz answered 200 after $i attempt(s) with the default entrypoint and CMD"

docker stop -t 10 "$name" >/dev/null
pass "container stopped"
