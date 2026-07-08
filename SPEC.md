# data-olympus Knowledge Format

**Version:** 0.2
**Date:** 2026-07-08
**Status:** Stable (shipped with data-olympus v0.3.0; the format is versioned independently of the package, see section 10)

---

## 1. Motivation

Knowledge that lives in plain markdown with YAML frontmatter is version-controllable, portable, and readable by both humans and automated agents without any special runtime. A git repository becomes the primary audit trail: every change is a commit, rollback is `git revert`, and the full history ships with the bundle.

This specification is **designed to be readable by consumers of Google's Open Knowledge Format (OKF)**: it inherits OKF's directory structure, frontmatter conventions, reserved filenames, and link model. Formal conformance testing against the OKF reference tooling is not yet in place ([issue #82](https://github.com/knaisoma/data-olympus/issues/82) tracks it); the claim today rests on shared structure by construction, not on an executable check. On top of that baseline we layer **governance extensions**: a stable `id` field decoupled from path, a controlled type vocabulary, explicit `status` and `tier` fields, ADR chain links, and a single-writer MCP server that enforces propose/review/commit safety across multiple concurrent agents.

The result is a format that is portable and human-readable at rest (plain files, no database required) while supporting structured queries, multi-agent write safety, and progressive disclosure when served through the data-olympus MCP.

---

## 2. Terminology

**Bundle.** A self-contained directory tree of markdown concept files, optionally accompanied by an `index.md` per directory, a root `log.md`, and a root `index.md` carrying version metadata. A bundle may be shipped as a git repository, a tarball, or a subdirectory inside a larger repository.

**Concept.** A single markdown file that represents one unit of knowledge: a standard, a decision, a workflow, a project record, a memory entry, or a reference document.

**Concept ID.** The stable symbolic identifier stored in the `id` frontmatter field (for example, `STD-U-001` or `ADR-002`). This is explicitly decoupled from the file path: renaming or reorganizing a file does not change its ID.

**Frontmatter.** A YAML mapping block at the very top of a markdown file, delimited by opening and closing `---` lines. The frontmatter holds all structured metadata for the concept.

**Body.** Everything in the markdown file after the closing `---` of the frontmatter block. If no frontmatter block is present, the entire file content is the body and the frontmatter is treated as an empty mapping.

**Link.** A markdown hyperlink from one concept to another using a bundle-relative or relative path. Links are untyped directed edges in the concept graph.

**Citation.** A reference to an external source (URL, paper, standard) embedded in the body. Citations are prose and are not part of the structured link graph.

**Tier.** A classification of scope for a concept: `T1` (universal), `T2` (stack-specific), `T3` (project-scoped), `T4` (component-scoped), or `meta` (tooling, audits, deployment configuration). Defined in the `tier` frontmatter field.

---

## 3. Bundle structure

A bundle is a directory tree. The root directory and any subdirectory may contain:

- One or more concept files (`.md` files other than the reserved names).
- An optional `index.md` for progressive disclosure of the directory's contents.
- An optional `log.md` for the directory's change history.

The bundle may be organized into subdirectories of any depth. Subdirectory names carry semantic meaning (for example, `universal/`, `decisions/`, `projects/`) but are not prescribed by this spec. All paths are case-sensitive.

**Reserved filenames.** The filenames `index.md`, `log.md`, and `template.md` are reserved in every directory. They are exempt from the concept schema (see section 4) and MUST NOT be validated as concept documents. `index.md` and `log.md` are inherited from OKF; `template.md` is a data-olympus extension for authoring-scaffold files that are not themselves governed concepts.

**Shipping forms.** A bundle may be shipped as:

- A git repository (the canonical form; git history doubles as the audit log).
- A tarball or zip archive.
- A subdirectory inside a larger repository.

---

## 4. Concept documents and frontmatter

### 4.1 Base layer (inherited from OKF)

The following conventions are inherited from OKF for interoperability. They are designed so an OKF consumer can read a data-olympus bundle; this is not yet backed by an executable conformance test against OKF reference tooling (see section 11 and [issue #82](https://github.com/knaisoma/data-olympus/issues/82)).

- Documents are UTF-8 encoded markdown files.
- Structured metadata is expressed as a YAML frontmatter block at the top of the file, opened and closed by a bare `---` line.
- The `type` field is required (see controlled vocabulary below).
- Recommended OKF fields: `title`, `description`, `resource`, `tags`, `timestamp`.
- Reserved filenames (`index.md`, `log.md`, `template.md`) are exempt from schema validation.
- Bundle-relative cross-links are preferred (see section 5).
- Consumers MUST tolerate unknown `type` values, unknown extra keys, missing optional fields, and broken links.

### 4.2 Governance extensions

The following fields are added by this profile. OKF consumers silently ignore unknown keys, so these additions are backward-compatible.

**Required fields** (a concept document that is missing any of these does not conform):

- `id`: stable symbolic identifier (for example, `STD-U-001` or `ADR-002`); decoupled from path so that files can be renamed or reorganized without breaking references.
- `type`: controlled vocabulary: `standard`, `decision`, `workflow`, `project`, `memory`, `reference`. Unknown values are a validation error against this spec (see section 9), but consumers MUST tolerate them at read time.
- `status`: lifecycle state: `draft`, `active`, `deprecated`, `superseded`, `proposed`, `accepted`, `rejected`. Standards and most concepts follow draft to active to deprecated; decisions (ADRs) follow proposed to accepted to superseded, deprecated, or rejected.
- `tier`: scope classification: `T1`, `T2`, `T3`, `T4`, `meta`.

**Recommended fields** (missing values produce a warning from `kb lint`, not an error):

- `title`: short human-readable name for the concept.
- `description`: one or two sentences summarizing the concept; used in generated index files and search results.
- `tags`: a YAML list of lowercase strings for faceted search.
- `timestamp`: ISO 8601 date or datetime of last meaningful content change.
- `applies_when`: a YAML list of short trigger phrases describing the coding intents this document governs (see below). Recommended for `standard` and `decision` documents in particular, since these are the concepts an agent needs to retrieve mid-task.

`tags` should always be a list; a scalar value is a warning. `applies_when` is parsed the same way (a non-list value is silently treated as empty, matching `tags`' parsing), but unlike `tags`, `kb lint` does not currently emit a warning when `applies_when` is missing or malformed — it is recommended by this spec but not yet schema-checked. See section 9 for the exact set of fields `kb lint` checks.

#### `applies_when`: authoring guidance

`applies_when` is the single highest-weight indexed field in the reference implementation's full-text search: it is matched (and boosted) alongside `title`, ahead of `description` and body text. It exists to close the gap between how an agent phrases a task mid-flow and how a document's title or prose describes itself. A standard titled "Secrets Handling" is not what an agent types when it is about to write a `.env` file; `applies_when` lets the document declare the vocabulary an agent would actually use.

`applies_when` also feeds the reference implementation's abstention gate (`kb_search`'s `abstain` mode): a query is required to match at least one of `title`, `tags`, or `applies_when` before retrieval proceeds over the full document; `description` and body prose are deliberately excluded from that gate because their common words would let an out-of-scope query pass. A document with no `applies_when` (and generic tags) is harder for the gate to recognize as relevant to a real coding intent.

Guidance for authoring good `applies_when` values:

- Use short verb phrases, not keywords or full sentences: `"writing a database migration"`, not `"database"` or `"Guidance for writing a safe database migration script."`.
- Phrase each entry as the coding intent an agent is mid-task on, in the agent's own vocabulary, not the document's internal terminology.
- Prefer several narrow phrases over one broad one. A standard governing secrets handling benefits more from `"reading a .env file"`, `"committing a credential"`, and `"logging a request body"` than from a single `"security"` entry.
- Keep the list short (a handful of phrases); this is a retrieval aid, not an exhaustive index of every section in the document.

Worked example:

```yaml
---
id: STD-U-601
type: standard
status: active
tier: T1
title: Secrets Handling
description: Universal rules for handling credentials, API keys, and other secrets.
applies_when:
  - "reading a .env file"
  - "committing a credential or API key"
  - "logging a request or response body"
  - "writing a script that calls a secrets manager"
tags: [security, secrets, credentials]
timestamp: "2026-06-24"
---
```

An agent mid-task on "I need to log this API response for debugging" shares no vocabulary with the document's title ("Secrets Handling") but matches `"logging a request or response body"` directly.

#### `validity`: freshness metadata with hard expiry semantics

`validity` is an optional nested object, concept-level only (it does not apply to reserved files). It declares a document's time-bounded applicability, separately from `timestamp` (see below). All sub-fields are optional; an ISO date (`"2026-07-08"`) or an ISO datetime (optionally timezone-suffixed, including the `Z` shorthand for UTC) is accepted and normalized to a plain date at index/lint time.

- `valid_from`: the document is not yet in force before this date. A future `valid_from` marks the document `upcoming`.
- `valid_until`: the document is expired on or after the day *after* this date (the day itself is still in force: the boundary is inclusive). **Expiry has teeth**: a document past `valid_until` is excluded from `kb_search`'s in-force filter (`in_force=true`) AND from every default `kb_search` result, not merely soft-downranked. Rationale: unlike a superseded document, an expired one has no named successor to outrank it — left visible it could be the top hit and would incorrectly govern. Retrieve it anyway with `include_expired=true`, or via the `validity_state` audit facet (below); `kb_get` by id always resolves it regardless of expiry.
- `last_verified`: the date someone last confirmed the document is still accurate. Advisory; not evaluated by any filter.
- `recheck_by`: a soft staleness deadline. A `recheck_by` in the past does NOT remove the document from search — it stays in force and visible — but the reference implementation surfaces a deviation-only `stale` indicator on the hit, and `kb lint` emits a warning.
- `verification_source`: free text describing how/where the document was last verified (a review, an incident, a runbook). Not evaluated by tooling.

```yaml
validity:
  valid_from: "2026-01-01"
  valid_until: "2026-12-31"
  last_verified: "2026-06-01"
  recheck_by: "2026-09-01"
  verification_source: "Q3 security review"
```

**`timestamp` is content-change metadata, not staleness.** `timestamp` (section 4.2, recommended fields) records when the document's content last meaningfully changed; it says nothing about whether the guidance is still applicable. Tooling MUST NOT derive expiry, staleness, or freshness from `timestamp` or from a file's `last_modified` (git-commit or mtime provenance) — a document can be perfectly fresh and untouched for years, or freshly edited and already past its `valid_until`. `validity` is the only source for freshness semantics.

The reference implementation's `kb_search` accepts `include_expired` (default `false`) and a `validity_state` facet (`"expired"`, `"stale"`, or `"expiring_within:N"` for N days) for audit queries such as "what expired last month" or "what expires soon"; filtering for `"expired"` implies including expired documents regardless of `include_expired`. Compact search hits carry a deviation-only `freshness` field (`stale` / `expired` / `upcoming`), omitted when the document is fresh or has no `validity` block; `expired` only ever appears when the hit was explicitly included. See the `kb_search` and `kb_get` MCP tool descriptions for the full parameter contract.

`kb lint` treats a malformed `validity` value (an unparsable date, or `validity` present but not a mapping) as **absent** (fail open, so the document keeps its normal visibility) while still emitting a warning; see section 9.

**Decision-chain fields** (typed lifecycle relationships, validated by `kb lint` since [issue #110](https://github.com/knaisoma/data-olympus/issues/110)):

- `supersedes`: the concept ID this document replaces. Authored as either a single scalar ID string or a YAML list of ID strings; both shapes normalize to a list at parse time (the bundled ADR importer emits a bare scalar when there is exactly one entry and a list when there is more than one, so both shapes must be accepted).
- `superseded_by`: the concept ID of the document that replaces this one; a single scalar ID string. Set when `status: superseded`.
- `contradicts`: the concept IDs whose guidance conflicts with this document's, where the conflict is unresolved (as opposed to `supersedes`, which is a resolved retirement). Authored as either a single scalar ID string or a YAML list of ID strings; both shapes normalize to a list at parse time (the same normalization as `supersedes`; a list is the canonical authored form). Documents an open disagreement so an agent reconciles or escalates rather than silently picking a side; `contradicts` never removes a document from retrieval and never affects ranking.
- `owner`: team or individual responsible for the concept (not checked by `kb lint`; documented convention only).

**Targets are stable concept IDs, never paths.** A `supersedes` / `superseded_by` / `contradicts` value must be the target document's `id` field, not a file path: paths break silently on rename (see the onboarding rename machinery in the reference implementation, which exists precisely because paths are not stable identifiers), while ids are designed to survive a move. `kb lint` warns when a value looks path-shaped (contains a `/` or ends in `.md`) and suggests the fix.

`supersedes` / `superseded_by` / `contradicts` are governance extensions not present in base OKF; an OKF consumer tolerates them as unknown keys. The reference implementation's indexer extracts all three into an `(source_id, rel, target_id)` edges table at build time. `kb lint` cross-checks these fields across the whole bundle (an in-memory id map over the discovered file list, not a database) and reports:

- **Errors** (block CI): a malformed value shape (a `supersedes`/`contradicts` entry that isn't a string, or a `superseded_by` that isn't a scalar string); a document that supersedes or is superseded_by itself; a supersession cycle (following the merged `supersedes`/`superseded_by` graph, of any length: A supersedes B supersedes A, or longer).
- **Warnings** (do not block CI by default): a dangling target id (not found anywhere in the bundle); an asymmetric pair (A supersedes B but B's `superseded_by` doesn't name A, in either direction); `superseded_by` set while the document's own `status` is in the in-force class (see section 8); `status: superseded` with no `superseded_by` set; a path-shaped target value; two in-force documents that `contradicts` each other (in either direction).

A structured `relationships:` block (grouping these fields under one key, possibly with per-edge reasons) may be adopted in a future spec version if the upstream OKF discussion on relationship fields (OKF issue #148) settles on a standard shape; per-edge structured metadata (`reason` / `decided_at` / `source`) is deferred for the same reason today (a retired document's body and `description` already carry that rationale in prose). Any such change would land as a staged migration (the importer-style normalization above already tolerates more than one authored shape for `supersedes`), not a breaking removal of the flat fields documented here.

**Edges are executable retrieval policy (issue #110 slice 2).** The `edges` table populated at index-build time is consumed by `kb_search`'s `in_force=true` filter and by `kb_get` / `kb_search` surfacing:

- **Graph exclusion.** An `in_force=true` query additionally excludes any document that is the TARGET of a `supersedes` edge whose SOURCE document is itself in force (the full status-class-AND-validity-window predicate defined above in this section, not merely `status: superseded` -- see `format.validate.is_in_force`). This is the "in-force-source guard": a `draft`, expired, or already-retired document can never retire another document just by naming it in `supersedes`. This closes the "forgotten status flip" gap, where a document supersedes another whose own `status` was never updated: the target is excluded from `in_force=true` results (an `in_force=false`/default search still returns it, same as an `upcoming` document). Graph exclusion is scoped to `in_force=true` only, exactly like the `upcoming` half of the validity window.
- **Cycles are not special-cased.** A mutually-supersessive pair or longer cycle where every member is independently in force (already a `kb lint` error, section 9) results in EVERY member of the cycle being excluded from `in_force=true`: each member independently satisfies the exclusion rule as the target of an in-force source's `supersedes` edge.
- **Dangling edges exclude nothing.** An edge whose source or target id has no corresponding document is never applied and never counted (the exclusion query joins `edges` to `docs` on both ends).
- **`kb_consult` is `in_force=true`.** Because a consultation must surface only currently-governing rules, the `kb_consult` MCP tool (section 8) queries with `in_force=true` and therefore never returns a graph-excluded, not-yet-in-force, or retired document.
- **A health counter, `graph_excluded_docs`, reports the count of documents currently excluded by this rule**, alongside `malformed_frontmatter` and `malformed_validity` (section 8 / `kb_health`), so a forgotten status flip does not hide silently.
- **Retirement is explainable.** `kb_get` always resolves a document regardless of in-force/graph-exclusion status (same as it already ignores expiry) and returns `superseded_by`: the union of the document's own frontmatter claim and any reverse `supersedes` edge naming it -- ONE consistent computed shape that covers both the honest self-declared case and the forgotten-status-flip case. `kb_get` also returns `contradicts` (the document's own list) and the computed reverse `contradicted_by`. Compact `kb_search` hits carry a deviation-only `superseded_by` (omitted when the document is not superseded), computed and attached to every hit regardless of `in_force` (same pattern as `freshness`). `contradicts` is annotation only: it is NEVER applied to filtering or ranking anywhere in the retrieval path.

**Reserved-file exemption.** Files named `index.md`, `log.md`, or `template.md` in any directory are exempt from both required and recommended field validation. They may carry any frontmatter (or none at all), subject to the rules in sections 6 and 7.

### 4.3 Example concept document

```markdown
---
id: STD-U-001
type: standard
status: active
tier: T1
title: Writing style standard
description: Defines tone, length, and formatting conventions for all written
  knowledge base content across all projects in the bundle.
tags:
  - writing
  - style
  - standards
timestamp: "2026-05-15"
owner: platform-team
applies_when:
  - "writing a new document or README"
  - "drafting a commit message or PR description"
---

## Purpose

This standard defines the writing conventions that apply to all knowledge base
documents regardless of tier.

...
```

---

## 5. Cross-linking

Links between concepts use standard markdown hyperlink syntax. The preferred form is a **bundle-relative path** starting from the bundle root (for example, `/decisions/ADR-002-single-writer-serving.md`). Relative paths (for example, `../universal/STD-U-001.md`) are also allowed.

Links are **untyped directed edges**: the format does not define a link-type vocabulary. Semantic relationships (supersedes, implements, references) are expressed in frontmatter fields, not in link syntax.

Consumers MUST tolerate broken links. A link that points to a file that does not exist in the bundle is not a conformance error. The current `kb lint` does not check links at all (no warning is emitted either way); link-checking is not yet implemented.

Link targets are always other markdown files within the bundle. Links to external URLs in the body are citations (see section 2) and are not part of the concept graph.

---

## 6. Index files

Each directory in the bundle SHOULD contain an `index.md` providing a human- and agent-readable overview of that directory's contents. Index files support progressive disclosure: an agent or human reader can understand the scope of a directory without reading every concept file.

Index file conventions:

- The file is named exactly `index.md`.
- It is exempt from the concept schema (section 4.2). It does not need `id`, `type`, `status`, or `tier`.
- Its body should contain a brief description of the directory's purpose and a list or table of the concepts it contains, with short descriptions drawn from their `description` fields.
- Index files MAY be generated by `kb index`; hand-edited sections are preserved in a `## Notes` block by convention.

The **bundle-root `index.md`** (the `index.md` at the root of the bundle directory) carries the version metadata for the bundle. It MAY include the following frontmatter fields:

- `okf_version`: the OKF spec version this bundle targets (for example, `"0.1"`).
- `spec_version`: the data-olympus format spec version this bundle targets (for example, `"0.1"`).

These fields MUST NOT appear in subdirectory `index.md` files.

---

## 7. Log files

Each directory MAY contain a `log.md` recording the significant changes to the concepts in that directory. Log files are optional.

Log file conventions:

- The file is named exactly `log.md`.
- It is exempt from the concept schema (section 4.2).
- Entries are date-grouped, ordered newest-first (most recent date heading at the top).
- Each entry records what changed, which concepts were affected, and optionally which agent or human made the change.
- The format of entries is prose; there is no required structured syntax within the body.

For bundles served via git, `log.md` is a human-readable complement to `git log`, not a replacement. The git history is the authoritative audit trail; `log.md` highlights the entries that matter for the directory's readers.

---

## 8. Serving model (NORMATIVE)

A conformant write-enabled data-olympus server MUST run as a **single replica**, exposing MCP over **streamable HTTP**.

**Rationale.** The write path is intentionally single-writer:

- A process-wide write serializer wraps the write → `git add` → commit → enqueue critical section, so two concurrent write operations in the same session cannot interleave (one thread's commit sweeping another's staged file).
- Per-path advisory locks prevent two concurrent write operations from racing on the same concept file. The lock is SHARED between the auto-commit path and the pending queue: an auto-commit cannot land on a path that already has a pending proposal in flight (whose later approval would clobber it), and vice versa. An orphaned lock (a crash between lock acquisition and the pending entry write) is reclaimed by the pending GC loop.
- Committed postimages pass a content-validation gate before commit: malformed YAML frontmatter, an invalid `type`/`status`/`tier` enum value, a forged/duplicate `id` (one already used by a different path, which would break every subsequent index rebuild), and (issue #114) a **NEW** document that is missing `status` are rejected `rejected_invalid_document` rather than pushed to `origin/main`. `status` has always been a required field (section 4.2) and a `kb lint` error; this closes the gap where the write path let a brand-new status-less document through even though `kb lint` would already flag it. Editing an EXISTING status-less document (one that predates this check) is still allowed with no `status` required, so an operator can migrate a legacy corpus incrementally — see the maintenance ledger ([`docs/operations.md`](docs/operations.md) section 5) for the migration vehicle that tracks the backlog. Reserved filenames and memory-inbox documents (which are always server-stamped with `status: proposed`) are exempt from this check, consistent with their existing exemption from the enum check above.
- Optimistic concurrency: when a caller supplies a base marker (`base_commit` / `base_blob_sha` / `target_file_hash`) the server refreshes the session worktree's base onto `origin/main` and compares; a stale base is rejected `rejected_stale_base` without committing. When no marker is supplied behavior is unchanged.
- Per-session git worktrees isolate in-flight proposed edits from the live index until they are committed.
- A durable push queue serializes outbound git pushes so no write is lost if the remote is briefly unavailable. A push rejected non-fast-forward (a second overlapping session moved `origin/main`) triggers a fetch + rebase of the session branch and a retry; if the rebase conflicts, the commit is demoted to a pending entry for operator resolution (with a `push_conflict_demoted` audit event) rather than retrying forever.

A single shared HTTP surface, rather than N independent stdio processes, gives every agent one synchronized conversation with the server. When multiple agents each run their own stdio MCP process against the same git working tree, they race each other's worktrees and lock state. Streamable HTTP eliminates that race: all agents share the same server process and its in-process coordination.

**Read-only mirrors MAY scale horizontally.** A read-only replica that serves only `kb_search`, `kb_get`, `kb_list`, and `kb_outline` need not maintain the write pipeline and may run as many instances as needed.

The single-replica invariant applies only to the write-enabled server. Deployments that need higher read throughput should place a caching reverse proxy in front of the single write instance, or run dedicated read-only replicas that periodically pull from the main instance's git remote.

The serving transport MUST be streamable HTTP (not stdio). The MCP endpoint is `/mcp` by convention.

**Readiness probes MUST NOT target `/api/v1/health`.** A load balancer or orchestrator readiness check MUST use `GET /readyz` (200 once the process is up and the index is loaded, independent of data staleness). `/api/v1/health` keeps a 503-on-degraded contract for data-freshness alerting; wiring it as a readiness probe would eject a healthy single replica from its Service on a transient git-remote staleness. See [`docs/serving.md`](docs/serving.md).

**Enforcement endpoints.** A server MAY expose an enforcement surface that turns the advisory KB into a gated consultation proxy for code and architectural decisions. The endpoints are:

- `POST /api/v1/consult`: record a consultation for a `(source_session, workspace)` pair and return the governing rules for the supplied intent.
- `POST /api/v1/gate/check`: return a verdict (`allow` or `consult_required`) for a pending code action, based on whether a fresh consultation is on record for the action's `(session_id, workspace)` pair.
- `GET /api/v1/compliance`: return aggregated enforcement-event counts, overall and per agent.

These three are mirrored as the `kb_consult`, `kb_gate_check`, and `kb_compliance` MCP tools. See [`docs/enforcement.md`](docs/enforcement.md) for the request bodies, configuration (`KB_CONSULT_TTL_SEC`, `KB_ENFORCE_FAIL_MODE`), and the Claude Code hook installer.

**Only an explicit-trigger consultation satisfies the gate.** `POST /api/v1/gate/check` returns `allow` only when a fresh `POST /api/v1/consult` is on record for the action's `(session_id, workspace)` pair. A read-only KB search or any other tool call does NOT count as a consultation and does NOT clear the gate; the agent MUST issue an explicit `kb_consult` (or the REST equivalent) whose freshness is bounded by `KB_CONSULT_TTL_SEC`. See [`docs/enforcement.md`](docs/enforcement.md).

**`kb_consult` retrieval is hard-filtered to the in-force class.** The rules `kb_consult` returns for a governed intent are restricted to the in-force class (section 4.2's `IN_FORCE_STATUSES`, within their validity window, and never a memory-inbox document — see the runtime envelope note below), so an unreviewed proposed memory, a retired/superseded/rejected document, or an expired/upcoming document is never presented as a governing rule.

**Runtime envelope: computed `in_force` is a serving-layer derivation, never frontmatter.** A served `kb_get` response or `kb_search` hit MAY carry a computed `in_force: bool` field: the single-sourced predicate (status class AND validity window AND not-under-the-memory-inbox-prefix AND not-graph-excluded per section 4.2's supersession-graph rule) evaluated at request time against the server's current clock. Verbose responses carry it unconditionally; compact responses emit it deviation-only (`in_force: false` only when the document is not in force, since the compact `status` field reflects raw frontmatter and would otherwise show a forged in-force-class status with no correcting signal). This field is NEVER written to a document's frontmatter and MUST NOT be treated as bundle content — it exists only in the served response envelope, alongside `freshness` and the other derived fields section 4.2 already documents. A bundle's on-disk conformance (section 9) is judged entirely on frontmatter; the runtime envelope is additive and orthogonal.

---

## 9. Conformance

**A bundle conforms to this spec when:**

1. Every `.md` file in the bundle that is not a reserved filename (`index.md`, `log.md`, `template.md`) contains a parseable YAML frontmatter block (a valid YAML mapping between opening and closing `---` delimiters at the top of the file).
2. Every such non-reserved concept document contains all four required fields (`id`, `type`, `status`, `tier`) with values from their respective controlled vocabularies:
   - `type`: one of `decision`, `memory`, `project`, `reference`, `standard`, `workflow`.
   - `status`: one of `draft`, `active`, `deprecated`, `superseded`, `proposed`, `accepted`, `rejected`.
   - `tier`: one of `T1`, `T2`, `T3`, `T4`, `meta`.

**Validation rules for consumers:**

- Consumers MUST tolerate unknown `type` values encountered at read time (forward compatibility with future spec versions).
- Consumers MUST tolerate unknown extra keys in frontmatter (future governance extensions).
- Consumers MUST tolerate missing optional and recommended fields.
- Consumers MUST tolerate broken links (links to non-existent files).
- A frontmatter block that contains a YAML syntax error is a parse failure; the document is non-conformant and MUST be reported as an error by `kb lint`.

**`kb lint` skips the following paths automatically:**

- **Vendor/VCS/meta directories:** `.git`, `__pycache__`, `.venv`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `node_modules`, `.github`, `.worktrees`, `archive`, `_archive`, `to-delete`, `test-fixtures`, `cli-fixtures`. Any `.md` file whose path passes through one of these directory names is not validated.
- **Root-level repo-meta files:** `README.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `CHANGELOG.md`, `NOTICE.md`, `LICENSE.md`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` — **only when they sit directly at the bundle root**. The same filename in a subdirectory (e.g. `projects/acme-app/README.md`) is a legitimate concept document and is still validated.

**`kb lint` severity levels:**

- `error`: missing required field, invalid enum value, YAML parse failure, a malformed `supersedes`/`superseded_by`/`contradicts` value shape, a document that supersedes or is superseded_by itself, or a supersession cycle (see section 4.2). Blocks CI.
- `warning`: missing recommended field (`title`, `description`, `tags`, `timestamp`), `tags` is not a list, or one of the three `validity` findings below. A dangling `supersedes`/`superseded_by`/`contradicts` target id, an asymmetric supersession pair, a path-shaped target value, `superseded_by` set on an in-force document, `status: superseded` with no `superseded_by`, or an in-force `contradicts` pair (see section 4.2). Does not block CI by default. Broken links and missing/malformed `owner` are not checked and produce no finding either way (see sections 4.2 and 5). `applies_when` is recommended by this spec but is not yet in `kb lint`'s checked field set: a missing or malformed `applies_when` produces no finding today, even though a non-list value is silently parsed as empty (matching `tags`' parsing, minus the warning).

**`kb lint` validity findings** (always `warning`, never `error` — these are wall-clock-relative checks, and an error would make CI flake purely with the passage of time):

- `recheck_by` is in the past.
- `valid_until` is in the past while `status` is in the in-force class (`active`/`accepted`/`approved`): the safety net for a typo'd date that would otherwise silently remove a rule from `kb_search` discovery.
- A `validity` value is malformed (an unparsable date, or `validity` present but not a mapping). The malformed value is treated as absent (fail open) for indexing/search purposes.

---

## 10. Versioning

The format is versioned independently of the data-olympus package. Two version fields are declared in the bundle-root `index.md` frontmatter:

- `spec_version`: the data-olympus format spec version (this document); for example, `"0.1"`.
- `okf_version`: the OKF spec version the bundle targets; for example, `"0.1"`.

Version numbering follows semver semantics:

- **Minor version increment** (for example, `0.1` to `0.2`): backward-compatible additions. New optional or recommended fields, new allowed enum values, new conventions that existing consumers can safely ignore.
- **Major version increment** (for example, `0.x` to `1.0`, or `1.x` to `2.0`): breaking changes. Removal of fields, changes to required field semantics, or changes to the parsing model that would cause existing conformant bundles to fail validation.

This document is now at `0.2`, incrementing from `0.1`: the addition of the optional `validity` frontmatter object plus the typed lifecycle-relationship validation of `supersedes`/`superseded_by`/`contradicts` (both section 4.2) are backward-compatible minor changes — existing bundles without these fields are unaffected, and an OKF or pre-`0.2` consumer silently ignores the unknown keys per section 4.1's forward-compatibility rule. The one **behavior** change accompanying the `validity` addition lives in the reference implementation, not the format itself: a document past its `valid_until` is now excluded from default `kb_search` results (previously the reference implementation had no `validity` concept at all, so nothing was ever excluded on this basis).

The `spec_version` field is optional in bundles targeting this `0.1` draft. It becomes required at `1.0`.

**Issue #114 (write-path `status` enforcement) does NOT bump this version.** `status` has been a required field under section 4.2 — and a `kb lint` error when absent — since the `0.1` draft; no prior version of this document ever described it as recommended, so there is no required-field-semantics change to record here. What issue #114 adds is a reference-implementation write-path check (section 8): a NEWLY created document missing `status` is now rejected at commit time, closing a gap where the write path was more permissive than `kb lint`. This does not affect any already-conformant bundle (which by definition already has `status` on every document) and does not change how an existing bundle is read, parsed, or validated — only how a brand-new document is admitted through the write path. Per the minor/major criteria above, that is neither: it is a reference-implementation enforcement-timing change, not a format change, so it is documented here rather than as a version increment. A bundle that is already non-conformant (missing `status` on one or more legacy documents) is unaffected in how it is READ — those documents keep being served, just never as in-force — see the maintenance ledger migration path in [`docs/operations.md`](docs/operations.md).

---

## 11. Relationship to other formats

**OKF (Open Knowledge Format).** data-olympus is designed to be readable by OKF consumers: our governance extensions (`id`, `status`, `tier`, `supersedes`, `superseded_by`, `contradicts`, `owner`, and the write pipeline) are meant to be invisible to OKF consumers because OKF requires tolerating unknown keys. This is a design intent backed by shared structure (directory layout, frontmatter conventions, reserved filenames, link model), not by an executable conformance test against the OKF reference tooling; [issue #82](https://github.com/knaisoma/data-olympus/issues/82) tracks adding one. The two formats are complementary: OKF defines the interoperable baseline; data-olympus adds the governance layer needed for multi-agent write safety and ADR chain tracking. The relationship is analogous to how OpenAPI is a profile of JSON Schema. See [`docs/okf-profile.md`](docs/okf-profile.md) for the full field-by-field profile: which extensions are stable, which are runtime-only serving fields, and which are experimental candidates tracked against open OKF ecosystem discussions.

**Obsidian and Notion.** Both support markdown files with YAML frontmatter and backlink graphs. data-olympus bundles can be opened in Obsidian as a vault; the frontmatter is visible in properties panels and the body renders normally. The difference is governance: Obsidian and Notion have no concept of propose/pending/resolve write pipelines, controlled-vocabulary enforcement, or tier-based access control. data-olympus complements these tools rather than replacing them: a team may author in Obsidian and commit conformant bundles to git for the MCP server to serve.

**Metadata-as-code patterns.** data-olympus is an instance of the metadata-as-code pattern: governance metadata lives in the same repository as the content it describes, evolves through the same PR/review process, and is audited through git history. This places it in the same family as `AGENTS.md`/`CLAUDE.md` agent-context formats, ADR tooling (adr-tools, Log4brains), and Backstage TechDocs, while adding the structured frontmatter and single-writer MCP serving layer that those tools lack.
