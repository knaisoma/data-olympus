# syntax=docker/dockerfile:1.7

# Digest-pinned base for reproducible, tamper-resistant builds. This is the
# multi-arch (linux/amd64, arm64, ...) OCI index digest for python:3.13-slim.
# To roll the pin forward: resolve the current tag's index digest and replace
# BOTH the tag comment and the @sha256 below, e.g.
#   docker buildx imagetools inspect python:3.13-slim   # shows the index digest
# or via the registry API (no docker needed):
#   TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull" | jq -r .token)
#   curl -sI -H "Authorization: Bearer $TOKEN" \
#     -H "Accept: application/vnd.oci.image.index.v1+json" \
#     https://registry-1.docker.io/v2/library/python/manifests/3.13-slim \
#     | grep -i docker-content-digest
FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280 AS runtime

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
RUN apt-get update && apt-get install -y --no-install-recommends \
        git openssh-client ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv (same version as before)
RUN pip install --no-cache-dir uv==0.5.4

WORKDIR /app

# Two-layer dependency caching (preserved from 2A/2B).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --frozen --no-dev

# uv cache (writable to nonroot user)
RUN mkdir -p /tmp/uv-cache && chmod 0777 /tmp/uv-cache
ENV UV_CACHE_DIR=/tmp/uv-cache

# Pre-create runtime volume mount points + writable kb-main, owned by the runtime
# uid so the container can write them when running as non-root (and when a PVC is
# mounted the fsGroup owns the volume root; these image dirs are the fallback for
# non-k8s runs like docker compose).
RUN mkdir -p /kb-main /kb-worktrees /state/pending /state/pending/locks \
             /state/push-queue /state/audit /index \
    && chown -R 65534:65534 /kb-main /kb-worktrees /state /index

# Bake the git host's current known_hosts; rotate by rebuilding image (or add a
# host at runtime via KB_SSH_KEYSCAN_HOST, see entrypoint.sh). Written into
# /etc/ssh (root-owned, world-readable) at build time; the entrypoint appends any
# runtime host into a user-writable copy so the read-only-root container can still
# extend it without needing to write /etc/ssh.
RUN ssh-keyscan -t ed25519,rsa "${KB_SSH_KEYSCAN_HOST}" > /etc/ssh/known_hosts \
    && chmod 0644 /etc/ssh/known_hosts

# Entrypoint runs as the non-root runtime user (no privilege drop). It prepares a
# user-writable known_hosts and the git identity, then execs the server.
COPY deploy/docker/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

ENV KB_MAIN_PATH=/kb-main \
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
CMD ["uv", "run", "--no-sync", "data-olympus-mcp"]
