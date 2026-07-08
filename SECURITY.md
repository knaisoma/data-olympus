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
- **Secret-scanning gate (issue #71).** Every commit path (`kb_propose_memory` / `kb_propose_edit` auto-commit, `kb_resolve_pending` approve, including an edited postimage, and onboarding bootstrap) scans the final postimage for credential-shaped content (PEM private-key blocks, GitHub/Slack tokens, AWS access key ids, generic `password=`/`secret=` assignments including env-style prefixed keys like `DB_PASSWORD=`, and connection strings with an inline password) BEFORE the content-validation gate and before anything is written to disk or committed, so a validation-error message can never echo a credential value. A match on an auto-commit or bootstrap path rejects the write `rejected_secret_detected`. The gate also covers the fields AROUND the postimage: a credential-shaped `target_path` (filename) is rejected on edit/bootstrap/resolve without ever echoing the path back; a flagged edit `reason` is replaced with a redacted note before it reaches pending meta, push metadata, or audit events; a flagged memory tag is stored redacted in pending meta. A low-confidence proposal containing a secret still enters pending (so the operator keeps the override workflow below), but the scan runs at propose time too: the response never echoes the raw text (only the pattern name), and the pending entry is tagged `secret_scan_flagged`/`matching_pattern` (names only) so `kb pending` surfaces the warning without exposing the value; a memory proposal's filename falls back to a neutral slug instead of embedding flagged text. Only the pattern name and an approximate line number are ever surfaced anywhere, never the matched value. Operators can extend the pattern set with `KB_SECRET_SCAN_EXTRA_PATTERNS` (comma-separated regexes; an invalid regex, or one with the classic nested-quantifier ReDoS shape, is logged and skipped, never crashes the server). Custom patterns execute through the `regex` engine with a hard 1-second match timeout, so even a catastrophic-backtracking pattern the load-time heuristic misses cannot hang the single-writer write path; a timed-out pattern is logged and skipped for that scan. A human resolving a pending entry may pass `override_secret_scan: true` (`kb resolve --override-secret-scan` on the CLI) to consciously commit content the scanner flagged as a false positive; the override is recorded in the audit event and is not available on any auto-commit or bootstrap path (an agent cannot self-authorize past the gate).

### Governed-lane write protection: propose vs promote (issue #112)

Forged authority is the residual hole after the write-pipeline controls above:
under allow-by-default, a high-confidence `kb_propose_edit` could commit a
postimage claiming `status: accepted` straight into an indexed prefix, or an
edit to an already-accepted doc could change its governing content while
keeping the accepted status -- and rules propagate to the whole team via
`git pull` + index refresh, so one poisoned write spreads fast.

**The model: agents can propose, only humans can promote.** Behind
`KB_GOVERNED_LANE_PROTECTION` (default ON; `off` restores the exact
pre-#112 behavior), three mechanisms compose at the tool-function layer
(never inside the shared commit primitives, so the maintenance-ledger
system write path and the operator resolve path are untouched):

- **Status clamp.** A non-operator-confirmed write whose postimage sets or
  changes `status` INTO the in-force class (`active`/`accepted`/`approved`)
  is demoted to a pending entry, never committed and never rejected.
- **Governed-target edit demotion.** An edit whose target document is
  CURRENTLY in force (status class AND validity window AND not-inbox AND
  not-graph-excluded -- the same composed predicate every other in-force
  surface uses) is always demoted to pending, regardless of confidence. An
  expired or superseded-out target is not protected (it does not currently
  govern anything). The in-force lookup FAILS CLOSED: when it cannot be
  completed (no index, or an index read failure), the edit is demoted with
  the distinct reason `governed_target_unverified` instead of
  auto-committing -- an unhealthy index cannot be leveraged to bypass this
  rule.
- **Injection-pattern annotation.** Advisory only: a postimage matching an
  agent-directed injection heuristic (imperative instruction-override
  phrasing, exfiltration-shaped URLs, base64-looking blobs, "do not tell the
  operator", ...) is tagged on the pending entry for the reviewer, but never
  blocks or demotes by itself.

Ordering: the issue #71 secret-scanning gate and the content-validation /
duplicate-id gate always run FIRST. A postimage that would be rejected
outright by either is rejected, never silently demoted -- a demotion cannot
be used to smuggle a secret or a corrupt document past those gates by
disguising it as a governance question.

Forging a governing rule now requires getting a human to approve it in
pending review, where provenance (agent identity, session, confidence,
commit trailers, the tamper-evident audit chain) is already in front of
them. The feedback loop makes a demotion hard to miss: every demotion
response carries `pending_id` + `demotion_reason` + an instruction to
inform the operator; `kb_session_recap` / `kb session-recap` / `GET
/api/v1/session-recap` reports the committed/demoted/rejected tally for a
session; `kb_consult`'s `pending_actions` envelope surfaces open demotions
for the calling session; and `bin/kb-session-recap-hook` is a ready-to-wire
SessionEnd/Stop hook. See `docs/serving.md` for the env var and demotion
semantics and `docs/operations.md` for the recap tooling and hook wiring.

**Explicit non-goals.** This is not protection against a malicious human
reviewer (the operator-resolve path is, by design, the trusted promotion
step) or against a git push to the remote outside this server's MCP/REST
surface (branch protection and commit signing on the remote are ops-level
controls, out of scope here).

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

  **Breaking change in v0.3.0:** a `KB_AUTH_PRINCIPALS` entry that omits the `capabilities` field now defaults to least privilege (`{read, propose}`) instead of all capabilities; grant `resolve`, `auto_commit`, `bootstrap`, or `record_event` by listing them explicitly. This prevents an agent token from silently gaining self-approval (`resolve` + `auto_commit`).

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
