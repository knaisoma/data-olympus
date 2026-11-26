#!/bin/sh
# Checks that the built container image reaps orphaned processes: tini must be
# PID 1 (deploy/docker/Dockerfile ENTRYPOINT). Run it INSIDE the image through
# the DEFAULT entrypoint chain -- tini, then /entrypoint.sh, then this script
# as the child -- so PID 1 is the thing under test:
#
#   docker run --rm -i --network none <image> sh -s < tests/image/zombie_reaping.sh
#
# (ci.yaml image-git-protocols runs exactly that against the built image.)
#
# What it proves:
#   1. PID 1 is /sbin/tini, not the entrypoint shell nor the server;
#   2. a grandchild orphaned by its parent's exit is reparented to PID 1 and
#      REAPED once it exits: its /proc entry disappears. Without a reaping
#      init it sits in state Z for the life of the container (knaisoma/
#      data-olympus#434: 8,000+ git zombies after 5 days);
#   3. SIGTERM sent to a private tini instance reaches the trapped child
#      (signal forwarding, so k8s SIGTERM handling is unchanged).
set -eu

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

TINI=${TINI_PATH:-/sbin/tini}
[ -x "$TINI" ] || fail "$TINI not present in image"

# From /proc/PID/stat: comm = between the first '(' and the LAST ')'; the
# fields after it are "state ppid ...".
proc_comm() { sed 's/^[^(]*(//; s/)[^)]*$//' "/proc/$1/stat" 2>/dev/null; }
proc_fields_after_comm() { sed 's/.*) //' "/proc/$1/stat" 2>/dev/null; }

count_zombies() {
    c=0
    for d in /proc/[0-9]*; do
        pid=${d#/proc/}
        st=$(proc_fields_after_comm "$pid" 2>/dev/null) || continue
        state=${st%% *}
        [ "$state" = "Z" ] && c=$((c + 1))
    done
    echo "$c"
}

# --- 1. PID 1 identity --------------------------------------------------------
[ -r /proc/1/cmdline ] || fail "/proc/1/cmdline not readable"
pid1=$(tr '\0' ' ' < /proc/1/cmdline | sed 's/ *$//')
case "$pid1" in
    "$TINI "*|"$TINI") pass "PID 1 is tini ('$pid1')" ;;
    *) fail "PID 1 is '$pid1', expected '$TINI'" ;;
esac

# --- 2. orphan reparented to PID 1, then reaped -------------------------------
# The middle shell backgrounds a 2s sleep and exits at once, so the sleep is
# orphaned and reparented to PID 1 (tini) while still running. This mirrors
# git's detached helpers in the real leak.
sh -c 'sleep 2 & exit' &
mid=$!
wait "$mid" 2>/dev/null || true

# Find the orphan: a process named "sleep" whose parent is PID 1.
opid=""
i=0
while [ "$i" -lt 10 ]; do
    for d in /proc/[0-9]*; do
        pid=${d#/proc/}
        [ "$(proc_comm "$pid" 2>/dev/null)" = "sleep" ] || continue
        fields=$(proc_fields_after_comm "$pid" 2>/dev/null) || continue
        # shellcheck disable=SC2086
        set -- $fields
        [ "$2" = "1" ] && opid=$pid && break
    done
    [ -n "$opid" ] && break
    i=$((i + 1))
    sleep 1
done
[ -n "$opid" ] || fail "orphaned sleep never appeared as a child of PID 1"
pass "orphan reparented to PID 1 (pid $opid)"

# When it exits, only a reaping init can clear its /proc entry.
i=0
while [ -d "/proc/$opid" ] && [ "$i" -lt 20 ]; do
    i=$((i + 1))
    sleep 1
done
if [ -d "/proc/$opid" ]; then
    st=$(proc_fields_after_comm "$opid" 2>/dev/null || echo "?")
    fail "orphan pid $opid still in /proc after 20s (state '$st'): PID 1 is not reaping"
fi
z=$(count_zombies)
[ "$z" = "0" ] || fail "$z zombie(s) present after the orphan exited"
pass "orphan reaped by PID 1 (no zombies remain)"

# --- 3. signal forwarding through tini ----------------------------------------
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
"$TINI" -s -- sh -c "trap 'touch $work/got-term; exit 0' TERM; sleep 5 & wait" &
tpid=$!
sleep 1
kill -TERM "$tpid"
wait "$tpid" 2>/dev/null || true
[ -f "$work/got-term" ] || fail "tini child never received SIGTERM"
pass "SIGTERM sent to tini reaches the trapped child"

echo "PASS: image reaps orphans and forwards signals (tini as PID 1)"
