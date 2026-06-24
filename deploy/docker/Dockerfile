# syntax=docker/dockerfile:1.7

FROM python:3.13-slim AS runtime

# Install git + ssh + gosu (privilege-drop helper) + ca-certs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git openssh-client ca-certificates curl gosu \
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

# Pre-create runtime volume mount points + writable kb-main.
RUN mkdir -p /kb-main /kb-worktrees /state/pending /state/pending/locks \
             /state/push-queue /state/audit /index \
    && chown -R 65534:65534 /kb-main /kb-worktrees /state /index

# Bake GitHub's current known_hosts; rotate by rebuilding image.
RUN ssh-keyscan -t ed25519 github.com > /etc/ssh/known_hosts \
    && chmod 0644 /etc/ssh/known_hosts

# Entrypoint runs as root (no USER directive); drops privileges in entrypoint.sh.
COPY deploy/docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV KB_MAIN_PATH=/kb-main \
    KB_INDEX_PATH=/index/kb.db \
    KB_PENDING_ROOT=/state/pending \
    KB_PUSH_QUEUE_ROOT=/state/push-queue \
    KB_WORKTREE_ROOT=/kb-worktrees \
    KB_HTTP_PORT=8080

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uv", "run", "--no-sync", "data-olympus-mcp"]
