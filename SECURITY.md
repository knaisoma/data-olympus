# Security Policy

## Supported versions

data-olympus is pre-release software. Only the latest commit on `main` receives security fixes. There is no LTS branch and no backport policy yet.

| Version | Supported |
|---------|-----------|
| 0.x (latest `main`) | Yes |
| Earlier 0.x commits | No |

## Reporting a vulnerability

**Do not open a public GitHub issue to report a security vulnerability.** Public disclosure before a fix is available puts all users at risk.

Report vulnerabilities privately using GitHub's built-in Security Advisories:

1. Go to `https://github.com/knaisoma/data-olympus/security/advisories/new`
2. Fill in the advisory form with a description, reproduction steps, and (if known) a suggested fix.
3. Submit. Maintainers will acknowledge within 5 business days and coordinate a fix and disclosure timeline with you.

For general questions about hardening or threat modelling, open a public Discussion instead.

## Responsible disclosure expectations

We follow coordinated disclosure. Please give maintainers reasonable time (typically 90 days) to investigate and release a fix before public disclosure. We will credit reporters by name (or handle) in the advisory unless you prefer to remain anonymous.

## Security-relevant surfaces

### Write pipeline

The single-writer write pipeline (propose, pending, resolve) is the primary attack surface for a hosted deployment. Key controls:

- **Path blocklist.** The `KB_WRITE_BLOCK_TIERS` and `KB_WRITE_BLOCK_PATHS` environment variables restrict which paths agent-proposed writes may target. Operators should configure these to the minimum surface that agents legitimately need.
- **Structural write rules.** Only `.md` files under indexed prefixes are accepted. Path traversal sequences (`..`) and writes under `.git/`, `tools/`, or `.worktrees/` are rejected by the server.
- **Path containment.** Every write path resolves its target through a shared `safe_join_under_root()` guard and rejects any proposal whose resolved location escapes the per-session worktree (symlink, traversal, or absolute path), before any filesystem side effect. This covers memory propose, edit, resolve, and onboarding bootstrap.
- **Payload size caps.** `KB_MAX_TEXT_BYTES` (memory text, default 256 KiB), `KB_MAX_POSTIMAGE_BYTES` (edit/bootstrap file, default 1 MiB), and `KB_MAX_BODY_BYTES` (REST request body, default 2 MiB) reject oversized proposals before any disk write. The body cap is enforced by reading the request stream incrementally and returning `413` once the byte count is exceeded, so it bounds chunked or `Content-Length`-omitting clients, not just honest ones. Set to `0` to disable a given cap.
- **Aggregate caps.** Onboarding bootstrap rejects requests over `KB_MAX_BOOTSTRAP_FILES` (default 50). The pending queue is bounded by `KB_PENDING_QUEUE_CAP` (default 100); enqueue past the cap is rejected rather than growing unbounded on disk.
- **Confidence clamp.** Client-supplied `confidence` is advisory. Only a caller whose principal holds the `auto_commit` capability may auto-commit; everyone else has their proposals parked as pending regardless of the asserted confidence (see authentication below).
- **Rate limiting.** A per-(remote_addr, agent_identity) sliding window (`KB_RATE_LIMIT_PER_HOUR`) plus an optional per-IP cap (`KB_RATE_LIMIT_PER_IP_PER_HOUR`, default disabled) bound write volume. Behind a proxy, set `KB_TRUSTED_PROXIES` so `remote_addr` is the real client IP (off by default; see the network-security section).
- **Single writer.** The server runs a single writer with advisory locking and a durable push queue. Concurrent write races from multiple agent sessions are serialised rather than silently merged.

### Audit log

Every write and enforcement decision is appended to a JSONL audit log. The log is **tamper-evident**: each event carries an `event_id`, the `prev_hash` of the previous event, and its own `hash` over the canonical event body (SHA-256, or keyed HMAC-SHA256 when `KB_AUDIT_HMAC_KEY` is set). Any later edit, deletion, or reordering breaks the chain; recompute it with `GET /api/v1/audit/verify` or `kb audit --verify`.

### Secrets handling

- `deploy/k8s/secret.template.yaml` is a placeholder template only. It must never contain real keys or credentials and must never be committed with real values substituted in.
- Secrets (deploy keys, API tokens) are operator-supplied at deploy time via SOPS-encrypted Kubernetes Secrets or equivalent. The codebase itself contains no credentials.

### Viewer (bundle visualizer)

The `data-olympus visualize` command produces an HTML bundle graph. User-authored markdown content that ends up in the rendered output is sanitized with DOMPurify before insertion into the DOM, which mitigates stored-XSS risks from malicious frontmatter or document bodies.

### Network exposure

The MCP server is a single-writer internal service designed for operator-controlled deployment (local network or private Kubernetes cluster).

#### Bearer-token authentication and capabilities (`KB_AUTH_TOKEN` / `KB_AUTH_PRINCIPALS`)

Authentication is resolved from the `Authorization: Bearer <token>` header against a registry of **principals**, each holding a set of **capabilities**: `read`, `propose`, `auto_commit`, `resolve`, `bootstrap`, `record_event`.

- `KB_AUTH_TOKEN=<secret>` registers a single full-capability principal named `operator` (back-compatible).
- `KB_AUTH_PRINCIPALS=<json>` optionally registers per-agent tokens with explicit capabilities, e.g.:

  ```json
  [{"name": "codex", "token": "<secret>", "capabilities": ["read", "propose"]}]
  ```

  A principal with `propose` but not `auto_commit` may propose, but its proposals are always parked as pending (the confidence clamp).

Tokens are compared with `hmac.compare_digest` (constant time).

**Coverage is REST and MCP.** Unlike earlier releases where the token guarded REST routes only, an MCP-transport middleware enforces the same capabilities on the MCP write tools (`kb_propose_*`, `kb_resolve_pending`, `kb_bootstrap_project`, `kb_record_event`). The observability/enforcement tools (`kb_list_pending`, `kb_audit`, `kb_consult`, `kb_gate_check`, `kb_compliance`) likewise require an authenticated principal over MCP when auth is configured, matching the REST gating. A token-less MCP client can no longer call write or observability tools when auth is configured.

When auth is configured, the following require a capable principal (else `401` for an anonymous/invalid token, `403` for an authenticated principal missing the capability):

| Route / MCP tool | Capability |
|---|---|
| `POST /api/v1/propose/memory`, `/propose/edit` · `kb_propose_memory`, `kb_propose_edit` | `propose` (+ `auto_commit` to skip pending) |
| `POST /api/v1/resolve/{pending_id}` · `kb_resolve_pending` | `resolve` |
| `POST /api/v1/onboarding/bootstrap` · `kb_bootstrap_project` | `bootstrap` |
| `POST /api/v1/audit/event` · `kb_record_event` | `record_event` |
| `POST /api/v1/consult`, `/gate/check` | any authenticated principal |
| `GET /api/v1/pending`, `/audit`, `/audit/verify` | any authenticated principal |

Read routes (`/api/v1/search`, `/get`, `/list`, `/outline`, `/health`) remain open regardless of auth.

The `bin/kb` CLI includes `Authorization: Bearer $KB_AUTH_TOKEN` on write and observability subcommands when the variable is set.

When neither `KB_AUTH_TOKEN` nor `KB_AUTH_PRINCIPALS` is set (the default), every caller is the fully-trusted local principal and all routes are open. This is the trusted-agent assumption: only safe on a trusted private network.

**Authentication is not a substitute for network-level access control.** Read routes stay open, so for strict confidentiality of the knowledge base you still MUST:

- Deploy on a trusted private network, or behind an authenticating reverse proxy (terminate TLS there).
- NOT expose the server to untrusted networks.

The default `kubectl apply -k deploy/k8s/` does NOT create the Ingress (it would publish the write routes to the LAN with auth unset by default); it is opt-in after `KB_AUTH_TOKEN` is set. The Docker Compose file binds `127.0.0.1:8080` so the local demo is not LAN-exposed. See `deploy/k8s/README.md`.

**Proxy headers.** Behind a reverse proxy, set `KB_TRUSTED_PROXIES` to the proxy address(es) so the rate limiter sees the real client IP via `X-Forwarded-For`. It is **off by default** (X-Forwarded-For ignored), which prevents a direct client from spoofing its address to evade the per-IP cap. See `docs/serving.md`.

## Container hardening

The Kubernetes pod runs fully rootless: the `prepare-git` initContainer (deploy-key staging + first-boot clone) and the main container both run as uid 65534 with `runAsNonRoot: true`, all Linux capabilities dropped, `allowPrivilegeEscalation: false`, and a read-only root filesystem. The old root+`gosu` entrypoint phase has been removed. The base image is digest-pinned in `deploy/docker/Dockerfile`. Readiness targets `/readyz` (index loaded, independent of data staleness); `/api/v1/health` keeps its 503-on-degraded contract for the CLI `--no-stale` flag and should be **alerted on, not probed**. Operational runbook: `docs/operations.md`.

## License

This project is licensed under the Apache License 2.0. Security notices do not change the license terms.
