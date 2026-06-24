# Serving model

This document describes the normative serving model for data-olympus. It
summarizes `SPEC.md` section 8; for the authoritative text, see that section.

## Single replica, streamable HTTP

A write-enabled data-olympus server runs as a **single replica** and exposes
MCP over **streamable HTTP**.

The write path is single-writer by design:

- Per-path advisory locks prevent two concurrent write operations from racing
  on the same concept file.
- Per-session git worktrees isolate in-flight proposed edits from the live
  index until they are committed.
- A durable push queue serializes outbound git pushes so no write is lost if
  the remote is briefly unavailable.

A single shared HTTP surface, rather than N independent stdio processes, gives
every agent one synchronized conversation with the server. When multiple agents
each run their own stdio MCP process against the same git working tree, they
race each other's worktrees and lock state. Streamable HTTP eliminates that
race.

## Read-only mirrors may scale horizontally

A read-only replica that serves only `kb_search`, `kb_get`, `kb_list`, and
`kb_outline` need not maintain the write pipeline and may run as many instances
as needed. For higher read throughput, place a caching reverse proxy in front
of the single write instance, or run dedicated read-only replicas that
periodically pull from the main instance's git remote.

## Git pull loop

On startup, and at the interval set by `KB_SYNC_INTERVAL_SEC` (default 60s),
the server calls `git pull` on `KB_MAIN_PATH`. If `KB_REMOTE_URL` is empty,
the pull loop runs but exits cleanly with no action. The `health` endpoint
reports `degraded: true` only when the index has not been rebuilt within
`KB_STALENESS_DEGRADED_SEC` (default 600s).

For a local read-only demo with no remote, set `KB_REMOTE_URL=""`. The server
stays healthy as long as the index builds successfully on startup.

## Authentication and network security

The MCP server has no built-in authentication. It is a single-writer internal service.

Operators MUST deploy it on a trusted private network or behind an authenticating reverse proxy. The write routes (`/api/v1/propose/*`, `/api/v1/resolve`, `/api/v1/onboarding/bootstrap`) allow arbitrary knowledge-base writes. Do NOT expose these routes to untrusted networks.

See `SECURITY.md` for the full threat model.

## Running locally

See `docs/quickstart.md` for the verified local-run procedure using
`scripts/run-local.sh`.

## Running in Kubernetes

See `deploy/k8s/` for the kustomize manifests. Apply them with:

```bash
# Apply namespace and non-secret resources
kubectl apply -k deploy/k8s/

# Apply the encrypted secret separately (operators use SOPS)
sops exec-file deploy/k8s/secret.sops.yaml 'kubectl apply -f {}'
```

The `deploy/k8s/secret.template.yaml` file shows the expected secret shape.
Fill in your git remote URL and deploy key, then encrypt with SOPS before
committing. Never commit the unencrypted secret.

## Running with Docker Compose

```bash
docker compose -f deploy/docker/compose.yaml up --build
```

See `docs/quickstart.md` section 5 for the bind-mount pattern to serve a
real bundle.
