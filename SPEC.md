# data-olympus Knowledge Format

**Version:** 0.1 (draft)
**Date:** 2026-06-24
**Status:** Draft

---

## 1. Motivation

Knowledge that lives in plain markdown with YAML frontmatter is version-controllable, portable, and readable by both humans and automated agents without any special runtime. A git repository becomes the primary audit trail: every change is a commit, rollback is `git revert`, and the full history ships with the bundle.

This specification is an **OKF-compatible profile**: any consumer built for Google's Open Knowledge Format can read a data-olympus bundle unchanged, because we inherit OKF's directory structure, frontmatter conventions, reserved filenames, and link model. On top of that baseline we layer **governance extensions**: a stable `id` field decoupled from path, a controlled type vocabulary, explicit `status` and `tier` fields, ADR chain links, and a single-writer MCP server that enforces propose/review/commit safety across multiple concurrent agents.

The result is a format that is portable and human-readable at rest (plain files, no database required) while supporting structured queries, multi-agent write safety, and progressive disclosure when served through the data-olympus MCP.

---

## 2. Terminology

**Bundle.** A self-contained directory tree of markdown concept files, optionally accompanied by an `index.md` per directory, a root `log.md`, and a root `index.md` carrying version metadata. A bundle may be shipped as a git repository, a tarball, or a subdirectory inside a larger repository.

**Concept.** A single markdown file that represents one unit of knowledge: a standard, a decision, a workflow, a project record, a memory entry, or a reference document.

**Concept ID.** The stable symbolic identifier stored in the `id` frontmatter field (for example, `STD-U-002` or `GDEC-017`). This is explicitly decoupled from the file path: renaming or reorganizing a file does not change its ID.

**Frontmatter.** A YAML mapping block at the very top of a markdown file, delimited by opening and closing `---` lines. The frontmatter holds all structured metadata for the concept.

**Body.** Everything in the markdown file after the closing `---` of the frontmatter block. If no frontmatter block is present, the entire file content is the body and the frontmatter is treated as an empty mapping.

**Link.** A markdown hyperlink from one concept to another using a bundle-relative or relative path. Links are untyped directed edges in the concept graph.

**Citation.** A reference to an external source (URL, paper, standard) embedded in the body. Citations are prose and are not part of the structured link graph.

**Tier.** A classification of scope for a concept: `T1` (universal), `T2` (stack-specific), `T3` (project-scoped), `T4` (component-scoped), or `meta` (tooling, audits, operator configuration). Defined in the `tier` frontmatter field.

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

The following conventions are inherited from OKF for interoperability. Any OKF consumer can read a data-olympus bundle because these are shared.

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

- `id`: stable symbolic identifier (for example, `STD-U-002` or `GDEC-017`); decoupled from path so that files can be renamed or reorganized without breaking references.
- `type`: controlled vocabulary: `standard`, `decision`, `workflow`, `project`, `memory`, `reference`. Unknown values are a validation error against this spec (see section 9), but consumers MUST tolerate them at read time.
- `status`: lifecycle state: `draft`, `active`, `deprecated`, `superseded`, `proposed`, `accepted`, `rejected`. Standards and most concepts follow draft to active to deprecated; decisions (ADRs) follow proposed to accepted to superseded, deprecated, or rejected.
- `tier`: scope classification: `T1`, `T2`, `T3`, `T4`, `meta`.

**Recommended fields** (missing values produce a warning from `kb lint`, not an error):

- `title`: short human-readable name for the concept.
- `description`: one or two sentences summarizing the concept; used in generated index files and search results.
- `tags`: a YAML list of lowercase strings for faceted search.
- `timestamp`: ISO 8601 date or datetime of last meaningful content change.
- `supersedes`: concept ID (or list of IDs) that this document replaces; enables machine-readable ADR chains.
- `superseded_by`: concept ID of the document that replaces this one; set when `status: superseded`.
- `owner`: team or individual responsible for the concept.

Note: `supersedes` and `superseded_by` are governance extensions not present in base OKF. `tags` should always be a list; a scalar value is a warning.

**Reserved-file exemption.** Files named `index.md`, `log.md`, or `template.md` in any directory are exempt from both required and recommended field validation. They may carry any frontmatter (or none at all), subject to the rules in sections 6 and 7.

### 4.3 Example concept document

```markdown
---
id: STD-U-002
type: standard
status: active
tier: T1
title: Writing style standard
description: Defines tone, length, and formatting conventions for all written
  knowledge base content across the operator's projects.
tags:
  - writing
  - style
  - standards
timestamp: "2026-05-15"
owner: platform-team
---

## Purpose

This standard defines the writing conventions that apply to all knowledge base
documents regardless of tier.

...
```

---

## 5. Cross-linking

Links between concepts use standard markdown hyperlink syntax. The preferred form is a **bundle-relative path** starting from the bundle root (for example, `/decisions/GDEC-017-tier-model.md`). Relative paths (for example, `../universal/STD-U-001.md`) are also allowed.

Links are **untyped directed edges**: the format does not define a link-type vocabulary. Semantic relationships (supersedes, implements, references) are expressed in frontmatter fields, not in link syntax.

Consumers MUST tolerate broken links. A link that points to a file that does not exist in the bundle is not a conformance error; it is an informational warning from `kb lint`.

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

- `okf_version`: the OKF spec version this bundle targets (for example, `"1.0"`).
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

- Per-path advisory locks prevent two concurrent write operations from racing on the same concept file.
- Per-session git worktrees isolate in-flight proposed edits from the live index until they are committed.
- A durable push queue serializes outbound git pushes so no write is lost if the remote is briefly unavailable.

A single shared HTTP surface, rather than N independent stdio processes, gives every agent one synchronized conversation with the server. When multiple agents each run their own stdio MCP process against the same git working tree, they race each other's worktrees and lock state. Streamable HTTP eliminates that race: all agents share the same server process and its in-process coordination.

**Read-only mirrors MAY scale horizontally.** A read-only replica that serves only `kb_search`, `kb_get`, `kb_list`, and `kb_outline` need not maintain the write pipeline and may run as many instances as needed.

The single-replica invariant applies only to the write-enabled server. Deployments that need higher read throughput should place a caching reverse proxy in front of the single write instance, or run dedicated read-only replicas that periodically pull from the main instance's git remote.

The serving transport MUST be streamable HTTP (not stdio). The MCP endpoint is `/mcp` by convention.

**Enforcement endpoints.** A server MAY expose an enforcement surface that turns the advisory KB into a gated consultation proxy for code and architectural decisions. The endpoints are:

- `POST /api/v1/consult`: record a consultation for a `(source_session, workspace)` pair and return the governing rules for the supplied intent.
- `POST /api/v1/gate/check`: return a verdict (`allow` or `consult_required`) for a pending code action, based on whether a fresh consultation is on record for the action's `(session_id, workspace)` pair.
- `GET /api/v1/compliance`: return aggregated enforcement-event counts, overall and per agent.

These three are mirrored as the `kb_consult`, `kb_gate_check`, and `kb_compliance` MCP tools. See [`docs/enforcement.md`](docs/enforcement.md) for the request bodies, configuration (`KB_CONSULT_TTL_SEC`, `KB_ENFORCE_FAIL_MODE`), and the Claude Code hook installer.

---

## 9. Conformance

**A bundle conforms to this spec when:**

1. Every `.md` file in the bundle that is not a reserved filename (`index.md`, `log.md`) contains a parseable YAML frontmatter block (a valid YAML mapping between opening and closing `---` delimiters at the top of the file).
2. Every such non-reserved concept document contains all four required fields (`id`, `type`, `status`, `tier`) with values from their respective controlled vocabularies:
   - `type`: one of `decision`, `memory`, `project`, `reference`, `standard`, `workflow`
   - `status`: one of `draft`, `active`, `deprecated`, `superseded`, `proposed`, `accepted`, `rejected`
   - `tier`: one of `T1`, `T2`, `T3`, `T4`, `meta`

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

- `error`: missing required field, invalid enum value, or YAML parse failure. Blocks CI.
- `warning`: missing recommended field, `tags` is not a list, or a broken link. Does not block CI by default.

---

## 10. Versioning

The format is versioned independently of the data-olympus package. Two version fields are declared in the bundle-root `index.md` frontmatter:

- `spec_version`: the data-olympus format spec version (this document); for example, `"0.1"`.
- `okf_version`: the OKF spec version the bundle targets; for example, `"1.0"`.

Version numbering follows semver semantics:

- **Minor version increment** (for example, `0.1` to `0.2`): backward-compatible additions. New optional or recommended fields, new allowed enum values, new conventions that existing consumers can safely ignore.
- **Major version increment** (for example, `0.x` to `1.0`, or `1.x` to `2.0`): breaking changes. Removal of fields, changes to required field semantics, or changes to the parsing model that would cause existing conformant bundles to fail validation.

The `spec_version` field is optional in bundles targeting this `0.1` draft. It becomes required at `1.0`.

---

## 11. Relationship to other formats

**OKF (Open Knowledge Format).** data-olympus is an OKF-compatible profile. An OKF consumer can read any conformant data-olympus bundle unchanged; our governance extensions (`id`, `status`, `tier`, `supersedes`, `superseded_by`, `owner`, and the write pipeline) are invisible to OKF consumers because they ignore unknown keys. The two formats are complementary: OKF defines the interoperable baseline; data-olympus adds the governance layer needed for multi-agent write safety and ADR chain tracking. The relationship is analogous to how OpenAPI is a profile of JSON Schema.

**Obsidian and Notion.** Both support markdown files with YAML frontmatter and backlink graphs. data-olympus bundles can be opened in Obsidian as a vault; the frontmatter is visible in properties panels and the body renders normally. The difference is governance: Obsidian and Notion have no concept of propose/pending/resolve write pipelines, controlled-vocabulary enforcement, or tier-based access control. data-olympus complements these tools rather than replacing them: a team may author in Obsidian and commit conformant bundles to git for the MCP server to serve.

**Metadata-as-code patterns.** data-olympus is an instance of the metadata-as-code pattern: governance metadata lives in the same repository as the content it describes, evolves through the same PR/review process, and is audited through git history. This places it in the same family as `AGENTS.md`/`CLAUDE.md` agent-context formats, ADR tooling (adr-tools, Log4brains), and Backstage TechDocs, while adding the structured frontmatter and single-writer MCP serving layer that those tools lack.
