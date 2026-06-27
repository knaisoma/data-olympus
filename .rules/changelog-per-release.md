# Rule: a changelog entry is mandatory for every release

Status: active
Since: 2026-06-27
Applies to: the data-olympus product (this repository)

## Rule

Every release of data-olympus MUST ship with a changelog entry in
[`CHANGELOG.md`](../CHANGELOG.md) describing the important functional changes in
that release. No release may be tagged without one.

"Functional change" means anything that changes observable behaviour for a user
or an integrating agent:

- new, changed, or removed CLI commands or flags
- new, changed, or removed MCP tools or REST endpoints, or their inputs/outputs
- changes to the bundle format, frontmatter schema, or serving contracts
- changes to enforcement, gating, or write-pipeline behaviour
- security-relevant changes
- bug fixes that change observable behaviour

Pure internal refactors, test-only changes, and CI tweaks that produce no
user-visible difference do not require an entry, but are not harmful to record.

## How it is satisfied (mechanics)

The project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The
mandate is satisfied continuously, not at the last minute:

- Every PR that makes a functional change MUST add or update an entry under the
  topmost `## [Unreleased]` block in `CHANGELOG.md`, under the correct
  `Added` / `Changed` / `Fixed` / `Removed` / `Security` / `Deprecated` heading.
- Cutting a release renames the `[Unreleased]` block to the version and date and
  opens a fresh empty `[Unreleased]` block above it. The release therefore
  inherits the entries accrued during the cycle, guaranteeing the release-level
  mandate is met by construction.
- The release date on the `[Unreleased]` heading is never hand-edited; it is set
  only at the moment the block is renamed to a version.

## Enforcement

- Contributor-facing: the PR checklist in [`CONTRIBUTING.md`](../CONTRIBUTING.md)
  lists the changelog update as a required item.
- Planned CI gate (tracked, not yet built): a workflow step that fails a PR which
  touches functional code paths (`src/`, `bin/`, REST/MCP surfaces, `SPEC.md`)
  without modifying the `[Unreleased]` block of `CHANGELOG.md`. This turns the
  rule from advisory into enforced, consistent with the product's own thesis that
  governance should be gated rather than trusted to goodwill.

## Why

data-olympus is a governance product. A release with no record of what
functionally changed is exactly the kind of undocumented decision the product
exists to prevent. The changelog is the human-readable counterpart to the git-
native decision history the KB format already provides.
