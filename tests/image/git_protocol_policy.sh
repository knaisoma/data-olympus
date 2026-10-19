#!/bin/sh
# Checks the git transport policy of the built container image. Run it INSIDE
# the image as its runtime user, with the entrypoint bypassed and no network:
#
#   docker run --rm -i --network none --entrypoint /bin/sh <image> -s \
#     < tests/image/git_protocol_policy.sh
#
# What it proves (deploy/docker/Dockerfile, "Close git's libcurl transports"):
#   - a clone over file:// still works;
#   - a clone and a push over the SSH transport still work. A stand-in ssh
#     runs the remote command (git-upload-pack / git-receive-pack) locally, so
#     git takes its real ssh code path without a network or an sshd;
#   - http, https, ftp and ftps URLs are refused by git's protocol policy, not
#     by a network error: the failure must carry git's
#     "transport '<name>' not allowed" message.
set -eu

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

# The policy must come from the image, not from this shell.
unset GIT_ALLOW_PROTOCOL GIT_CONFIG_NOSYSTEM GIT_CONFIG_SYSTEM GIT_CONFIG_GLOBAL \
      GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT GIT_SSH_COMMAND 2>/dev/null || true

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
ident="-c user.name=policy-check -c user.email=policy-check@localhost"

for proto in http https ftp ftps; do
    value=$(git config --system --get "protocol.$proto.allow" || true)
    [ "$value" = never ] || fail "system gitconfig sets protocol.$proto.allow to '$value', expected 'never'"
done
pass "system gitconfig refuses http, https, ftp and ftps"

# A bare source repository with one commit.
git init -q "$work/seed"
echo policy > "$work/seed/README"
git -C "$work/seed" add README
# shellcheck disable=SC2086
git -C "$work/seed" $ident commit -q -m seed
git clone -q --bare "$work/seed" "$work/src.git"
expected=$(git -C "$work/src.git" rev-parse HEAD)

# file:// transport.
git clone -q "file://$work/src.git" "$work/via-file" || fail "file:// clone was refused"
[ "$(git -C "$work/via-file" rev-parse HEAD)" = "$expected" ] || fail "file:// clone has the wrong HEAD"
pass "file:// clone works"

# SSH transport through a local stand-in for ssh. With GIT_SSH_VARIANT=simple
# git passes only the host and the remote command; run the command here.
cat > "$work/fake-ssh" <<'EOF'
#!/bin/sh
for last do :; done
exec sh -c "$last"
EOF
chmod 0755 "$work/fake-ssh"
export GIT_SSH="$work/fake-ssh" GIT_SSH_VARIANT=simple

git clone -q "git@kb-remote.invalid:$work/src.git" "$work/via-ssh" || fail "ssh transport clone was refused"
[ "$(git -C "$work/via-ssh" rev-parse HEAD)" = "$expected" ] || fail "ssh transport clone has the wrong HEAD"
git -C "$work/via-ssh" fetch -q origin || fail "ssh transport fetch was refused"
echo push >> "$work/via-ssh/README"
# shellcheck disable=SC2086
git -C "$work/via-ssh" $ident commit -q -am push
git -C "$work/via-ssh" push -q origin HEAD:refs/heads/ssh-push-check || fail "ssh transport push was refused"
[ "$(git -C "$work/src.git" rev-parse refs/heads/ssh-push-check)" = "$(git -C "$work/via-ssh" rev-parse HEAD)" ] \
    || fail "ssh transport push did not update the remote"
pass "ssh transport clone, fetch and push work"
unset GIT_SSH GIT_SSH_VARIANT

# libcurl transports are refused by policy.
for proto in http https ftp ftps; do
    if git ls-remote "$proto://kb-remote.invalid/repo.git" > "$work/out" 2> "$work/err"; then
        fail "$proto remote was not refused"
    fi
    grep -q "transport '$proto' not allowed" "$work/err" \
        || fail "$proto remote failed for another reason: $(cat "$work/err")"
    if git clone -q "$proto://kb-remote.invalid/repo.git" "$work/clone-$proto" 2> "$work/err"; then
        fail "$proto clone was not refused"
    fi
    grep -q "transport '$proto' not allowed" "$work/err" \
        || fail "$proto clone failed for another reason: $(cat "$work/err")"
done
pass "http, https, ftp and ftps remotes are refused by policy"

echo "git protocol policy: all checks passed"
