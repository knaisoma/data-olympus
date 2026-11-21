# syntax=docker/dockerfile:1.7

# Digest-pinned base for reproducible, tamper-resistant builds. This is the
# multi-arch (linux/amd64, arm64, ...) OCI index digest for python:3.15-alpine
# (Python 3.15.2 on Alpine Linux, musl libc; every locked runtime dependency
# ships a musllinux wheel, so nothing is compiled in the image).
# The build and runtime stages use this same pinned base. To roll the pin
# forward: resolve the current tag's index digest and replace the tag and the
# @sha256 on BOTH FROM lines below, e.g.
#   docker buildx imagetools inspect python:3.15-alpine   # shows the index digest
# or via the registry API (no docker needed):
#   TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull" | jq -r .token)
#   curl -sI -H "Authorization: Bearer $TOKEN" \
#     -H "Accept: application/vnd.oci.image.index.v1+json" \
#     https://registry-1.docker.io/v2/library/python/manifests/3.15-alpine \
#     | grep -i docker-content-digest
FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd AS build

# Build stage: uv and the dependency sync live here only. The runtime stage
# below copies /app (the virtual environment and the project) and carries no
# uv or uvx, so neither the binaries nor the Rust crates compiled into them are
# part of the shipped image. Both stages use the same pinned base, so the
# venv's interpreter symlink (/app/.venv/bin/python -> /usr/local/bin/python3)
# resolves identically in the runtime stage.
#
# Install uv, pinned by content: the static uv and uvx binaries from astral's
# official image, by its multi-arch OCI index digest. Roll forward
# deliberately; 0.11.15 is the minimum that carries the fixes for the uv
# scanner findings. To move the pin, replace BOTH the tag and the digest, after
# checking that the index digest carries astral's build provenance:
#   docker buildx imagetools inspect ghcr.io/astral-sh/uv:<version>
#   gh attestation verify oci://ghcr.io/astral-sh/uv@<index digest> --owner astral-sh
# (signer: astral-sh/uv .github/workflows/publish-docker-image.yml).
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /uvx /usr/local/bin/

# Use the base image's interpreter, never a downloaded one, so the venv points
# at the path the runtime stage also has; copy files out of the uv cache rather
# than hard-linking them, since the cache does not travel to the runtime stage.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

WORKDIR /app

# Two-layer dependency caching (preserved from 2A/2B).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The project build (the `uv sync` below, which builds + installs the wheel via
# hatchling) needs everything pyproject.toml references at build time:
#   - readme = "README.md" and license-files = ["LICENSE", "NOTICE"];
#   - [tool.hatch.build.targets.wheel] packages = src/data_olympus;
#   - [tool.hatch.build.targets.wheel.force-include] the bin/ enforcement
#     machinery shipped inside the wheel at data_olympus/_bin/.
# All of these must be in the build context before the sync, not just at
# wheel-publish time.
COPY README.md LICENSE NOTICE ./
COPY bin/ ./bin/
COPY src/ ./src/
RUN uv sync --frozen --no-dev

FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd AS runtime

# SSH host to trust for git over SSH. Defaults to github.com; override at build
# time (--build-arg KB_SSH_KEYSCAN_HOST=git.example.com) for a non-GitHub remote.
# The runtime KB_SSH_KEYSCAN_HOST env var (see entrypoint.sh) can add another host
# at boot without a rebuild.
ARG KB_SSH_KEYSCAN_HOST=github.com

# Install git + ssh + ca-certs. gosu is intentionally NOT installed: the image
# runs entirely as the non-root uid 65534 (see USER below), so there is no
# root->non-root privilege drop to perform. The deploy-key permission fix and the
# first-boot /kb-main clone that the old root entrypoint did are now handled by a
# non-root k8s initContainer (see deploy/k8s/statefulset.yaml) writing to shared
# volumes with an fsGroup, so the runtime container never needs root or gosu.
# curl is intentionally NOT installed either: nothing in the image calls it (the
# probes are httpGet/tcpSocket and the entrypoint and initContainer do not use
# it), and each copy of libcurl adds its own scanner findings. The bin/kb client
# scripts that use curl run on operator machines, not inside this container.
# `apk upgrade` first: the pinned base can lag the Alpine security updates (at
# the time of the move, the zlib and OpenSSL fixes the scanner reported were
# already published for 3.24 but not yet in the base image). The base stays
# pinned by digest; only packages from the Alpine repository move forward.
RUN apk upgrade --no-cache \
    && apk add --no-cache git openssh-client ca-certificates

# The virtual environment and the project, built in the build stage. No uv.
COPY --from=build /app /app
WORKDIR /app

# Glama and other public catalog sandboxes need a safe, populated corpus so
# tool calls exercise real results without exposing an operator KB.
COPY example-bundle/ /example-bundle/

# Pre-create runtime volume mount points + writable kb-main, owned by the runtime
# uid so the container can write them when running as non-root (and when a PVC is
# mounted the fsGroup owns the volume root; these image dirs are the fallback for
# non-k8s runs like docker compose).
RUN mkdir -p /kb-main /kb-worktrees /state/pending /state/pending/locks \
             /state/push-queue /state/audit /index \
    && chown -R 65534:65534 /kb-main /kb-worktrees /state /index /example-bundle

# Bake the git host's current known_hosts; rotate by rebuilding image (or add a
# host at runtime via KB_SSH_KEYSCAN_HOST, see entrypoint.sh). Written into
# /etc/ssh (root-owned, world-readable) at build time; the entrypoint appends any
# runtime host into a user-writable copy so the read-only-root container can still
# extend it without needing to write /etc/ssh.
RUN ssh-keyscan -t ed25519,rsa "${KB_SSH_KEYSCAN_HOST}" > /etc/ssh/known_hosts \
    && chmod 0644 /etc/ssh/known_hosts

# Close git's libcurl transports. The server reaches its KB remote over SSH
# (and local paths in tests); it never needs git over http, https, ftp or ftps.
# The Alpine git package still installs libcurl and the git-remote-http(s)/ftp(s)
# helpers, so refuse those protocols in the system gitconfig (/etc/gitconfig):
# git then stops with "transport '<name>' not allowed" before it starts a
# libcurl helper. ssh and file keep their defaults. This is a default, not a
# sandbox: GIT_ALLOW_PROTOCOL, a user or repository config, or `git -c` can
# re-enable a protocol. Placed after every build step that might run git.
# tests/image/git_protocol_policy.sh checks the behaviour inside the image.
RUN git config --system protocol.http.allow never \
    && git config --system protocol.https.allow never \
    && git config --system protocol.ftp.allow never \
    && git config --system protocol.ftps.allow never

# Entrypoint runs as the non-root runtime user (no privilege drop). It prepares a
# user-writable known_hosts and the git identity, then execs the server.
COPY deploy/docker/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

ENV KB_MAIN_PATH=/example-bundle \
    KB_INDEX_PATH=/index/kb.db \
    KB_PENDING_ROOT=/state/pending \
    KB_PUSH_QUEUE_ROOT=/state/push-queue \
    KB_WORKTREE_ROOT=/kb-worktrees \
    KB_HTTP_PORT=8080 \
    KB_SSH_KEYSCAN_HOST=${KB_SSH_KEYSCAN_HOST}

EXPOSE 8080

# Run as the non-root nobody uid. Combined with runAsNonRoot:true in k8s this lets
# the manifest pass the restricted PodSecurity standard.
USER 65534:65534

ENTRYPOINT ["/entrypoint.sh"]
# The console script of the venv, run directly; the entrypoint execs "$@".
CMD ["/app/.venv/bin/data-olympus-mcp"]
