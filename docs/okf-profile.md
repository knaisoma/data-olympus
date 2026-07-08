# The data-olympus OKF profile

This document is the field-by-field reference for data-olympus as an [Open
Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)
(OKF) profile: which frontmatter fields are stable and lint-guarded, which are
runtime-only serving-envelope fields that never touch frontmatter, which are
experimental candidates tracked against open OKF discussions, and how each
maps to OKF baseline compatibility. It exists so an adjacent OKF implementer
(or a data-olympus operator) can compare field names and semantics without
reading the codebase, and so nobody — including us — overclaims formal OKF
conformance before [issue #82](https://github.com/knaisoma/data-olympus/issues/82)
lands an executable test against OKF reference tooling.

**This is a profile document, not a new specification.** `SPEC.md` remains the
authoritative source; where this document restates a rule, `SPEC.md` wins on
conflict. Where this document describes something still landing in v0.4.0
(marked explicitly below), treat it as a design description, not a shipped
guarantee, until the corresponding PR merges.

---

## 1. OKF baseline inherited conventions

data-olympus inherits, by construction, OKF's directory structure, frontmatter
basics, reserved filenames, link model, and unknown-key tolerance (`SPEC.md`
sections 3-5). Specifically:

- Documents are UTF-8 markdown with a YAML frontmatter block delimited by a
  bare `---` line (`SPEC.md` section 4.1).
- The reserved filenames `index.md`, `log.md`, and `template.md` are exempt
  from concept-schema validation in every directory (`SPEC.md` section 3;
  enforced as `format.validate.RESERVED` and checked first thing in
  `validate_document`).
- Links are untyped directed edges using bundle-relative or relative markdown
  hyperlink syntax; consumers MUST tolerate broken links (`SPEC.md` section 5).
- Consumers MUST tolerate unknown `type` values, unknown extra frontmatter
  keys, missing optional fields, and broken links (`SPEC.md` sections 4.1 and
  9). This is the tolerance rule every extension in this document depends on:
  it is what lets an OKF consumer silently ignore `supersedes`, `validity`,
  and every other data-olympus addition.

**On the conformance wording, deliberately careful:** `SPEC.md` section 1 says
this profile is "designed to be readable by consumers of ... OKF" and that
"formal conformance testing against the OKF reference tooling is not yet in
place." Section 11 repeats the same qualifier: the claim "rests on shared
structure by construction, not on an executable check." [Issue
#82](https://github.com/knaisoma/data-olympus/issues/82) tracks adding that
executable check. Until it lands, every "OKF-readable" or "OKF-compatible"
claim in this document (and in `README.md`/`WHY.md`) means exactly that shared
structure, and nothing stronger.

---

## 2. Stable governance extensions

These fields are shipped, schema-checked by `data-olympus lint`
(`src/data_olympus/format/lint.py` via `validate_document` and the cross-file
lifecycle pass), and part of the current `SPEC.md` (version 0.2). "Stable"
here means the field name and semantics are not expected to change shape; it
does not mean the field is required in every bundle unless stated.

| Field | Required? | Lint severity if violated | Notes |
|---|---|---|---|
| `id` | Yes | error (missing) | Stable symbolic identifier, decoupled from path (`SPEC.md` section 4.2). |
| `type` | Yes | error (missing or not in `{decision, memory, project, reference, standard, workflow}`) | Controlled vocabulary, `format.validate.TYPES`. |
| `status` | Yes | error (missing or not in `{draft, active, deprecated, superseded, proposed, accepted, rejected}`) | `format.validate.STATUSES`; see the note on issue #114 below — this field is already lint-required today, not a 0.4.0 change. |
| `tier` | Yes | error (missing or not in `{T1, T2, T3, T4, meta}`) | `format.validate.TIERS`. |
| `applies_when` | Recommended | none today (see caveat below) | Highest-weight indexed field for `kb_search`; feeds the abstention gate. Not yet in `kb lint`'s checked field set. |
| `supersedes` | Optional | error on malformed shape/self-reference/cycle; warning on dangling/asymmetric/path-shaped target | Scalar ID or list of IDs, normalized to a list (issue #110). |
| `superseded_by` | Optional | error on malformed shape/self-reference; warning on dangling/asymmetric/path-shaped target, or set while status is in-force | Scalar ID only (issue #110). |
| `contradicts` | Optional | error on malformed shape; warning on dangling/path-shaped target, or an in-force pair | Scalar ID or list of IDs; annotation only, never filters or ranks (issue #110). |
| `owner` | Optional | none (documented convention only) | Team or individual responsible for the concept. |

### `status`, and the note on issue #114

`status` is listed as a **required** field in `SPEC.md` section 4.2 today, and
`format.validate.REQUIRED = ("id", "type", "status", "tier")` has always
enforced it as a lint **error** when absent — this is not new in v0.4.0.

What v0.4.0 changes (per [issue #114](https://github.com/knaisoma/data-olympus/issues/114),
**shipping in 0.4.0**, not yet merged as of this document) is the migration
story for a corpus that already has status-less documents in it: a hard error
alone would break CI on upgrade for any bundle with legacy gaps. The
maintenance ledger (issue #113, already shipped — see
`src/data_olympus/maintenance.py`) already computes
`status_present_in_all_kb_entries` and a capped `missing_status_paths` list at
every index build, and surfaces a `missing_status` item through the
`pending_actions` CTA (`HealthResponse.pending_actions`,
`ConsultResponse.pending_actions`) when the corpus is dirty. Issue #114's
remaining scope is to make that migration the operator-facing story for the
already-existing lint error — nagging until the corpus is clean rather than
failing outright — and a `SPEC.md` version bump documenting the field's status
as normative rather than merely restating it. Treat the *lint enforcement* as
already true today, and the *migration/version-bump ceremony* as the piece
still landing.

### `applies_when`: authoring guidance

`applies_when` is a YAML list of short, verb-phrase trigger strings describing
the coding intents a `standard` or `decision` document governs (`SPEC.md`
section 4.2). It is the single highest-weight indexed field in `kb_search`
(matched and boosted alongside `title`, ahead of `description` and body text),
and it feeds the abstention gate: a query must match `title`, `tags`, or
`applies_when` before retrieval proceeds. A non-list value is silently parsed
as empty (matching `tags`' parsing), but unlike `tags`, `kb lint` does not
currently warn on a missing or malformed `applies_when` — `SPEC.md` section 9
states this explicitly as the one recommended field with no lint coverage
today.

### `supersedes` / `superseded_by` / `contradicts`: typed lifecycle edges

Shipped in [issue #110](https://github.com/knaisoma/data-olympus/issues/110)
across two slices (both merged to `release/0.4.0` as of this document):

- **Slice 1** (parse + cross-file lint): flat frontmatter fields, normalized
  to a canonical shape at parse time (`format/lint.py`'s
  `_normalize_multi_ref` / `_normalize_single_ref`). Targets must be stable
  concept IDs, never paths — `_path_shaped` warns on a `/` or `.md`-suffixed
  value. Cross-file checks run over an in-memory ID map built from the
  discovered file list (`_cross_file_lifecycle_findings`): malformed shape,
  self-supersession, and supersession cycles are errors; dangling targets,
  asymmetric pairs, `superseded_by` set on an in-force document, `status:
  superseded` with no `superseded_by`, path-shaped targets, and in-force
  `contradicts` pairs are warnings.
- **Slice 2** (executable retrieval policy): the indexer materializes both
  fields into an `edges` table. An `in_force=true` query additionally excludes
  any document that is the *target* of a `supersedes` edge whose *source*
  document is itself in force — the "in-force-source guard"
  (`format.validate.graph_excluded_ids_sql`, `is_in_force`). A mutually
  supersessive cycle is not special-cased: every in-force member independently
  satisfies the exclusion rule. `kb_get` always resolves regardless of
  graph-exclusion status and returns the union of a document's own
  `superseded_by` claim and any reverse `supersedes` edge naming it — one
  consistent computed shape (`GetResponse.superseded_by`). `contradicts` never
  filters or ranks anywhere in the retrieval path; it is annotation surfaced
  on both docs so an agent reconciles or escalates rather than silently
  picking a side.

This decision directly answers OKF issue #148's typed-relationships thread:
data-olympus keeps the flat scalar/list shape (rather than adopting a nested
`relationships:` block) until OKF settles one, precisely because the important
property — the edge is *executable retrieval policy*, not just navigation
metadata — does not require a nested shape. See section 4 below for the
conditional migration path if OKF #148 standardizes something else.

### `validity`: freshness metadata with hard expiry semantics

Shipped in [issue #107](https://github.com/knaisoma/data-olympus/issues/107),
merged to `release/0.4.0`. An optional nested object, concept-level only:

| Sub-field | Semantics | Evaluated by |
|---|---|---|
| `valid_from` | Document not yet in force before this date (`upcoming`). | `is_in_force`, `compute_freshness` |
| `valid_until` | Document expired on/after the day after this date (inclusive boundary). **Has teeth**: excluded from `in_force=true` AND every default `kb_search` result, not merely downranked. | `is_expired`, `not_expired_sql_fragment`, `in_force_sql_fragment` |
| `last_verified` | Advisory; not evaluated by any filter. | none |
| `recheck_by` | Soft staleness deadline. Past date does NOT remove the doc from search; only adds a `stale` deviation indicator. | `compute_freshness` (lint warning only) |
| `verification_source` | Free text; not evaluated by tooling. | none |

All dates normalize to ISO `YYYY-MM-DD` at index/lint time
(`normalize_validity_date`). `kb lint` treats a malformed `validity` value (an
unparsable date, or `validity` present but not a mapping) as **absent** (fail
open) while still emitting a warning — `_validity_findings` in `validate.py`.
`timestamp` (the existing recommended content-change field) is explicitly
*not* a staleness signal: `SPEC.md` section 4.2 states tooling MUST NOT derive
expiry or freshness from `timestamp` or a file's `last_modified`.

---

## 3. Runtime-only serving envelope fields

These fields are **computed at request time and never written to frontmatter**
(`SPEC.md` section 8's "runtime envelope" note). A bundle's on-disk conformance
(section 9 of `SPEC.md`) is judged entirely on frontmatter; these fields are
additive and orthogonal, visible only in served MCP/REST responses.

| Field | Where it appears | Semantics |
|---|---|---|
| `in_force` | `SearchHitModel.in_force`, `GetResponse.in_force` | Single-sourced predicate: status class AND validity window AND not-inbox AND not-graph-excluded (`format.validate.is_in_force`). Verbose responses carry it unconditionally; compact responses emit it deviation-only (`in_force: false` only). |
| `freshness` | `SearchHitModel.freshness`, `GetResponse.freshness` | One of `stale` / `expired` / `upcoming`, or omitted when fresh or the doc has no `validity` block (`compute_freshness`). |
| `superseded_by` (computed) | `SearchHitModel.superseded_by`, `GetResponse.superseded_by` | Union of the document's own frontmatter claim and any reverse `supersedes` edge naming it; omitted when empty. |
| `contradicted_by` | `GetResponse.contradicted_by` | Computed reverse of `contradicts`: every other doc whose `contradicts` names this one. `kb_get` only (not on search hits); verbose responses always carry it, compact responses emit it when non-empty (`GetResponse.compact_dump`). |
| `pending_actions` | `HealthResponse.pending_actions`, `ConsultResponse.pending_actions` | Maintenance-ledger CTA (issue #113): short `{kind, message, count}` items an agent should surface to the operator and act on only with confirmation. Omitted entirely (not an empty list) when the corpus is clean. |

Every row above is shipped and present in `src/data_olympus/models.py` at the
code state this document describes. One further envelope addition — a
per-session recap (N committed, M pending) surfaced through the existing
`pending_actions` envelope so a governed-lane demotion is never silent — is
part of [issue #112](https://github.com/knaisoma/data-olympus/issues/112),
**shipping in 0.4.0** but not yet merged; see the #112 note below for why it
is not tabled here.

`kb_consult`'s retrieval is hard-filtered to the in-force class (`SPEC.md`
section 8): the rules it returns are restricted to `IN_FORCE_STATUSES`, within
their validity window, and never a memory-inbox document, so an unreviewed
proposed memory or a retired/expired/upcoming document is never presented as a
governing rule. This is also where issue #109's decision landed: server-
rendered memories (`kb_propose_memory`) are stamped `type: memory` /
`status: proposed` so the existing status filter and deviation-only status
emission apply with no new code path, and a document under the memory-inbox
prefix is *never* in force regardless of claimed status
(`format.validate.is_inbox_path` / `memory_inbox_prefix`).

**Provenance fields** (issue #109, shipped): `ProposeMemoryRequest` /
`ProposeEditRequest` accept an optional `evidence: list[str]`, echoed on
`PendingEntry.evidence` and `AuditEvent.evidence` (redacted item-by-item by the
same scan that covers `reason`). `source_session` and `reason` were already
persisted in pending meta and are now returned by `kb_list_pending`
(`PendingEntry.source_session`, `PendingEntry.reason`).

**Note on issue #112 (governed-lane protection, shipping in 0.4.0, not yet
merged).** Per the issue design: a non-operator-confirmed write may not set or
change `status` into the in-force class, and any agent-proposed edit targeting
an already in-force document is always demoted to pending regardless of
confidence. To be explicit about the code state this document describes:
neither mechanism exists at HEAD yet — a high-confidence agent edit still
auto-commits (`operator_confirmed=False` in `tools_write.py`'s commit path)
and the write gate's status vocabulary (`write_gate._WRITE_STATUSES`) accepts
in-force values (`active`, `accepted`, `approved`) on any write, so today an
unreviewed write CAN claim in-force status. #112 closes exactly that gap.
Neither mechanism adds a new frontmatter field — the design describes a
per-write notice plus the per-session recap mentioned above, surfaced through
the existing `pending_actions` envelope, so no new row belongs in the field
tables until the recap's exact shape lands. This subsection is intentionally
small: expect a follow-up doc-consistency pass once #112 merges.

---

## 4. Experimental / candidate extensions

None of the following are implemented. They are tracked against specific OKF
ecosystem threads and are documented here so an adjacent implementer can see
where data-olympus currently stands on each axis.

- **Structured `relationships:` block** ([OKF #148](https://github.com/GoogleCloudPlatform/knowledge-catalog/issues/148)).
  Conditional: `SPEC.md` section 4.2 says a nested block (grouping
  `supersedes`/`superseded_by`/`contradicts` under one key, possibly with
  per-edge `reason`/`decided_at`/`source`) may be adopted in a future spec
  version *if* the upstream OKF discussion settles on a standard shape. Any
  such change would land as a staged migration (the existing scalar-or-list
  normalization for `supersedes` already tolerates more than one authored
  shape), not a breaking removal of the flat fields.
- **Version-keyed validity** ([OKF #159](https://github.com/GoogleCloudPlatform/knowledge-catalog/issues/159)).
  Deferred per the #107 decision comment pending that thread settling; today's
  `validity` object is concept-level only, with no per-version validity
  windows.
- **Confidence/reliability axis** ([OKF #151](https://github.com/GoogleCloudPlatform/knowledge-catalog/issues/151)).
  Deferred per the #107 decision comment. data-olympus's public position on
  this thread (see the issue #108 context links) is that lifecycle/validity
  ("is this claim currently allowed to govern behavior") is a first-class
  sibling axis to reliability/confidence ("how much do we believe this
  claim"), not a field that should be folded into one object — the two
  questions have different failure modes and a document can be simultaneously
  low-confidence and in-force, or high-confidence and retired.
- **`authority_state` / `allowed_use` enum — explicitly REJECTED.** The
  issue #109 decision considered and rejected adding a parallel
  authority/allowed-use vocabulary derived from `status`. Reasoning: a
  second, derived vocabulary that must track `status` forever is exactly the
  "two fields that can only agree or disagree" failure mode that also rules
  out `allowed_use` as a standalone field — it can drift from its source of
  truth with no structural guarantee. The decision fixed the real gaps
  (`kb_consult` in-force filtering, memory stamping, the mandatory-`status`
  direction in #114, computed `in_force`) at the root instead of adding a
  vocabulary to paper over them. `origin_provider` (constant inside
  data-olympus) and `asserted_by` (duplicate of `agent_identity`) were
  rejected for the same not-worth-a-parallel-field reasoning. The
  authority-forgery risk that motivated the original ask was explicitly split
  out of the #109 slice and assigned to governed-lane protection ([issue
  #112](https://github.com/knaisoma/data-olympus/issues/112), shipping in
  0.4.0 but not merged at the code state this document describes — see the
  #112 note in section 3 for what HEAD currently allows). The direction is a
  write-path control that prevents an unreviewed write from minting in-force
  status, rather than a frontmatter field describing the risk after the fact.

---

## 5. Per-field compatibility table

"OKF-baseline readable" means an OKF consumer that tolerates unknown keys (per
section 1's inherited rule) can safely ignore the field. Every data-olympus
extension is readable this way by construction. The table also includes the
OKF-recommended fields this profile constrains further (`type`, `title`,
`description`, `tags`, `timestamp`), so a single table answers both "will an
OKF consumer choke on this" and "what does data-olympus tooling do with it".

| Field | OKF-baseline readable? | Lint status | Consumer behavior |
|---|---|---|---|
| `id` | yes (unknown key) | error if missing | Stable cross-reference target, decoupled from path. Conformance requires an authored `id`; for a non-conformant doc with none, the reference index derives an effective id from the path (`index._derive_id_from_path`) so the doc stays addressable — a fallback for broken input, not a sanctioned authoring mode. |
| `type` | partially — OKF defines the key, not the vocabulary | error if missing/invalid | Controlled vocabulary layered on OKF's minimal `type` field. |
| `status` | yes (unknown key) | error if missing/invalid | Drives `IN_FORCE_STATUSES` class membership; absence is the #114 migration hazard. |
| `tier` | yes (unknown key) | error if missing/invalid | Scope classification; not evaluated by retrieval logic itself. |
| `title` | yes — OKF recommends it | warning if missing | Boosted in `kb_search` alongside `applies_when`; part of the abstention gate's discriminating column set. |
| `description` | yes — OKF recommends it | warning if missing | Indexed below `title`/`applies_when`; deliberately excluded from the abstention gate. |
| `applies_when` | yes (unknown key) | none (documented, not lint-checked) | Highest-weight `kb_search` field; feeds the abstention gate. |
| `tags` | partially — OKF recommends the key | warning if missing; warning if present and not a list | Faceted search; part of the abstention gate's discriminating column set. |
| `timestamp` | yes — OKF recommends it | warning if missing | Content-change metadata; MUST NOT be read as freshness. |
| `supersedes` | yes (unknown key) | error (shape/self/cycle), warning (dangling/asymmetric/path-shaped) | Extracted into the `edges` table; source of the in-force-source graph-exclusion guard. |
| `superseded_by` | yes (unknown key) | error (shape/self), warning (dangling/asymmetric/path-shaped/in-force) | Same edges table; surfaced on `kb_get`/compact hits (deviation-only). |
| `contradicts` | yes (unknown key) | error (shape), warning (dangling/path-shaped/in-force pair) | Annotation only; never filters or ranks. |
| `owner` | yes (unknown key) | none | Documented convention; no tooling reads it. |
| `validity` (and sub-fields) | yes (unknown key) | warning only (malformed value, `recheck_by` past, `valid_until` past while in-force) | Drives `in_force` and default-search exclusion for expiry; `recheck_by` drives `freshness: stale` only. |
| `in_force` (runtime) | n/a — never in frontmatter | n/a | Serving-envelope only; MUST NOT be treated as bundle content. |
| `freshness` (runtime) | n/a — never in frontmatter | n/a | Serving-envelope only. |
| `contradicted_by` (runtime) | n/a — never in frontmatter | n/a | Serving-envelope only; `kb_get` (verbose always, compact when non-empty). |
| `pending_actions` (runtime) | n/a — never in frontmatter | n/a | Serving-envelope only; `kb_health`/`kb_consult`. |

---

## 6. Relationship to adjacent projects

The fuller comparison against OKF itself, enterprise catalogs, markdown KB
tools, agent-context conventions, RAG/memory layers, and ADR tooling lives in
[`docs/comparison.md`](comparison.md); this section covers only the three
adjacent projects surfaced in the OKF ecosystem threads that motivated this
document ([issue #108](https://github.com/knaisoma/data-olympus/issues/108))
and does not duplicate that comparison's structure.

- **[`inkxel/throughline`](https://github.com/inkxel/throughline)** is a
  commit-triggered memory scaffold for a single code repo (journal, decisions,
  wiki, research folders under `knowledge/`) that exports to OKF on demand,
  validated against Google's reference tooling. It targets a different unit of
  governance than data-olympus: a per-repo, largely single-writer memory layer
  authored through a git hook, versus data-olympus's cross-project, multi-
  agent, server-enforced propose/pending/commit pipeline over a live MCP
  endpoint. Throughline's author is an active participant in the same OKF
  lifecycle/relationships threads this document tracks (OKF #148, #151) and
  has converged on a structurally similar `supersedes`/`contradicts` shape
  independently.
- **[`inkxel/dotKnowledge`](https://github.com/inkxel/dotKnowledge)** (early
  RFC, v0.0.x) is a portable "sealed bundle" format scoped to a subject
  (`person`/`org`/`brand`/`project`) with a `capsule.yaml`/`BOUNDARY` manifest
  declaring a `rises:` policy (what may be synthesized *out* of the bundle)
  and a draft "convergence protocol" for mounting multiple bundles under one
  boundary-respecting layer. It names Throughline as its reference engine.
  data-olympus has no subject-typed bundle concept and no boundary/rises
  policy; its closest analog is the write-path's structural rule set (path
  prefix allow-list, `KB_WRITE_BLOCK_TIERS`/`KB_WRITE_BLOCK_PATHS`), which
  governs what may be *written into* a bundle rather than what may be
  synthesized *out of* one — a different axis, not a competing implementation
  of the same one.
- **[`dynamicfeed/signed-okf`](https://github.com/dynamicfeed/signed-okf)**
  adds a tamper-evident, Ed25519-signed manifest (`okf.manifest.json`) over an
  OKF bundle: it hashes every file and lets a holder of the issuer's public
  key verify the bundle was signed by the claimed issuer and is unaltered.
  This is a provenance/integrity concern, orthogonal to the governance
  extensions in this document: data-olympus's write-path integrity guarantees
  come from git history and the single-writer MCP pipeline's commit
  provenance (agent identity, session, evidence — section 3 above), not from
  cryptographic signing of the bundle at rest. The two are compatible in
  principle (a data-olympus bundle could be signed with `signed-okf` tooling
  as an additional, external layer) but nothing in data-olympus verifies or
  produces a `signed-okf` manifest today.
- **Generic memory/RAG MCP servers** (Cognee, Zep/Graphiti, Mem0, Letta,
  Supermemory) are covered in depth in
  [`docs/comparison.md`](comparison.md#agent-memory--knowledge-layers-cognee-zepgraphiti-and-peers).
  The short version for this document's purposes: none of them expose an
  OKF-readable frontmatter schema or a lint-checked lifecycle vocabulary —
  they are self-mutating semantic memory, not curated, reviewed, versioned
  knowledge — so they are not directly comparable on the field-by-field axis
  this document covers.

---

## 7. Migration policy

Extension fields graduate out of this profile in one of two directions:

- **Into the core OKF spec**, if an upstream OKF discussion standardizes a
  shape data-olympus's flat fields currently approximate. The clearest
  candidate is `supersedes`/`superseded_by`/`contradicts` under OKF #148: if
  OKF adopts a structured `relationships:` block, `SPEC.md` section 4.2
  already commits to accepting the upstream shape via the same importer-style
  normalization that today accepts both a scalar and a list for `supersedes`
  (the bundled ADR importer emits a bare scalar for one entry, a list for
  more than one). A staged migration, never a breaking removal of the flat
  fields in the same release.
- **Out of "experimental" and into "stable governance extension"**, when a
  candidate in section 4 ships with lint coverage and a `SPEC.md` normative
  section, exactly as `validity` (issue #107) and the typed lifecycle edges
  (issue #110) did in this release. The rejected candidates in section 4
  (`authority_state`/`allowed_use`) do not have a graduation path: they were
  evaluated and declined, not deferred.

Format version bumps follow `SPEC.md` section 10's semver-style rule: a new
optional field or enum value is a minor bump (existing bundles unaffected); a
required-field change or removed field is a major bump requiring a staged
migration (the `status`-mandatory direction in issue #114 is the current
example — see section 2 above for why the lint enforcement is not itself
new, only its migration ceremony).

---

## See also

- [`SPEC.md`](../SPEC.md), especially sections 4 (frontmatter), 8 (serving
  model, runtime envelope), 9 (conformance), 10 (versioning), and 11
  (relationship to other formats).
- [`docs/comparison.md`](comparison.md) for the full competitive landscape.
- [`docs/enforcement.md`](enforcement.md) for the `kb_consult` gate this
  document's section 3 summarizes.
- [`docs/operations.md`](operations.md) for the maintenance ledger's operator-
  facing workflow.
- [Issue #82](https://github.com/knaisoma/data-olympus/issues/82): OKF
  conformance test tracker, referenced throughout section 1.
