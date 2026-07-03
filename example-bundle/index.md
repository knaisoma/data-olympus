---
spec_version: "0.1"
okf_version: "0.1"
---
# Acme Knowledge Bundle

An example data-olympus bundle demonstrating the full tier hierarchy (T1-T4 and
meta), several supported concept types (`standard`, `decision`, `workflow`,
`project`), and cross-links between concepts. It does not yet include
`memory` or `reference` type documents, or a `superseded` document.

See [log.md](log.md) for the change history of this bundle.

# Universal Standards (T1)

- [STD-U-001 Writing Style](universal/foundation/STD-U-001-writing-style.md) - base writing style
- [STD-U-002 Code Review](universal/foundation/STD-U-002-code-review.md) - mandatory review steps
- [STD-U-601 Secrets Handling](universal/security/STD-U-601-secrets.md) - credential safety rules

# Stack Standards (T2)

- [STD-BN-001 NestJS Module Structure](tech-stacks/backend-nestjs/STD-BN-001-module-structure.md) - NestJS directory layout

# Decisions (meta)

- [ADR-001 Use data-olympus](decisions/ADR-001-use-data-olympus.md) - adopt the format
- [ADR-002 Single-writer serving](decisions/ADR-002-single-writer-serving.md) - concurrency model

# Workflows (meta)

- [WF-001 Knowledge Base Review Flow](workflows/WF-001-review-flow.md) - propose, review, commit

# Projects

- [Acme App (T3)](projects/acme-app/README.md) - the flagship app
- [Acme App API component (T4)](projects/acme-app/components/api/README.md) - REST API component
