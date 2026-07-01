# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- CONVENTION: The topmost section should always be an [Unreleased] block for
     changes merged to main but not yet tagged. When a release is cut, rename
     that block to the version and date, then open a new empty [Unreleased] block
     above it. Never hand-edit the release date on the [Unreleased] heading. -->

## [Unreleased]

### Added

- Automated, human-gated release pipeline (conformant with the STD-U-810
  versioning standard). A daily `data-olympus-release-cutter` routine computes the
  SemVer bump from Conventional Commits and opens a release PR; merging that PR
  cuts an annotated `vX.Y.Z` tag, a GitHub Release, and the container image via
  CI. New helpers `scripts/compute_release.py`, `scripts/should_tag.py`,
  `scripts/lint_pr_title.py` (a PR-title Conventional Commit check enforced by
  `.github/workflows/pr-title-lint.yml`), and `scripts/compute-version.sh` (preview
  version plus invariant check). New `tag-release.yml` cuts the tag on a
  `pyproject.toml` version bump, builds the image via a reusable
  `release-image-reusable.yml` (which also stamps OCI version and revision
  labels), then publishes the GitHub Release.
  Process is documented in `.rules/versioning.md` and `.rules/release-routine.md`.
  No tags are ever cut unattended.
- Search now short-circuits exact-id and exact-tag queries: a single-token query
  that is a document id (e.g. `STD-U-002`, or a path-derived id) is surfaced as
  the top hit via a direct lookup even when it is absent from the bm25 results,
  and a single-token exact-tag query lifts docs carrying that tag ahead of the
  rest. Implemented as a composable `reranker` (issue #39); the match stage and
  `_build_match_expr` are untouched.
- `kb_search` and `kb_get` now support and return `status` and `type`, making the
  documented "filter by status / tier / type" capability real (index schema v4).
- Retrieval now indexes `applies_when` trigger metadata and `description` with
  column-weighted ranking, improving coding-intent to governing-rule matching
  (index schema v5). `kb_get` returns both fields.
- Status-aware search ranking (enabled by default): `kb_search` reranks hits by a
  status prior on top of BM25, so an in-force doc (`active`, `accepted`,
  `approved`) outranks a `superseded`/`deprecated`/`rejected` one that matches the
  same query, and drafts do not beat live guidance. Status matching is
  case-insensitive; unknown or empty statuses stay neutral and are never dropped.
  This changes result ordering for queries that hit both a live and a retired doc.
  A deployment overrides the built-in status->weight map via `KB_STATUS_WEIGHTS`
  (a JSON object of `{status: weight}`, negative boosts, positive penalizes).
- Enforcement core: data-olympus can now act as a gated, mandatory consultation
  proxy for code/architectural decisions, not only an advisory KB. New MCP tools
  `kb_consult`, `kb_gate_check`, `kb_compliance` and REST endpoints
  `/api/v1/consult`, `/api/v1/gate/check`, `/api/v1/compliance`, backed by a
  shared heuristic intent classifier and an in-memory consultation ledger.
- `kb enforce install|uninstall|status|doctor` CLI plus a `kb-enforce-hook`
  dispatcher install an idempotent, reversible Claude Code enforcement shim
  (SessionStart / UserPromptSubmit / PreToolUse / Stop). Fail-open by default
  (`KB_ENFORCE_FAIL_MODE`), consultation freshness via `KB_CONSULT_TTL_SEC`.
- Cross-agent enforcement providers: `kb enforce install --agent codex|gemini|opencode|copilot-cli|copilot-ide` and `--all`. Codex and Gemini get hard PreToolUse/BeforeTool gates (merging into existing hooks), OpenCode gets a `tool.execute.before` plugin, and Copilot CLI/IDE get a soft instructions-file + MCP provider. Antigravity is a documented-unsupported stub. The `kb-enforce-hook` dispatcher gained a `--dialect gemini` output mode.
- Detection floor for un-hookable agents: `kb enforce report` (and `data-olympus report`) correlates governed git commits against the consult audit and lists changes with no consultation on record. An opt-in repo-scoped git provider (`kb enforce install --agent git`) installs a post-commit warning hook, or a pre-commit blocking hook with `--block`. Reuses the existing audit endpoint; no server change.
- Streamable-http session idle reaper bounds transport accumulation (issue #43).
  A background loop terminates sessions with no request activity for longer than
  `KB_SESSION_IDLE_TIMEOUT_SEC` (default 1800s / 30 min; `0` disables reaping and
  keeps observability only), scanning every `KB_SESSION_REAP_INTERVAL_SEC`
  (default 60s). `kb_health` and `/api/v1/health` gained a `live_sessions` field
  reporting the current live transport count (`null` until the HTTP app is
  serving); a value that only climbs signals leaked sessions.
- Synonym / acronym query expansion (default-on). Before the FTS MATCH is built,
  the search pipeline rewrites the query term list through a curated, bidirectional
  synonym map, so a short-form query (`k8s`, `rls`) also reaches documents that only
  use the long form (`kubernetes`, `row level security`) and vice versa. Adjacent
  query tokens are scanned as n-grams so multi-word canonical keys match from a
  long-form query. Expansion is bounded (32 terms) and de-duplicated, originals are
  ranked first, and it is configurable via `KB_SYNONYMS` (extra/override groups) and
  `KB_SYNONYMS_MODE` (`merge` default / `replace` / `off`). This is curated
  lexical expansion, not semantic (embedding) retrieval.
- Enforcement hardening and observability: gate_bypass and gate_degraded events are now recorded (via `data-olympus report --emit-events` / the git warn hook, and the pre-tool hook on a reachable-degraded gate), so `kb_compliance` surfaces them. The gate receives `action_diff` and the classifier uses word-boundary matching plus dependency-install command signals (Codex and Claude now gate the Bash tool). New `kb enforce install --mode off|soft|hard`, ledger persistence via `KB_LEDGER_PATH`, a friendly PATH hint for `kb enforce report`, and a CI guard requiring a changelog entry for functional changes.
- Guided onboarding: MCP prompts `onboard`, `onboard_project`, and `onboard_component` walk an agent through bootstrapping a new workspace or component, backed by a single-sourced playbook (`render_playbook`). A read-only `kb_cleanup_plan` MCP tool plus `POST /api/v1/onboarding/cleanup-plan` classify local repo docs against KB content and propose thin-pointer replacements for duplicates. `GET /api/v1/onboarding/playbook` and `kb onboard playbook` expose the same script to agents without native MCP prompt support.
- Composable search pipeline: `Index.search()` now runs expand-query / match /
  re-rank stages with pluggable `query_expander` and `reranker` hooks, so ranking
  and query-expansion features compose without rewriting the core query. When a
  reranker is installed the query is matched against a wider BM25 candidate pool
  (over-fetched) and the reranked result is truncated back to the requested limit.
- Read-only replica serving mode (`KB_READ_ONLY`, issue #44) for horizontal read scaling. When set, the server registers only the read tools and read REST routes; the write/enforcement-write tools and routes (propose/resolve/bootstrap/consult/gate/record-event plus the observability GETs `/api/v1/pending`, `/api/v1/audit`, `/api/v1/audit/verify`, `/api/v1/compliance`) are not registered and return 404, and the write pipeline (worktrees/push queue/pending) is never initialised, so a replica is never a git writer. A new `deploy/k8s/read-replica/` overlay (Deployment, Service, ConfigMap, NetworkPolicy, kustomization) runs N interchangeable read pods with per-pod ephemeral clone + index scratch and references a separate read-only git deploy key Secret (`data-olympus-mcp-readonly`). The replica logs an unambiguous "starting in READ-ONLY mode; write pipeline disabled" line at startup.

### Fixed

- Intermittent `503 Service Unavailable` from the single-replica MCP under load.
  The readiness probe was too aggressive (`timeoutSeconds: 1`) while every
  REST/enforcement handler ran synchronous work on the one asyncio event loop, so
  a burst stalled the probe and the only pod was ejected from the Service (nginx
  then served a 503 with an empty upstream, leaving no application-log trace).
  Fixes: relax the readiness probe (5s timeout, 5 failures) and raise the CPU
  limit to 2; offload blocking handler work to the anyio worker pool while serving
  `/api/v1/health` inline (off that pool) from a short-TTL cache of
  `Index.health()`; add locks to the audit log and consultation ledger for safe
  concurrent access.
- The consultation ledger grew without bound (every `(session, workspace)` pair,
  the whole file rewritten on every consult). It now evicts entries past the
  consult TTL, enforces a hard max-entries cap, and bounds an oversized persisted
  ledger on load rather than holding it all in memory until the first prune.
- `data-olympus-mcp --help` now prints usage and exits 0 instead of crashing with
  `NotADirectoryError: KB root not a directory: /kb-main`. Argument parsing runs
  before config loading, so the documented quickstart command works on first run.
- Git sync failures are now visible instead of masquerading as a fresh
  no-change. `kb_health` / `/api/v1/health` expose `last_git_fetch_status`
  (`changed` / `no_change` / `no_remote` / `fetch_failed` / `ff_failed`),
  `last_git_fetch_error`, `last_git_fetch_at`, `last_successful_refresh_at`, and
  `remote_head_sha`. A fetch or fast-forward failure no longer advances the
  freshness marker, so staleness climbs and health degrades rather than reporting
  the KB as up to date against a broken or diverged remote. A deployment with no
  remote (read-only) is classified `no_remote` and stays healthy.
- Write routes return structured errors instead of opaque 500s. In read-only mode
  (`KB_REMOTE_URL` unset) the write pipeline is disabled, and `POST
  /api/v1/propose/*`, `/resolve/{id}`, and `/onboarding/bootstrap` now return a
  `503 {"error":"write_pipeline_disabled"}` (and the MCP write tools return
  `{"status":"write_pipeline_disabled"}`) instead of crashing on an internal
  assertion. Missing-field validation was extended to `/api/v1/consult`,
  `/api/v1/gate/check`, and `/api/v1/resolve/{id}`, so a request that omits a
  required field gets an actionable `400` rather than a `KeyError`-driven 500.

### Security

- Hardening from the companion security review:
  - **Audit chain forgery fixed (blocker):** `verify()` now tolerates unhashed
    legacy lines only as a prefix before the chain starts; any unhashed line after
    the first hashed event breaks verification, so an appended legacy-shaped record
    can no longer forge an event while keeping `/audit/verify` green.
  - **Path containment tightened:** `safe_join_under_root()` now also requires the
    resolved path to equal the lexical target, rejecting an in-tree symlink that
    redirects an allowed `target_path` to a different in-root file (which would
    decouple classification/blocklist/audit from where bytes land).
  - **Body cap is real for chunked clients:** the REST body limit is enforced by
    streaming the request and returning `413` past the cap, instead of trusting
    `Content-Length`.
  - **MCP observability tools gated:** `kb_list_pending`, `kb_audit`,
    `kb_consult`, `kb_gate_check`, and `kb_compliance` require an authenticated
    principal over MCP when auth is configured, matching the REST gating.
  - **Aggregate caps:** onboarding bootstrap rejects over `KB_MAX_BOOTSTRAP_FILES`
    (default 50), and `KB_PENDING_QUEUE_CAP` is now enforced in the pending queue.
  - **Bootstrap confidence parse:** `/api/v1/onboarding/bootstrap` returns a clean
    `400` on non-numeric confidence instead of a 500.
  - **Compliance route gated:** `GET /api/v1/compliance` now requires an
    authenticated principal when auth is configured, like the other observability
    routes (it was the one REST route still open).
  - **Atomic bootstrap:** a low-confidence bootstrap that would overflow the
    pending queue is rejected up front (and rolls back on a capacity race), so it
    never leaves a partial set of pending entries.
- Observability routes are gated when auth is configured. With `KB_AUTH_TOKEN`
  set, `GET /api/v1/pending`, `/api/v1/audit`, and `/api/v1/audit/verify` now
  require an authenticated principal (they leak target paths and agent
  identities); they stay open when no auth is configured. `bin/kb pending` and
  `kb audit` send the bearer header automatically.
- Deploy hardening for the sample Kubernetes manifests: the StatefulSet pins an
  immutable image tag (was `data-olympus:latest`), drops all Linux capabilities
  except those the root entrypoint needs before it `gosu`-drops to uid 65534,
  forbids privilege escalation, sets a `RuntimeDefault` seccomp profile, and runs
  a read-only root filesystem (writes go to the PVCs and a `/tmp` emptyDir). A new
  default-deny `NetworkPolicy` restricts ingress to the ingress controller and
  same-namespace clients, and the ingress gained cert-manager/TLS guidance. The
  release workflow no longer moves the `latest` channel on manual `edge` builds,
  so `latest` always means the last promoted release.
- The audit log is now tamper-evident. Each appended event carries an
  `event_id`, the `prev_hash` of the previous event, and its own `hash` over the
  canonical body (SHA-256, or keyed HMAC-SHA256 when `KB_AUDIT_HMAC_KEY` is set).
  Any later edit, deletion, or reordering breaks the chain. A new
  `GET /api/v1/audit/verify` route and `kb audit --verify` recompute and report
  the first broken line. Legacy unhashed lines are tolerated, so enabling the
  feature on an existing log does not retroactively flag it.
- Resource limits bound write-side abuse. New configurable caps reject oversized
  proposals before any disk side effect: `KB_MAX_TEXT_BYTES` (memory text, default
  256 KiB), `KB_MAX_POSTIMAGE_BYTES` (edit/bootstrap file, default 1 MiB), and
  `KB_MAX_BODY_BYTES` (REST request body via `Content-Length`, default 2 MiB,
  returns 413). The sliding-window rate limiter gained an optional per-IP cap
  (`KB_RATE_LIMIT_PER_IP_PER_HOUR`, default 0 = disabled) so a client cannot
  multiply its quota by varying `agent_identity`.
- Identity + capability authorization unifies MCP/REST write auth, per-agent
  policy, and a confidence clamp. A `Principal` is resolved from the
  `Authorization: Bearer` header against a registry built from `KB_AUTH_TOKEN`
  (a full-capability `operator`) and an optional `KB_AUTH_PRINCIPALS` JSON list
  of per-agent tokens with explicit capabilities (`read`, `propose`,
  `auto_commit`, `resolve`, `bootstrap`, `record_event`).
  - **MCP write tools are now authenticated.** A FastMCP middleware enforces the
    same capabilities on `kb_propose_*`, `kb_resolve_pending`,
    `kb_bootstrap_project`, and `kb_record_event` that the REST layer enforces,
    closing the gap where MCP write tools bypassed `KB_AUTH_TOKEN`.
  - **Confidence is clamped for non-privileged callers.** A principal lacking the
    `auto_commit` capability has its proposals parked as *pending* regardless of
    the client-asserted confidence, so a caller can no longer self-assert
    `confidence: 1.0` to skip operator review. REST returns 401 for anonymous and
    403 for an authenticated principal missing a capability.
  - When no auth is configured (no token, no principals) every caller is the
    fully-trusted local principal, preserving the prior trusted-local behavior.
- Path containment is now enforced uniformly across every write path. A new
  shared `safe_join_under_root()` guard rejects any proposed write whose resolved
  location escapes the per-session worktree (symlink, traversal, or absolute
  path). This closes a memory-proposal symlink-escape where a malicious KB commit
  could plant `memory/inbox` as a symlink and cause a high-confidence proposal to
  write a file outside the worktree before `git add` aborted. The edit, resolve,
  and onboarding-bootstrap paths now use the same guard. Regression tests cover
  the helper directly and each write path.
- Cleanup-plan input validation is now centralized in `kb_cleanup_plan_fn` itself,
  so both `POST /api/v1/onboarding/cleanup-plan` and the MCP `kb_cleanup_plan`
  tool are protected identically (previously only the REST route validated,
  and the MCP tool called the function directly, unvalidated). The shared
  function now rejects a non-list `local_files`, a non-object entry, a
  non-string `path`, and a non-string `content` (including an explicit
  `content: null`, which used to slip through `.get()` and crash later). It
  also enforces a per-file `max_content_bytes` cap, an aggregate `max_files`
  cap, and requires `jaccard_threshold` to be a finite number in `[0.0, 1.0]`
  (rejecting `nan`, `inf`, and out-of-range values). REST and MCP callers pass
  the existing `KB_MAX_BOOTSTRAP_FILES` / `KB_MAX_POSTIMAGE_BYTES` caps through
  to the shared function.

### Changed

- `uv run mypy src` is now green and runs as a CI gate (added `types-PyYAML`, fixed
  the source type errors). Type regressions in `src/` now fail CI; `tests/` typing
  is intentionally out of scope.
- The path-to-`(tier, category)` taxonomy now ships a deployment-neutral default
  and is configurable at deploy time, with no code change: `KB_TAXONOMY_PATH`
  (a JSON file of `[prefix, tier, category]` triples that replaces the default
  table), `KB_INDEXED_PREFIXES` (comma-separated writable top-level prefixes
  that replace the default set), and `KB_MEMORY_INBOX_PREFIX` (directory new
  memory proposals are written under, default `memory/inbox/`). `tech-stacks/<stack>/`
  is now classified dynamically as `stack:<stack>` rather than from a fixed
  allow-list. Deployments that relied on the previous built-in directory names
  must set these variables to preserve prior classification and writable paths.

### Fixed

- The enforcement hook now reports the correct per-agent identity to the consult audit. Previously every agent (Claude, Codex, Gemini) was recorded as `claude-code`, so the `kb_compliance` per-agent view was wrong for Codex and Gemini. The `kb-enforce-hook` dispatcher gained an `--agent` flag and each provider threads its own identity.
- `data-olympus lint` no longer false-greens when its file discovery matches nothing. The summary now reports how many concept files were actually linted (e.g. `0 errors across 0 files (9 linted)`), and the command exits non-zero when a bundle has no concept files to lint, so a broken or over-broad skip walk surfaces as a red CI gate instead of a silent pass. Reserved files (`index.md`, `log.md`, `template.md`) are exempt from the concept schema and so are excluded from the linted count, preventing a bundle that kept only its generated indexes from passing the guard. File discovery is now exposed as `discover_bundle_files` for direct testing.

## [0.1.0] - 2026-06-24

### Added

**Format (OKF-compatible profile)**

- Governance-grade knowledge-base format as a conformant extension of the [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf).
- Controlled frontmatter fields: `id` (stable slug), `type` (decision, standard, workflow, project, component), `status` (draft, proposed, accepted, deprecated, superseded), `tier` (T1-T4 plus freeform), and `supersedes` chain for tracing decision history.
- Bundle layout: `decisions/`, `universal/`, `tech-stacks/`, `projects/`, `workflows/`, `log.md`, plus per-prefix `index.md` generated by the `index` command.
- `SPEC.md`: format specification covering bundle layout, frontmatter schema, reserved filenames, and serving contracts.

**MCP server (single-writer, streamable HTTP)**

- Streamable-HTTP MCP server (`data-olympus-mcp`) backed by FastMCP.
- Read tools: `kb_search`, `kb_get`, `kb_list`, `kb_health`, `kb_outline`.
- Write pipeline: `kb_propose_memory`, `kb_propose_edit`, `kb_resolve_pending` with pending-confirmation flow, path blocklist (`KB_WRITE_BLOCK_TIERS`, `KB_WRITE_BLOCK_PATHS`), and structural write rules.
- Onboarding tools: `kb_onboarding_status`, `kb_bootstrap_project`.
- Audit tools: `kb_audit` with JSONL event log.
- Single-replica advisory locking and durable push queue to prevent concurrent write races.
- Per-session worktree isolation for in-flight proposed edits.
- Health endpoint with degraded/healthy state.
- Optional bearer-token authentication for the write routes via `KB_AUTH_TOKEN`. When set, `POST /api/v1/propose/memory|propose/edit|resolve/{id}|onboarding/bootstrap` require `Authorization: Bearer <token>` (constant-time compared with `hmac.compare_digest`); a missing or wrong token returns `401`. Read routes stay open. When unset (the default), there is no auth. The `bin/kb` CLI sends the header automatically (via a curl config FD, never in argv) when `KB_AUTH_TOKEN` is set. There is otherwise no built-in auth, so operators should still deploy behind a trusted network or authenticating reverse proxy. See `SECURITY.md`.

**`data-olympus` CLI**

- `data-olympus lint <bundle>`: validates bundle conformance against the format spec; exits non-zero on errors (warnings are informational only).
- `data-olympus index <bundle>`: regenerates `index.md` files for each prefix in the bundle.
- `data-olympus visualize <bundle>`: produces a self-contained Cytoscape-backed HTML graph of document relationships and cross-links (adapted from the OKF visualizer). Bundle markdown is sanitized with DOMPurify and the CDN scripts are pinned with SubResource Integrity (SRI) hashes.

**`kb` bash REST client (`bin/kb`)**

- REST client over the MCP server's HTTP API.
- Subcommands: `kb search`, `kb get`, `kb list`, `kb health`, `kb outline`.
- Write subcommands: `kb propose memory`, `kb propose edit`, `kb resolve`, `kb pending`, `kb audit`.
- Onboarding subcommands: `kb onboard status`, `kb onboard project`, `kb onboard component`, `kb onboard rename`, `kb onboarding-check`.
- Workspace auto-detection via `bin/_kb_detect_workspace.sh` (resolves workspace and component from `git remote` of the current working directory).

**Example bundle**

- `example-bundle/`: a multi-tier example with decisions (ADR-001, ADR-002), universal standards, a backend-nestjs tech-stack stub, an acme-app project stub, a workflow, and a `log.md`.
- Pre-generated `index.md` files and `viz.html` graph for the example bundle.

**Deploy**

- `deploy/docker/`: Dockerfile, `compose.yaml`, and `entrypoint.sh` for local Docker deployment.
- `deploy/k8s/`: Kustomize manifests (StatefulSet, Service, Ingress, ConfigMap, Namespace) for Kubernetes deployment.
- `deploy/k8s/secret.template.yaml`: template (not real credentials) for the SOPS-encrypted Secret.

**Documentation**

- `docs/quickstart.md`: verified local-run procedure with curl and `kb` CLI examples.
- `docs/serving.md`: single-replica serving model, read-only replicas, and git pull loop.
- `docs/adoption.md`: bring-your-own-KB guide (author, lint, index, serve, wire an agent).
- `docs/comparison.md`: how data-olympus relates to OKF, enterprise catalogs, markdown KB tools, agent-context conventions, RAG, and ADR tooling.

[Unreleased]: https://github.com/knaisoma/data-olympus/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/knaisoma/data-olympus/releases/tag/v0.1.0
