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
- **Single writer.** The server runs a single writer with advisory locking and a durable push queue. Concurrent write races from multiple agent sessions are serialised rather than silently merged.

### Secrets handling

- `deploy/k8s/secret.template.yaml` is a placeholder template only. It must never contain real keys or credentials and must never be committed with real values substituted in.
- Secrets (deploy keys, API tokens) are operator-supplied at deploy time via SOPS-encrypted Kubernetes Secrets or equivalent. The codebase itself contains no credentials.

### Viewer (bundle visualizer)

The `data-olympus visualize` command produces an HTML bundle graph. User-authored markdown content that ends up in the rendered output is sanitized with DOMPurify before insertion into the DOM, which mitigates stored-XSS risks from malicious frontmatter or document bodies.

### Network exposure

The MCP server is a single-writer internal service designed for operator-controlled deployment (local network or private Kubernetes cluster).

#### Optional bearer-token gate (`KB_AUTH_TOKEN`)

Set the `KB_AUTH_TOKEN` environment variable to a non-empty secret string to enable built-in bearer-token authentication on the write routes:

```
KB_AUTH_TOKEN=<your-secret-token>
```

When set, the following write routes require an `Authorization: Bearer <token>` header that exactly matches `KB_AUTH_TOKEN` (checked with `hmac.compare_digest` to prevent timing attacks):

- `POST /api/v1/propose/memory`
- `POST /api/v1/propose/edit`
- `POST /api/v1/resolve/{pending_id}`
- `POST /api/v1/onboarding/bootstrap`

Requests with a missing or incorrect token receive `HTTP 401 {"error": "unauthorized"}`.

Read routes (`/api/v1/search`, `/api/v1/get`, `/api/v1/list`, `/api/v1/outline`, `/api/v1/health`) remain open regardless of `KB_AUTH_TOKEN`.

The `bin/kb` CLI automatically includes `Authorization: Bearer $KB_AUTH_TOKEN` on write subcommands when the variable is set.

When `KB_AUTH_TOKEN` is empty (the default), behavior is unchanged from previous releases: all routes are open.

**`KB_AUTH_TOKEN` is not a substitute for network-level access control.** For full protection of both read and write routes, you still MUST:

- Deploy on a trusted private network, or behind an authenticating reverse proxy.
- NOT expose the server to untrusted networks.

Bearer-token auth adds a meaningful second layer for write routes, but for environments requiring strict confidentiality of the knowledge base, add authentication at the network layer as well.

## License

This project is licensed under the Apache License 2.0. Security notices do not change the license terms.
