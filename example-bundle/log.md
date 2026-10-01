# Bundle Change Log

Changes are listed newest-first, grouped by date.

---

## 2026-07-03 (0.3.0: lifecycle and applies_when demonstration)

Added the remaining concept types and lifecycle mechanics referenced in
SPEC.md but not previously demonstrated in this bundle: a real supersession
pair, a `memory` document, a `reference` document, and `applies_when`
retrieval-trigger metadata on the standards most likely to be hit mid-task.

New files:

- `universal/foundation/STD-U-003-commit-message-format.md` (T1 standard,
  `status: superseded`, `superseded_by: STD-U-004`)
- `universal/foundation/STD-U-004-commit-message-format.md` (T1 standard,
  `status: active`, `supersedes: STD-U-003`)
- `memory/accepted/2026-06-20-nestjs-module-naming-collision.md` (memory)
- `reference/rest-api-endpoints.md` (reference)
- `memory/index.md`, `memory/accepted/index.md`, `reference/index.md`

Changed:

- Added `applies_when` to `STD-U-001` and `STD-U-601` (existing standards)
  and to the new `STD-U-004`.
- Updated the root `index.md` to list the new sections and to state
  accurately which concept types and lifecycle features the bundle now
  demonstrates.

---

## 2026-06-24 (P3 expansion)

Added richer multi-tier content to the example bundle to demonstrate cross-links,
the full tier hierarchy (T1 through T4 and meta), and all supported concept types.

New files:

- `universal/foundation/STD-U-002-code-review.md` (T1 standard)
- `universal/security/STD-U-601-secrets.md` (T1 standard)
- `tech-stacks/backend-nestjs/STD-BN-001-module-structure.md` (T2 standard)
- `decisions/ADR-002-single-writer-serving.md` (meta decision)
- `workflows/WF-001-review-flow.md` (meta workflow)
- `projects/acme-app/components/api/README.md` (T4 project component)
- This `log.md` file.

---

## 2026-06-24 (initial bundle)

Created the initial example bundle with:

- `universal/foundation/STD-U-001-writing-style.md` (T1 standard)
- `decisions/ADR-001-use-data-olympus.md` (meta decision, accepted)
- `projects/acme-app/README.md` (T3 project)
- Generated `index.md` files and `viz.html` graph.
