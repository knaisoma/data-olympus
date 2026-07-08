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

- **`data-olympus init <dir>` bundle scaffold command** (issue #66). Creates a
  new knowledge bundle: the tier directories (`--tiers`, default `universal,
  tech-stacks,projects,decisions,workflows,tooling`), a root `index.md`
  carrying the `spec_version`/`okf_version` frontmatter, a `template.md`
  authoring scaffold, and one example concept document per SPEC-supported
  `type` (`standard`, `decision`, `workflow`, `project`, `memory`,
  `reference`), including a real `superseded`/`superseded_by`/`supersedes`
  pair and `applies_when` trigger metadata. The generated bundle passes
  `data-olympus lint` with zero errors and zero warnings and builds cleanly
  under `data-olympus index`. Refuses to scaffold into a non-empty directory
  (no `--force` in this slice).

- **Typed lifecycle relationships: parsed, indexed, and lint-validated
  (issue #110, slice 1).** `supersedes` (scalar ID or list of IDs, normalized
  to a list at parse time so both shapes the ADR importer emits lint clean),
  `superseded_by` (scalar ID), and a new `contradicts` field (scalar ID or
  list of IDs, normalized to a list the same way as `supersedes`;
  unresolved-conflict evidence that never affects retrieval ranking) are now
  parsed into `ParsedDoc` and extracted into a new `edges` table
  (`source_id`, `rel`, `target_id`) at index-build time (schema version
  bumped 8 -> 9). `kb lint` gained a cross-file pass (an in-memory id map
  over the discovered bundle, no database) that reports: **errors** for a
  malformed field shape, self-supersession, or a supersession cycle of any
  length; **warnings** for a dangling target id, an asymmetric
  supersedes/superseded_by pair, a path-shaped target value (concept ids are
  the only stable target, never paths), `superseded_by` set on an in-force
  document, `status: superseded` with no `superseded_by`, and an in-force
  `contradicts` pair. `SPEC.md` section 4.2 documents the normalized shapes
  and the full lint severity list; the "not validated today" note is
  removed. Slice 2 (a separate change) will consume the edges table for
  in-force graph exclusion, `kb_get`/`kb_search` surfacing, and a health
  counter.

- **Secret-scanning gate on the write path (issue #71).** Every commit path
  (`kb_propose_memory` / `kb_propose_edit` auto-commit, `kb_resolve_pending`
  approve, including a resolved `edited_text`, and onboarding bootstrap) now
  scans the final postimage for credential-shaped content (PEM private-key
  blocks, GitHub/Slack tokens, AWS access key ids, generic `password=`/
  `secret=` assignments with a non-placeholder value, and connection strings
  with an inline password) before anything is written to disk. A match rejects
  the write with a distinct `rejected_secret_detected` status; nothing is
  committed and no pending entry is created on the auto-commit/bootstrap
  paths. Only the pattern name and an approximate line number are ever
  surfaced in the response, the audit event, or a log line, never the
  matched secret value. Operators can extend the built-in pattern set with
  `KB_SECRET_SCAN_EXTRA_PATTERNS` (comma-separated regexes; an invalid entry
  is logged and skipped, never crashes the server). `kb_resolve_pending` gains
  an operator-only `override_secret_scan` flag to consciously commit a
  flagged postimage that is a confirmed false positive; the override is
  recorded on the resulting audit event, and no auto-commit or bootstrap path
  exposes it.

### Fixed

- **`data-olympus index` silently regenerated zero indexes for a bundle whose
  absolute path passes through a skip-named ancestor** (found in companion
  review of the `init` scaffold). `regenerate_indexes` matched skip-directory
  names (`.git`, `.venv`, `node_modules`, ...) against the bundle's absolute
  path components instead of the bundle-relative ones, so a bundle located
  under e.g. a `.venv/` or `node_modules/` parent produced "wrote 0 index.md
  file(s)" with no error. Skip matching is now relative to the bundle root,
  matching `discover_bundle_files` in the lint pipeline.

## [0.3.5] - 2026-07-06

### Fixed

- **Ambiguous rejection reason for writes outside the deployment's indexed
  prefixes.** `kb_propose_edit` (and `kb_propose_memory`) rejected a
  well-formed, non-malicious path outside `KB_INDEXED_PREFIXES` with the same
  opaque reason string (`traversal_or_excluded` / `not_md_or_excluded`) used
  for an actual traversal or control-character attempt. A syntactically fine
  path outside the configured prefixes is an ordinary deployment-configuration
  fact, not a structural or security rejection - conflating the two sent an
  operator hunting for a nonexistent traversal bug during a company-knowledge
  KB audit session (`operator/laptop.md`, readable and indexed for search,
  came back rejected on write with no indication it was simply outside the
  deployment's writable-prefix allowlist). `rejected_path_not_indexable`
  responses (and audit events) now carry a specific reason:
  `structurally_invalid` (empty/control-chars/absolute/traversal/excluded
  segment), `not_markdown`, or `not_in_indexed_prefixes`.
- Investigated the same session's second report (a `rejected_stale_base`
  resolve appearing to "leak" its path lock) and confirmed it is **not a
  bug**: `restore_resolve` deliberately keeps the path lock held after a
  gate rejection so the operator's still-pending proposal can't be clobbered
  by a concurrent write to the same path (documented behavior, hardened
  across multiple rounds of review). `reject`ing the stale entry (which does
  release the lock) and re-proposing fresh, exactly what that session did, is
  the correct and only intended recovery for a pending entry with a
  permanently-wrong base marker - there was nothing to fix.

## [0.3.4] - 2026-07-05

### Fixed

- **Enforcement gate false-positives on prose that merely mentions a governed
  keyword, for tool calls with a known file path.** `IntentClassifier.classify`
  scanned `action_diff` (the raw Write/Edit content or Bash command text, up
  to 4000 chars) for free-text keywords like `auth`, `rls`, `tenant`, `secret`
  in addition to `intent`, for every tool call. A scratch write whose text
  simply discussed one of those words as a topic - not an actual governed
  action - tripped the pre-tool gate regardless of the real (ungoverned)
  write target, and a fresh `kb_consult` call did not clear it because the
  consult's own classification (`intent`-only) and the gate's classification
  (`intent + action_diff`) disagreed on whether the action was governed.
  Keyword matching on `action_diff` now only applies when the tool call
  carries no `action_path` (Bash commands and OpenCode's `patch`, whose
  content is the only signal available); when an `action_path` is present
  (Write/Edit/MultiEdit), classification uses the path globs and dependency
  command patterns, unchanged, so this keeps full detection for real governed
  edits (a patch touching `package.json`) without flagging unrelated prose in
  files the path globs don't consider governed. Found via a company-knowledge
  KB audit session that reproduced it live (a scratch `.txt` file containing
  "RLS tenant boundaries auth trust model" was blocked with no governed path
  or command in sight); independently reviewed via `codex exec` before
  release.
  - The audit's other finding (CX1: the pre-tool gate and the pre-commit
    `report --staged` gate resolving two different workspace keys) was
    already fixed in v0.2.0 (#64, worktree-invariant workspace key); no
    further change needed. It was still visible in that audit only because
    it predated the deployed kn-dev redeploy.

## [0.3.3] - 2026-07-05

### Fixed

- **MCP streamable-HTTP session churn under long-lived clients.** A persistent
  client (notably `opencode serve`) that holds the `GET /mcp` SSE stream open was
  being forced to reconnect on a cycle, piling up abandoned transports on the
  server and leaking event listeners on the client until it degraded. Two causes
  addressed:
  - The activity middleware now keeps a session non-idle for as long as its SSE
    stream is actually open (re-stamping every `KB_SESSION_TOUCH_INTERVAL_SEC`,
    default 30s, clamped to a third of the idle window), so the reaper evicts a
    session only after its stream has genuinely closed. With that safety in
    place, the idle-reap default (`KB_SESSION_IDLE_TIMEOUT_SEC`) drops from 1800s
    to 300s, so an abandoned handshake is cleared in minutes instead of half an
    hour.
  - The shipped ingress manifest (`deploy/k8s/ingress.yaml`) now sets a long
    `proxy-read-timeout`/`proxy-send-timeout` and disables buffering, so a proxy
    with a short default idle timeout (nginx: 60s) no longer closes the SSE
    stream and triggers the reconnect loop. See `docs/serving.md`.

## [0.3.2] - 2026-07-05

### Fixed

- **`gate/check` no longer self-DoSes multi-agent fleets with 429s.** The
  enforcement hook calls `POST /api/v1/gate/check` (and `kb_gate_check`) once per
  gated tool action for every agent, but it shared the write/consult quota
  (`KB_RATE_LIMIT_PER_HOUR`, default 100). Behind an ingress with no auth, all
  clients collapse to a single limiter bucket, so a few active agents exhausted
  the hourly quota in minutes and everything after got `429 Too Many Requests`.
  gate-check now has its own ceiling, `KB_GATE_CHECK_RATE_LIMIT_PER_HOUR`,
  defaulting to `0` (unthrottled, consistent with the read routes) since it is a
  mandatory per-action probe that does only cheap classification and no writes.
  Set a positive value for an explicit backstop. `consult` and
  `onboarding/cleanup-plan` stay throttled by `KB_RATE_LIMIT_PER_HOUR`.

## [0.3.1] - 2026-07-05

### Fixed

- **`prepare-git` initContainer is now idempotent across pod restarts.** It staged
  the deploy key with `cp /etc/git-key-mount /state/git-key` then `chmod 0400`.
  Because `/state` is a PVC, on any pod restart the `0400` (unwritable) key already
  exists and the plain `cp` fails under `set -e`, leaving the pod stuck in
  `Init:Error`. The manifest now `rm -f`s the staged key before copying (the
  `/state` dir is writable to uid 65534 via `fsGroup`). Only the first boot was
  ever exercised before; a `kubectl rollout restart` or any pod recreation hit it.

## [0.3.0] - 2026-07-03

### Added

- **Serving/ops hardening (WP3a).** Production-readiness work on the serving path,
  container, and operator docs:
  - **Readiness split from data staleness.** New `GET /readyz` (200 when the
    process is up and the index is loaded, **independent of staleness**) and
    `GET /livez` (always 200). The Kubernetes readiness probe now targets
    `/readyz` instead of `/api/v1/health`, so a git-remote outage that makes the
    KB stale no longer ejects a single-replica pod from the Service and turns
    "reads are slightly old" into a hard 503. `/api/v1/health` keeps its
    503-on-degraded contract for the `bin/kb --no-stale` flag; alert on it rather
    than probing it.
  - **Audit-log rotation with chain continuity (`KB_AUDIT_MAX_BYTES`).** Size-based
    rotation that carries the tamper-evident hash chain across files (the first
    event of a new segment links to the last hash of the previous one), so
    `verify` validates across the rotation boundary. The read path can include
    rotated segments for `--since` queries and bounds the reverse scan. Off by
    default (single-file, backward compatible; old single-file logs still verify).
  - **Proxy-header configuration (`KB_TRUSTED_PROXIES`).** Enables uvicorn
    `proxy_headers` + `forwarded_allow_ips` so the rate limiter sees the real
    client IP behind an ingress instead of collapsing every client into the proxy
    address. Off by default (X-Forwarded-For ignored) so a direct client cannot
    spoof its address.
  - **Rootless container.** The root+`gosu` entrypoint phase is removed. Deploy-key
    staging and the first-boot `/kb-main` clone move to a non-root `prepare-git`
    initContainer; the main container and initContainer both run as uid 65534 with
    `runAsNonRoot: true`, all capabilities dropped, and a read-only root FS, so the
    manifest passes the **restricted** Pod Security Standard. The base image is
    digest-pinned, the git SSH host is build-arg + runtime-env configurable
    (`KB_SSH_KEYSCAN_HOST`), Docker Compose binds `127.0.0.1:8080`, and the Ingress
    is commented out of the default kustomization (opt-in after auth is set) with a
    `deploy/k8s/README.md` note.
  - **`malformed_frontmatter` in health.** The count of docs whose front-matter was
    present but malformed at the last index build (from WP2b) is surfaced on the
    health payload. It is a warning signal and deliberately does **not** flip
    `degraded`.
  - **Operations runbook.** New `docs/operations.md` (linked from README and
    `docs/serving.md`) covering backup (git remote plus the audit chain / pending
    proposals / unpushed commits it does not cover), upgrade (image bump, taxonomy
    compatibility, index rebuilds), recovery playbooks (degraded/fetch-failed,
    history rewrite, frozen and rebase-conflict-demoted push entries, orphaned
    locks), and the health/readiness/alerting model.

- **Write-pipeline integrity core (epic #72).** This wave makes the write-safety
  claims in `SPEC.md` section 8 and `docs/serving.md` actually true; each item
  ships with regression tests, including two integration tests that drive a real
  second clone against a shared bare remote:
  - **Write serialization.** A process-wide write serializer wraps the write →
    `git add` → commit → enqueue critical section, and a per-path advisory lock —
    now SHARED between the auto-commit path and the pending queue — prevents an
    auto-commit from racing a concurrent write or landing on a path with a pending
    proposal in flight (whose later approval would clobber it). The auto-commit
    path previously took no lock, so concurrent `kb_propose_*` calls in one session
    could interleave and one thread's commit could sweep another's staged file.
  - **Non-fast-forward push recovery.** A push rejected non-FF (a second
    overlapping session moved `origin/main`) is now classified distinctly from a
    network failure: the push loop fetches + rebases the session branch onto
    `origin/main` and retries. On a rebase conflict the commit is demoted to a
    pending entry for operator resolution (with a `push_conflict_demoted` audit
    event and the queue entry removed) instead of retrying identically forever.
    Pure contention (a repeated non-FF race) is retried in-line for a bounded
    number of passes and then demoted the same way, so it never silently freezes.
  - **CAS also enforces `base_commit`.** When the base marker is a specific commit
    (not the `HEAD` sentinel), the blob the target had at that commit must equal
    the current blob, closing the bypass where a caller supplied only `base_commit`.
    If the base cannot be refreshed onto `origin/main` (remote unreachable / rebase
    conflict) while an enforceable marker was supplied, the write is rejected
    `rejected_stale_base` rather than committed against a stale base.
  - **CAS enforcement.** `base_commit` / `base_blob_sha` / `target_file_hash`
    were accepted and stored but never checked. They are now enforced at commit
    time on both the auto-commit and resolve paths: a supplied base marker that
    does not match the current content on the worktree's refreshed base is rejected
    `rejected_stale_base` without committing. No marker → behavior unchanged.
  - **Content-validation gate.** No validation ran on the write path, so malformed
    YAML, a forged/duplicate `id`, or an invalid enum value committed and pushed to
    `origin/main` — and a duplicate `id` broke every subsequent index rebuild (one
    bad write, persistent degraded state). Every postimage is now format-validated
    plus checked for a duplicate `id` — using the EFFECTIVE id the rebuild assigns
    (explicit frontmatter `id` OR the path-derived id) against BOTH the live index
    and the session worktree's committed tree, and including reserved files —
    before commit; failures are rejected `rejected_invalid_document` with
    machine-readable errors. Rendered memories pass the gate as a cheap self-check.
    The **bootstrap** path now commits its whole file bundle through the same
    serialized, per-path-locked, validated, reset-on-failure write path as the
    single-file writes (it previously wrote and committed directly, unserialized
    and ungated).
  - **Atomic pending claim.** `approve`/`reject` were get-then-remove, so two
    concurrent resolves of the same id both committed. Resolution now claims the
    entry via `os.rename` before reading (test-and-set); the loser gets
    `already_resolved` / not-found. Orphaned path locks (a crash between lock
    create and entry write) are reclaimed by the pending GC loop. The gated
    resolve path holds the claim + path lock across the CAS/validation gates and
    restores the pending entry if a gate rejects, so an operator's proposal is
    never consumed without a commit.
  - **Post-commit enqueue is recoverable and truthful.** A push-queue enqueue
    failure after a successful commit no longer turns a made commit into an
    exception path (which would drop the bootstrap in-flight guard and the resolve
    claim). Instead an in-process recovery re-enqueue is attempted so the live push
    loop still drains it, and the response `push_state` is truthful: `queued` only
    when the entry landed, else `enqueue_failed_recovery_pending` (the durable
    commit on the session branch is republished by startup `init_recovery`).
  - **Unique memory filenames.** `<date>-<slug>.md` collided for same-day,
    same-slug memories and the second auto-commit silently overwrote the first; a
    short session/body-derived uniquifier is now appended.
  - **Pending expiry audit.** Expiring a >24h pending entry now emits a
    `pending_expired` / `auto_rejected` audit event instead of rejecting silently.
  - **Ordering of side effects.** The commit message and all gates are now
    evaluated before the file is written and `git add`-ed; on any post-add failure
    the worktree is hard-reset so no staged leftover is swept into the next commit.
  - **Git identity in the shipped image.** The Docker entrypoint (and the server
    `main()` as a fallback) now set a default `GIT_AUTHOR_*`/`GIT_COMMITTER_*`
    identity (overridable via `KB_GIT_AUTHOR_NAME` / `KB_GIT_AUTHOR_EMAIL`) so a
    commit inside a fresh container no longer fails with "who are you".

- **PyPI distribution + `data-olympus setup` wizard** (issue #70). The package is
  now installable from PyPI (`uvx data-olympus`, `uv tool install data-olympus`),
  published by the release chain via GitHub Actions Trusted Publishing (OIDC, no
  API token). A new `.github/workflows/publish-pypi-reusable.yml` builds the
  sdist + wheel, runs `twine check --strict` and a wheel smoke test, and uploads
  through `pypa/gh-action-pypi-publish` to the `pypi` environment;
  `tag-release.yml` calls it off the same decided tag as the container image, and
  `.github/workflows/publish-pypi.yml` adds a PR-time dry run (build + twine
  check, no upload) plus a manual-tag fallback. The publish path is **inert until
  the operator completes the one-time pypi.org publisher setup** documented in
  `docs/releases/pypi-trusted-publishing.md`. The wheel now ships the enforcement
  machinery (`bin/kb-enforce-hook`, `_kb_enforce.py`, the OpenCode gate plugin)
  as package data so the wizard works from a clean install.
- **`data-olympus setup`** (new `src/data_olympus/setup_wizard.py` + CLI
  subcommand): an idempotent, first-run/update wizard that probes the server
  endpoint (`/api/v1/health`), detects installed agents (Claude Code, Codex,
  Gemini, OpenCode), writes each agent's MCP registration with timestamped
  backups (preferring `claude mcp add` / `codex mcp add` when the CLI is present,
  merging documented config files otherwise), offers enforcement-hook install via
  the existing provider registry, and prints a doctor summary (endpoint
  reachability, agents wired, hooks installed, installed vs latest version with
  offline-tolerant PyPI/GitHub lookup). `data-olympus setup --check` is a fully
  read-only summary and update-check path.
- `data_olympus.__version__` now reads from the installed distribution metadata
  instead of a hand-maintained literal, so it cannot drift from the packaging
  version the release chain tags on.
- **`data-olympus import` command** to migrate an existing agent-rule corpus into
  a governed draft bundle (issue #67). Supports six source kinds via `--kind`:
  - Flat rule files (`claude-md` / `agents-md` / `gemini-md` / `cursorrules`):
    heuristic splitting into candidate concepts (markdown headings first, then
    blank-line-separated bullet clusters for heading-less files). Each becomes a
    draft with stamped frontmatter (generated `<prefix>-NNN` id, `type: standard`,
    `status: draft`, tier/category from flags, title from heading, description
    from the first sentence, tags heuristically from content). The original text
    is preserved verbatim as the body.
  - `adr` (adr-tools `doc/adr/NNNN-title.md` directories): maps number+title to
    `ADR-NNNN`/title, parses the `## Status` section into `supersedes` /
    `superseded_by` references, preserves the parsed adr-tools status in a
    non-activating `source_status` field, `type: decision`.
  - `okf` bundles: normalizes alias field names into the profile, fills missing
    required fields with draft-safe defaults, and reports every inference.
  - Everything lands as `status: draft`; nothing is committed to git and nothing
    auto-activates (imported ADRs keep their original status in `source_status`
    for the reviewer). Duplicate ids are refused. The command
    writes files into the output dir and prints a human-readable and (with
    `--json`) machine-readable report: files created, sections skipped as too
    short, inferences made, and "needs human review" items. It runs the existing
    lint machinery over the output (lint-clean on the happy path) and points at
    `kb_cleanup_plan` for dedup when importing into an existing KB. Re-running
    into the same output dir is refused unless `--force` is given (no silent
    duplication or clobber). Type/status/tier vocabularies are single-sourced
    from `format/validate.py`.
- Specified `applies_when` in SPEC.md (WP4c, 0.3.0). `applies_when` is the
  highest-weight indexed field in `Index.search` (tied with `title`, ahead of
  `description` and body) and one of the three discriminating columns for the
  `abstain` signal gate, but was previously implemented and used in the
  benchmark story without ever being documented. SPEC.md section 4.2 now lists
  it as a recommended field with authoring guidance (short verb phrases in an
  agent's own vocabulary) and a worked example; `docs/adoption.md` gained a
  matching authoring section. Documented honestly: unlike `tags`, a malformed
  `applies_when` produces no `kb lint` warning today.
- Upgraded `example-bundle/` to demonstrate the format's remaining
  differentiators (WP4c, 0.3.0): a real supersession pair
  (`STD-U-003`/`STD-U-004`, `status: superseded`/`supersedes`/`superseded_by`),
  a `memory` document and a `reference` document (the two concept types the
  bundle previously lacked), and `applies_when` on the standards most likely to
  be hit mid-task. `example-bundle/index.md` and the new directory `index.md`
  files were updated to match; `uv run data-olympus lint example-bundle`
  remains zero-error.
- Quickstart lifecycle-aware retrieval demo (WP4c, 0.3.0). `docs/quickstart.md`
  gained a verified section showing default search (soft status rerank: the
  active `STD-U-004` outranks the superseded `STD-U-003` it replaced, both
  still returned) versus `in_force=true` (hard filter: `STD-U-003` excluded
  from the result set entirely), against the upgraded example bundle.
- CI doc-consistency guard (WP4c, 0.3.0): `scripts/check_doc_consistency.py`,
  wired as a new `doc-consistency-guard` CI job. Fails when the
  `type`/`status`/`tier` enum lists restated in SPEC.md or `docs/adoption.md`
  drift from `data_olympus.format.validate`'s canonical
  `TYPES`/`STATUSES`/`TIERS`, or when SPEC.md's reserved-filename list drifts
  from `validate.RESERVED`. Checks every restatement of an enum in a file
  (SPEC.md states `type`/`status` twice; both are now checked, not just the
  first), tolerant of reordering, rewrapped lines, and an Oxford "or".
- Cheap OKF minimal-field structural check (`tests/test_okf_minimal_fields.py`,
  WP4c, 0.3.0): asserts every example-bundle concept doc has non-empty
  `id`/`type` and the bundle-root `index.md` declares `okf_version`. This is a
  structural floor, not a conformance test; the module docstring states
  exactly what it does and does not prove.
- Benchmark re-cut with honest baselines and regenerated public numbers (epic
  #75, WP2c). Methodology fixes so the benchmark measures retrieval, not
  string-echo, and so every attribution is defensible:
  - **Status-aware BM25 baseline** (`bm25-status-aware`): the same BM25 ranker
    that also reads `status` frontmatter and skips superseded/deprecated docs. It
    isolates "the win is having governance metadata" (serves-stale 0.000, like
    data-olympus) from "the engine is a better ranker".
  - **Honest ranking for the strawman baselines.** `whole-dump` no longer scored
    on recall@k/NDCG/MRR (it does not rank; `ranks = False`, reported only on
    token cost / precision / order-free Contains-Gold); `grep-read` now ranks by
    query-term match count instead of alphabetical file order.
  - **De-leaked synthetic corpus.** Lifecycle answer-vocabulary ("previous",
    "current", "(old)"/"(current)") removed from doc bodies and titles; the
    lifecycle signal now lives only in `status` + the supersedes chain, and
    old/new pairs are lexically identical. Shared distractor vocabulary added.
    Remaining known leak (the `exact` topic-word echo) is documented on purpose.
  - **`serves_stale` metric** (headline lifecycle metric): fraction of
    supersession-topic queries where the retired doc reached the payload at all
    (tiebreak-independent), replacing the tiebreak-sensitive staleness rate as the
    honest signal.
  - **Bootstrap confidence intervals** (`metrics.bootstrap_mean_ci`, seeded and
    deterministic): every reported mean now carries a 95% percentile-bootstrap CI.
  - **Normalized token policy**: token cost reported both as-shipped and under a
    normalized policy (top-1 full doc per method), so the token comparison
    separates retrieval from response-payload convention.
  - **Grown governance strata** for non-degenerate CIs: 30 governing topics, 10
    supersession pairs, 31 distractor topics (trigger_covered n=30, negative n=31,
    supersession n=10, paraphrase_uncovered n=74 after enforcing FTS-token
    disjointness from gold triggers).
  - **Committed real-corpus result**: `benchmarks/real_corpus_eval.py --lexical-only`
    over the committed `example-bundle` (18 hand-authored docs, 9 paraphrase
    queries) writes a reproducible non-templated number to
    `benchmarks/real_corpus/example_bundle_result.json`.
  - **Docs-drift guard**: `docs/comparison.md` and `WHY.md` benchmark tables are
    generated from the committed result JSONs by `benchmarks/docs_tables.py`
    between `<!-- BENCH:* -->` markers; `scripts/check_benchmark_docs.py` (wired
    into CI) fails the build if any quoted number drifts.
  - All committed results (`benchmarks/results/`, `governance_results/`, the
    embeddings ablation, the real-corpus result) regenerated against the hardened
    engine. With the B1 `in_force` fix, data-olympus exact recall rises from the
    0.858 artifact to 1.000 and ALL recall to 0.582 (edging BM25's 0.572).
- Deployable `in_force` and `abstain` modes on `kb_search` (issue #68, epic #75).
  `kb_search` (MCP tool and `GET /api/v1/search`) gains two parameters:
  - `in_force: bool` HARD-filters results to the in-force status class
    (`active`/`accepted`/`approved`) before ranking, excluding
    superseded/deprecated docs entirely rather than soft-downranking them. The
    class is defined once in `format.validate.IN_FORCE_STATUSES` and shared by
    the status reranker and this filter; `Index.search` gained an `in_force`
    parameter (the single-status `status` filter is unchanged and composes with
    it). The filter also applies to the dense (embedding) candidate source.
  - `abstain: bool` applies a signal gate: a query that matches no
    discriminating column (title/tags/applies_when) returns no hits with
    `abstained: true` and `abstain_reason: "no_signal_match"` (distinct from an
    ordinary empty result). The gate logic is single-sourced in
    `data_olympus.search_gate`; the benchmark ablation now imports it.
  Fixes benchmark bug B1: `benchmarks/methods/data_olympus.py` switched from the
  `status="active"` filter (which silently dropped `accepted` gold decision docs)
  to `in_force=True`. Committed benchmark numbers, `report.md`, and
  `docs/comparison.md` are intentionally NOT re-cut here (a later package does).
- Actionable gate deny message: the BLOCKED text (and the `consult_required`
  verdict `reason`) now includes the exact workspace key, the session id (the one
  parameter an agent cannot guess), and a copy-pasteable
  `kb_consult(workspace=..., source_session=..., intent=...)` call.
  `GateCheckResponse` gained `session_id` and `workspace` echo fields so MCP
  callers see the gate key.
- Write-pipeline visibility (issue #72, Wave 0). `kb_health` and `/health` now
  report the **live** pending-queue and push-queue sizes (previously hardwired to
  zero because the counters were static attributes that were never updated), plus
  a new `push_queue_frozen` count so a stuck write path is observable.
- Startup push recovery. On boot the server scans every per-session worktree and
  re-enqueues any commit reachable from its HEAD but not from `origin/main`,
  recovering commits orphaned by a crash between `git commit` and the push-queue
  enqueue. Already-queued shas are skipped, so recovery cannot double-enqueue.
- Periodic worktree GC. A background task honors `KB_WORKTREE_IDLE_SEC` to remove
  idle per-session worktrees (deferring any with unpushed commits) so KB
  checkouts no longer accumulate one-per-session forever. GC also deletes the
  session's `kb-session/<safe_id>` branch, so a returning session can create its
  worktree again instead of hitting a fatal "branch already exists" error.


### Changed

- **BREAKING (default response shape): token-compact read-tool responses (#65).**
  The read tools `kb_search`, `kb_get`, `kb_list`, `kb_outline`, and `kb_health`
  now return a **token-compact** representation **by default**, because their
  consumer is almost always an LLM paying for every token. Measured against the
  example-bundle with a real tokenizer (tiktoken cl100k; reproduce with
  `python -m benchmarks.token_compact --tokenizer tiktoken`) this saves ~37% of
  tokens on `kb_search`, ~42% on `kb_list`, ~41% on `kb_health`, and ~6-7% on
  `kb_get` (which keeps its full body and provenance); 26.1% aggregate. What
  changed in the default shape:
  - `kb_search`: each hit is `{id, title, snippet}` plus `status` **only when the
    hit is not currently in force** (e.g. `superseded`/`deprecated`) and `type`
    when set. The `query` echo, the per-hit `path`, and the raw bm25 `score` are
    dropped; snippets are capped at 160 chars. Array order still conveys rank; to
    read a hit in full, call `kb_get(id)`.
  - `kb_get`: keeps the full `content_markdown` body (unchanged) plus
    `source_commit`/`last_modified` provenance, and trims the envelope: `path`,
    `git_remote_url`, and `last_modified_source` are dropped, and empty
    `status`/`type`/`applies_when`/`description` are omitted.
  - `kb_list`: drops per-entry `path`; omits a null `category`.
  - `kb_health`: keeps the core snapshot and omits diagnostic fields that are
    null/empty (a no-error steady state no longer emits a run of nulls).
  - `kb_outline`: already lean; its shape is unchanged.

  **Opt out** with `verbose=true` (a REST query parameter, or the `verbose`
  argument on each MCP tool), which restores the exact pre-#65 JSON shape
  byte-for-byte. The bundled `kb` CLI requests `verbose=true` automatically, so
  its output is unaffected. Any first-party or third-party consumer that parsed
  the dropped fields (`query`, per-hit `path`/`score`, the full health envelope)
  must either adopt the compact shape or pass `verbose=true`. A committed
  measurement harness (`benchmarks/token_compact.py`) reproduces the per-tool
  token table under either tokenizer (`python -m benchmarks.token_compact`
  defaults to the dependency-free `simple` splitter; add `--tokenizer tiktoken`
  for the cl100k numbers quoted above), and a tokenizer-based regression test
  (`tests/test_token_compact_budget.py`, with both a `simple` budget and a
  `tiktoken` aggregate-savings guard) fails loudly if a future change re-bloats
  the compact default.

- **MCP/REST write-surface parity (deferred WP0b items).** The MCP
  `kb_resolve_pending` tool now applies the `KB_MAX_POSTIMAGE_BYTES` `edited_text`
  cap (REST already did), and the MCP `kb_consult` / `kb_gate_check` /
  `kb_cleanup_plan` tools now apply the shared rate limiter consistently with their
  REST counterparts.

- **BREAKING (auth):** A `KB_AUTH_PRINCIPALS` entry that omits an explicit
  `capabilities` list now defaults to least privilege (`read`, `propose`) instead
  of all capabilities. Previously such an entry silently received `resolve` and
  `auto_commit`, letting an agent approve its own proposals and skip operator
  review. **Action required:** any deployment relying on the old implicit
  full-capability default must now list the capabilities it needs explicitly, e.g.
  `{"name": "operator-agent", "token": "...", "capabilities": ["read", "propose",
  "resolve", "auto_commit", "bootstrap", "record_event"]}`. The single
  `KB_AUTH_TOKEN` operator principal is unaffected and still receives all
  capabilities.
- **Search pipeline hardening (WP2b, epic #75): false-positive reduction,
  dense-channel fixes, and index/query performance.**
  - Penalized expansion backfill (finding a). Query expansion (synonym #38 +
    co-occurrence #40) previously folded derived terms into the single FTS5
    `MATCH`; bm25 has no positional preference, so a doc matching only a synonym
    got full idf weight and could outrank a doc matching the user's actual term.
    `Index.search` now matches the user's own terms in a PRIMARY pass and the
    expansion terms in a SEPARATE penalized backfill pass whose hits can only
    rank BELOW the worst primary hit. The "expansion is down-weighted" claim in
    the `query_expansion` / `cooccurrence` docstrings is now actually true.
  - Rank-class invariant (finding d). `SearchHit` gained a `rank_class`
    (`RANK_CLASS_PRIMARY` / `RANK_CLASS_BACKFILL`); the status and hybrid
    rerankers now sort by `(rank_class, score)`. This makes the documented
    "backfill hits are never reordered above primaries" invariant genuinely true
    (the previous score-only "strictly worse" floor could be lifted by an
    active-status boost or the hybrid re-normalisation).
  - Co-occurrence defaults hardened (finding b): `min_count` 2->3, `min_pmi`
    0.0->0.1, plus a corpus-size floor (`KB_COOCCURRENCE_MIN_DOCS`, default 50)
    that auto-disables the table on small corpora where PMI is noise, and a
    per-doc unique-token cap (`KB_COOCCURRENCE_MAX_DOC_TOKENS`, default 400) that
    bounds the O(n^2) pair counting so a multi-thousand-word doc is no longer a
    build-time memory cliff.
  - Gated trigram index (finding c): the `fts_trigram` secondary table (a full
    second tokenized copy of the corpus, ~2-3x index-size / build-time tax) is
    now built and populated ONLY when the trigram fallback is enabled
    (`KB_TRIGRAM_MODE=on`), not unconditionally.
  - Dense-channel intent vocabulary (finding f): the text embedded at build time
    now includes `applies_when` and `tags` (the curated intent phrases a
    paraphrase query embeds near), not just title/description/body.
- **Enforcement gate policy: the gate now clears only on an explicit
  consultation, not on any recent consult (behavioral change).** Previously the
  gate meant "an HTTP call to `/consult` happened recently in this session", so
  the per-agent installers' per-prompt auto-consults (Claude/Codex
  `UserPromptSubmit`, Gemini `BeforeAgent`) kept the ledger perpetually fresh and
  the gate only fired during autonomous stretches longer than the TTL. Consults
  now carry a `trigger`: an **explicit** consult (a deliberate `kb_consult` call,
  the default, and any old client that omits the field) clears the gate, while a
  **prompt_hook** consult (the installer auto-consults, now marked as such) is
  recorded for audit/compliance and injects rules but never clears the gate. A
  prompt-hook consult never downgrades a still-fresh explicit consult. Deployments
  that relied on the per-prompt auto-clear will now see the gate require an
  explicit `kb_consult` before governed edits. Backward compatible for old
  clients (a bare consult with no `trigger` counts as explicit). See
  `docs/enforcement.md`.
- Frozen push-queue entries (those that hit `max_attempts`) are now **skipped**
  by the retry loop instead of being retried every interval forever; the freeze
  is logged once at WARN and surfaced via `push_queue_frozen` in health. See
  `docs/serving.md` for the operator unfreeze procedure.
- The push-retry loop now drains the push queue in a thread executor (like the
  git-pull loop) rather than on the event loop, and `GitOps.push` takes a bounded
  timeout (default 60s), so a hung git remote can no longer block the loop that
  answers the readiness probe. A push timeout is classified as a retryable
  failure.
- Quickstart and adoption docs lead with the `uvx data-olympus` / `uv tool
  install` install path and the setup wizard (issue #70). The adoption guide's
  per-agent wiring section is corrected to the current registration surfaces
  (Claude Code via `claude mcp add`, not a hand-edited `~/.claude/settings.json`;
  Codex via `codex mcp add`; Gemini's `"type": "http"`; OpenCode's `mcp-remote`
  wrapper).

> Scope note: this wave makes write-path failures **visible** and recovers
> orphaned commits. It does **not** fix the non-fast-forward publication stall
> itself (rebase-retry redesign); that is tracked separately as Wave 1.


### Fixed

- **Release delivery for 0.3.0.** Two release-pipeline bugs surfaced on the first
  0.3.0 run and are fixed so the tag, image, and PyPI package publish together: the
  Docker image build now copies everything `pyproject.toml` references into the
  build context before the project-installing `uv sync` (`README.md`, `LICENSE`,
  `NOTICE`, and the `bin/` machinery force-included into the wheel); and the PyPI
  publish runs inline in `tag-release.yml` rather than through a reusable workflow,
  which PyPI trusted publishing does not reliably match through.
- **Stale auto-commit path locks are reclaimed.** A hard kill while an auto-commit
  held a per-path advisory lock left the lock on the `/state` volume with no
  in-process holder to release it, wedging that path (`rejected_path_lock_busy`)
  until manual cleanup. Locks now record an `owner_kind`; crash-orphaned
  auto-commit locks are reclaimed unconditionally at server startup (a fresh
  process holds none) and, while running, once older than
  `KB_AUTO_COMMIT_LOCK_TTL_SEC` (default 600s, far above a seconds-long commit
  critical section). The periodic reclaim runs under the same process-wide write
  serializer that `path_lock` acquire/release runs under, so a stalled holder that
  resumes cannot free the path (and let a successor re-acquire it) mid-reclaim;
  the delete is additionally inode + timestamp ownership-checked as defense in
  depth. Pre-fix legacy auto-commit locks (no `owner_kind`, `auto-commit:`
  `pending_id` prefix) are recognised too, so an upgrade on a persistent `/state`
  volume frees any old wedged path. Pending-proposal locks are never TTL-reclaimed
  (they legitimately live until the operator resolves or the entry expires).

- Search pipeline hardening (WP2b, epic #75), bug + perf fixes:
  - `Index.search` could return MORE than `limit` hits when embeddings were
    configured but no reranker was set (finding h): the dense union widened the
    pool but truncation lived only inside the reranker branch. Truncation to
    `limit` is now unconditional.
  - Vectors are batch-fetched once per build and cached as an in-memory
    id->vector matrix (finding g). The hybrid reranker previously opened one
    SQLite connection per candidate hit via `get_vector`, and `dense_candidates`
    re-read + re-deserialized the whole `doc_vectors` table on every query. The
    cache is keyed by the db file's (size, mtime) so the atomic build swap
    invalidates it transparently.
  - `Index.build` now runs a single `git log --name-only` pass for every file's
    last-commit time instead of one `git log` subprocess per file, and reads +
    parses each markdown file exactly once (previously three times: two parses
    plus a separate content read) (finding i). Per-file `(last_modified, source)`
    metadata is byte-for-byte identical.
  - Malformed front matter is no longer silently swallowed (finding j): a doc
    whose front-matter block is present but is invalid YAML (so its
    `status`/`supersedes` are dropped, quietly disabling staleness protection)
    is now logged at WARN per doc, counted, and surfaced via
    `Index.malformed_frontmatter_count` and the `malformed_frontmatter` field in
    `health()`. (Wiring this count into the `/health` degraded signal is a
    tracked follow-up.)
- Onboarding pipeline (`tools_onboarding.py`, issue WP3b). Four coherence and
  atomicity fixes, each with regression tests:
  - **`partial` is no longer a dead end.** `kb_bootstrap_project` accepted only
    `absent` and rejected `partial` with `rejected_already_onboarded`, contradicting
    the playbook that told the agent to bootstrap on `absent` OR `partial`. It now
    proceeds on `partial`, narrowing the request to only the exact
    `missing_files` paths under this workspace/component so an already-committed
    file is never overwritten and a basename collision cannot smuggle in a write
    to a different project or component; if nothing supplied fills a gap it
    rejects truthfully rather than committing an empty change. The playbook text
    now matches this behaviour.
  - **Double-bootstrap race closed.** A committed bootstrap is invisible to the
    index until it is pushed and re-pulled, so a second call in that convergence
    window passed the `absent` re-check and double-committed. A short-lived,
    self-expiring in-flight marker (`onboarding_inflight.py`) is now claimed on the
    committed path and rejects a concurrent bootstrap for the same
    workspace/component as `rejected_already_in_progress`. The playbook now
    instructs waiting for `kb_onboarding_status` to report `onboarded` before
    running `kb_cleanup_plan`.
  - **Pending bundle is now atomic.** In the low-confidence path a `PathLockBusyError`
    was silently swallowed per file, so a partially-enqueued bundle returned
    `pending_confirmation` with only the subset that fit. Lock-busy is now treated
    like queue-full: the whole bundle rolls back (zero orphan pending entries or
    held locks) and rejects as `rejected_path_locked`. Every entry of a bundle is
    stamped with a shared `bundle_id` in its metadata (the seam for a future
    bundle-aware resolve UX).
  - **Safe remote-URL injection confirmed.** The `git_remote_url` front-matter
    injection parses and re-serializes YAML with `yaml.safe_dump` (a newline-laden
    URL cannot forge keys) and its presence check is key-aware, so a URL mentioned
    only in the body no longer false-positives and suppresses injection. Regression
    tests lock both behaviours in.
- OpenCode gate (`data-olympus-gate.ts`): resolves the workspace to the main git
  worktree **basename** (matching the key every other surface records consults
  under) instead of the raw absolute path, which could never match; and passes
  the tool arguments (command/patch/content) as `action_diff`, so bash and patch
  actions are actually classified instead of the gate seeing empty everything and
  allowing.
- Enforcement hook (`bin/kb-enforce-hook`): a null `session_id` now renders as an
  empty string instead of the literal `"None"` (which became a bogus ledger key);
  the critical-path pre-tool gate now uses tight `--max-time 2 --connect-timeout 1`
  timeouts; and the pre-tool payload is parsed once (base64-framed) instead of
  spawning `python3` per field.
- Anchored the installer tool matchers (`^(...)$`) so `Bash` no longer
  substring-matches `BashOutput` (and likewise for the Codex and Gemini matchers).
- `kb enforce doctor` now also verifies the managed marker/version in the live
  settings file and that the hook dispatcher exists and is executable, and warns
  (and fails) when the dispatcher resolves inside a `.worktrees/` or
  `.claude/worktrees/` checkout, which dangles after pruning and silently fails
  open.
- Docs: `docs/enforcement.md` now states that Codex DOES gate Bash (matching the
  installer) and documents the explicit-consult gate policy honestly.
- `scripts/run-local.sh` no longer deletes an arbitrary user-supplied `$1`
  path. It previously ran `rm -rf "$KB_DIR"` unconditionally, so passing your
  own bundle's path as the first argument would silently delete it. The
  script now refuses to touch a pre-existing directory that is not its own
  default demo path and does not carry a marker file it created itself,
  printing the correct way to serve your own bundle (direct
  `data-olympus-mcp` invocation with `KB_MAIN_PATH`, per `docs/quickstart.md`
  section 6) instead. The no-argument default demo flow is unchanged.
- `data-olympus index` and `data-olympus visualize` no longer treat
  `template.md` as a concept document. Both modules maintained their own
  `{"index.md", "log.md"}` reserved-filename set, drifted from
  `format/validate.py`'s `RESERVED` (used by lint and validation), so a
  directory containing only a `template.md` was wrongly treated as holding
  concept docs: `index` generated an `index.md` for it and listed
  `template.md` as a concept entry, and `visualize` counted it as a graph
  node. Both now import the same `RESERVED` set as the rest of the format
  tooling, so `index.md`, `log.md`, and `template.md` are consistently
  reserved everywhere.


### Security

- Hardened the write and enforcement surface (issue #74). Ten fixes, each with
  regression tests:
  - **Request body caps.** The resolve, consult, gate/check, and audit/event REST
    routes read the body with an uncapped `request.json()`; they now go through the
    same `KB_MAX_BODY_BYTES` streaming cap as propose/bootstrap and return 413 on
    oversize input.
  - **`resolve.edited_text` cap.** Operator-supplied `edited_text` on approve
    became the committed postimage with no size check; it is now bounded by
    `KB_MAX_POSTIMAGE_BYTES` and rejected with a distinct
    `rejected_edited_text_too_large` status, leaving the pending entry in place.
  - **YAML frontmatter injection.** Memory frontmatter was string-concatenated, so
    a `tags` / `agent_identity` value containing a newline or `]` could forge
    reserved keys (`id` / `status` / `supersedes`); a forged duplicate `id` breaks
    every index rebuild. Frontmatter is now serialized with `yaml.safe_dump`, and
    the size cap counts the full rendered postimage (frontmatter + body), not just
    the body.
  - **Backslash / control-char path bypass.** `decisions\x.md` validated as
    `decisions/x.md` but wrote a literal root-level file outside every indexed
    prefix and invisible to `KB_WRITE_BLOCK_PATHS`. Path normalization now returns
    the canonical form and every downstream operation (classification, blocklist,
    join, `git add`, pending record) uses it; control characters (newline, CR, tab,
    NUL) in a target path are rejected. The same canonical-path handling and safe
    YAML frontmatter (with the size cap applied after remote-URL injection) now
    also cover the onboarding bootstrap path.
  - **Enforcement-plane auth + rate limiting.** `/consult`, `/gate/check`, and
    `/onboarding/cleanup-plan` were anonymous-allowed even when auth was configured
    and were unthrottled. They now require an authenticated principal when auth is
    configured (no-auth deployments are unchanged) and are subject to the shared
    rate limiter. `kb_cleanup_plan` is added to the MCP auth-required tool set so
    the MCP enforcement plane matches REST.
  - **Viewer HTML injection.** A doc body containing `</script>` could break out of
    the embedded `<script>` block and run arbitrary JS; `</` is now escaped to
    `<\/`. Fixed a substitution-ordering bug where a body containing the literal
    `__DISPLAY_NAME__` was mangled (both placeholders are now substituted in a
    single pass).
  - **Search limit clamp.** `kb_search` clamped only the upper bound, so
    `limit=-1` reached SQLite as `LIMIT -1` (unlimited full-corpus dump); it is now
    clamped to 1..100.
  - **Actionable status codes.** Malformed `since` / `limit` query params on
    `/audit` and `/compliance` now return 400 instead of an opaque 500, and resolve
    of an unknown/expired `pending_id` returns 404 (with path-traversal-shaped ids
    rejected).
  - **Rate-limiter hygiene.** The sliding-window limiter now evicts empty bucket
    keys (bounding memory under varying identities) and guards its read-modify-write
    with a lock (correct under the threadpool the REST handlers run on).


### Documentation

- Reworded OKF-compatibility claims for honesty (WP4c, 0.3.0). `README.md`,
  `SPEC.md`, `WHY.md`, and `docs/comparison.md` asserted "OKF-compatible" /
  "any OKF consumer can read a data-olympus bundle unchanged" / "every
  data-olympus bundle is a valid OKF bundle" with zero executable backing: no
  test runs a real OKF reference consumer/producer against a data-olympus
  bundle. The strongest claims are now worded as design intent ("designed to
  be readable by OKF consumers") backed by the shared structure that does
  exist by construction (directory layout, frontmatter conventions, reserved
  filenames, link model), with formal conformance testing tracked in
  [issue #82](https://github.com/knaisoma/data-olympus/issues/82). A cheap,
  non-behavioral structural floor (non-empty `id`/`type` on every example-bundle
  concept doc, `okf_version` on the bundle-root index) was added as
  `tests/test_okf_minimal_fields.py`; its docstring is explicit that this
  proves frontmatter shape, not that any OKF tool can read the bundle.

- Corrected several stale or inaccurate claims ahead of 0.3.0: `docs/adoption.md`
  now describes the actual `KB_MAIN_PATH`-ignoring behaviour of
  `scripts/run-local.sh` (pointing to the direct server invocation instead),
  and its frontmatter schema section and `supersedes`/`superseded_by`
  semantics now match `src/data_olympus/format/validate.py` and `SPEC.md`
  (concept IDs, not bundle-relative paths). `SPEC.md` no longer promises
  `kb lint` warnings for missing `supersedes`/`superseded_by`/`owner` or
  broken links, neither of which is implemented; its reserved-filename list
  in section 9 now includes `template.md`, and its `okf_version` example
  matches what `example-bundle/index.md` actually ships (`"0.1"`). `README.md`
  drops the stale "pre-release (v0.1)" status line and adds the Python 3.13+
  requirement to the quickstart, plus links to `SECURITY.md` and
  `benchmarks/README.md`. `CONTRIBUTING.md`'s documented `data-olympus lint`
  output now includes the `(N linted)` suffix the CLI actually prints.
  `CHANGELOG.md` gains the previously missing `[0.1.1]` section (the tag
  was released 2026-06-24 without a changelog entry) and its compare link.
  `docs/comparison.md`'s RAG section and "Honest weaknesses" no longer claim
  there is no embedding/semantic retrieval at all; they now describe the
  optional local-embedding hybrid (off by default) shipped in 0.2.0.
  `example-bundle/index.md` no longer claims to demonstrate "all supported
  concept types" (it has no `memory`, `reference`, or `superseded` example
  documents).

## [0.2.0] - 2026-07-03

### Added

- Content-free real-corpus retrieval eval harness (`benchmarks/real_corpus_eval.py`
  + `benchmarks/real_corpus_eval.md`). Point it at your own KB directory and a
  labeled query set to measure what the optional local-embedding hybrid (issue
  #42) adds over the lexical stack (FTS + synonym + co-occurrence expansion) on
  your corpus; it prints only aggregate metrics (recall@k, MRR, recovered /
  regressed counts) and never document text, so it is safe to run over a private
  KB. The writeup includes a worked private-corpus example and an honest reading
  positioning embeddings as opt-in rather than default-on.

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
- Optional local-embedding hybrid ranking (issue #42). Real paraphrase / synonymy
  handling via a LOCAL ONNX MiniLM-class model (no external API, no query-time
  network), blended with BM25. OFF by default (`KB_EMBEDDINGS_MODE`, default off):
  with the feature off and the `embeddings` extra uninstalled, nothing imports
  the embedding stack and the lexical product is byte-for-byte unchanged. When
  enabled, each doc is embedded at `Index.build` time into a new `doc_vectors`
  table (schema v8, float32 blobs) written into the same tmp DB and swapped
  atomically, so vectors rebuild atomically with the rest of the index. A hybrid
  reranker embeds the query once and blends NORMALISED bm25 with cosine over the
  candidate hits' stored vectors, then re-sorts. It composes as the INNER layer
  under the exact-id/exact-tag short-circuit (so an exact id/tag still wins) and
  wraps the status prior (so an active doc still outranks the superseded one it
  replaced); when disabled the reranker stack is exactly today's. If enabled but
  the extra/model is unavailable, startup fails LOUDLY with an actionable message
  rather than silently reverting to lexical-only. Config: `KB_EMBEDDINGS_MODE`
  (on/off, default off), `KB_EMBEDDINGS_MODEL` (default `BAAI/bge-small-en-v1.5`,
  384-dim), `KB_EMBEDDINGS_WEIGHT` (cosine fraction of the blend in [0, 1],
  default 0.35). The extra pulls in `fastembed` (which bundles `onnxruntime`; no
  torch); the default model is a ~33 MB quantised ONNX file fetched once at
  enable time and cached locally. Build cost: one batched local embed pass over
  the corpus at `Index.build` (seconds for a small KB, no network at query
  time); storage cost is 384 x 4 bytes (~1.5 KB) per doc in `doc_vectors`.
  Install with `uv sync --extra embeddings`.
  When enabled, `Index.search` now adds a semantic candidate SOURCE (not just a
  reranker over the FTS pool): the top dense neighbours by query-doc cosine are
  unioned into the FTS candidate pool before the hybrid blend, so a paraphrase
  with ZERO lexical overlap (e.g. querying "car" against a corpus that only says
  "automobile") is retrieved where bm25 alone returned nothing. A minimum-cosine
  threshold guards abstention: an out-of-scope query whose nearest neighbour is
  only weakly similar pulls in nothing. A dense-only candidate (absent from the
  FTS results) is scored at the pool's worst (neutral-floor) bm25 so the blend
  ranks it by its cosine component. Two new `Index` knobs (dense candidate count,
  default 10; minimum cosine, default 0.5). With embeddings OFF (the default)
  `search` is byte-for-byte pure FTS. Embeddings settings
  (`KB_EMBEDDINGS_MODE`/`MODEL`/`WEIGHT`) are now read from env ONLY in
  `load_config` and threaded into the `Index` (and its embedder) via `Config`;
  the build and query paths no longer re-read the environment, so a programmatic
  caller's `Config` values are honoured.
- Trigram fuzzy-match fallback for typos and partial identifiers (issue #41). A
  secondary FTS5 table (`fts_trigram`, `trigram` tokenizer, schema v7) is built
  into the same tmp DB and swapped atomically with the primary FTS index, so it
  rebuilds atomically. At query time it is used only as a FALLBACK: the primary
  `porter unicode61` FTS query runs first and, only when it returns at or below a
  small threshold of hits, a trigram match (an OR of the query's own quoted
  trigrams, so no FTS operator injection) backfills the results. Trigram hits are
  APPENDED after the primary hits and scored strictly worse than any primary hit,
  so an exact/primary match is never diluted or reordered by a fuzzy hit, and a
  query with good primary hits is unaffected. A query shorter than 3 chars safely
  no-ops the fallback. Opt-in and off by default (existing search behaviour is
  unchanged): `KB_TRIGRAM_MODE` (on/off, default off) and
  `KB_TRIGRAM_FALLBACK_THRESHOLD` (default 3).
- Corpus co-occurrence query expansion (embedding-free semantics, issue #40).
  At index-build time the indexer learns, per term, the top-k terms it most
  strongly co-occurs with across documents (pointwise mutual information at
  document granularity) into a bounded `related_terms` table (schema v6) built
  into the same tmp DB and swapped atomically with the FTS index. At query time
  a `query_expander` appends a term's related terms, down-weighted by appending
  them after the originals so BM25 still favours typed terms, bounded to <= 32
  terms. It composes WITH the synonym expander (issue #38) rather than replacing
  it: synonyms run first, then co-occurrence broadens the synonym-expanded set
  (`compose_expanders`). Config knobs: `KB_COOCCURRENCE_MODE` (on/off, default
  on), `KB_COOCCURRENCE_K` (default 5), `KB_COOCCURRENCE_MIN_COUNT` (default 2),
  `KB_COOCCURRENCE_MIN_PMI` (default 0.0). Stopword-like and short tokens are
  skipped so the table stays focused and build cost negligible.
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

- Enforcement consult gates now agree on a worktree-invariant workspace key. The
  pre-tool gate and the pre-commit `data-olympus report --staged` gate derived the
  workspace differently (the hook from the detector or raw cwd, the report from
  `Path.cwd().name`), so a single `kb_consult` could not clear both gates from a
  linked git worktree. Both now resolve to the main worktree's basename (the first
  non-bare entry of `git worktree list`, correct for separate-git-dir and
  bare-repo layouts): `report`'s
  default `--workspace`, and the hook's `resolve_workspace`, which now tries git
  first so it never disagrees with the report even when `KB_WORKSPACES_ROOT`
  resolves the linked worktree. A new `kb-enforce-hook resolve-workspace [dir]`
  prints the resolved key.
- `data-olympus lint` (and `discover_bundle_files`) no longer discovers zero files
  when the bundle sits under an ancestor directory named like a skip-dir (for
  example a checkout under `.worktrees/`, `.git/`, or `node_modules/`). Skip-dir
  matching now applies only to path components inside the bundle, not the absolute
  ancestor path.
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

## [0.1.1] - 2026-06-24

### Added

- `data-olympus lint` skips repo-meta directories and root-level meta files.
- CI builds and publishes a multi-arch (amd64 + arm64) container image to
  GHCR on release.

### Fixed

- Bumped `fastmcp` to 3.x to close Dependabot-reported CVEs.
- Fixed the broken OKF link (now points to the knowledge-catalog repository).

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

[Unreleased]: https://github.com/knaisoma/data-olympus/compare/v0.3.3...HEAD
[0.3.3]: https://github.com/knaisoma/data-olympus/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/knaisoma/data-olympus/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/knaisoma/data-olympus/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/knaisoma/data-olympus/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/knaisoma/data-olympus/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/knaisoma/data-olympus/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/knaisoma/data-olympus/releases/tag/v0.1.0
