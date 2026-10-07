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
- On `release/new`, retain `[Unreleased]` and do not commit a target version.
  The computed release version and date belong in generated release notes and
  the `release: X.Y.Z` squash record, grouped by breaking changes, features,
  fixes and other changes with issue/PR links and migration guidance. Generate
  them from the same parsed commit set used for version computation.
- Do not edit changelog or version files while squashing: the squash tree must
  equal reviewed `H`. Any later archival update to `CHANGELOG.md` goes through
  a reviewed PR on the recut branch, preserving that proof.
- Until the first new-model release, transitional release preparation may
  rename `[Unreleased]` to the computed version and date and open a fresh
  `[Unreleased]` block. Do that before final review, never after it.

## Enforcement

- Contributor-facing: the PR checklist in [`CONTRIBUTING.md`](../CONTRIBUTING.md)
  lists the changelog update as a required item.
- CI runs `scripts/check_changelog.py` for functional paths (`src/`, `bin/`,
  `deploy/`, `SPEC.md`). Its current check requires `CHANGELOG.md` in the changed
  paths; review verifies that the entry is under `[Unreleased]`. Documentation
  alone does not require an entry. The guard's `no-changelog` label is not a
  replacement for recording functional release changes.

## Why

data-olympus is a governance product. A release with no record of what
functionally changed is exactly the kind of undocumented decision the product
exists to prevent. The changelog is the human-readable counterpart to the git-
native decision history the KB format already provides.
