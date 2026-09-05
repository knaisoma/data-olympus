# Data Olympus versioning and release rule

Status: active
Since: 2026-07-31
Applies to: the Data Olympus product
Governing standard: STD-U-810

## Version mapping before 1.0

Data Olympus uses the documented pre 1.0 project deviation where features
advance the minor version.

Conventional Commit types since the last stable tag drive the bump:

* `feat:` advances the minor version.
* Any breaking change advances the minor version while the project remains
  before 1.0.
* `fix:` and `perf:` advance the patch version.
* Other types do not create a release unless a functional path changed.

Functional paths are defined by `scripts/check_changelog.py`:

* `src/`
* `bin/`
* `deploy/`
* `SPEC.md`

A functional path change creates at least a patch release even when its commit
type would otherwise produce no release.

## Single source

`scripts/compute_release.py` is the single source for bump computation.

`pyproject.toml` is the single source for the package version.

Release preparation consumes their output without reimplementing the mapping.

## Weekly release change

Select a coherent issue batch under `.rules/release-planning.md`. Prepare work
and version metadata together on a short-lived release branch, for example
`codex/release-YYYY-MM-DD`, including lockfile, changelog, and user-facing release
notes. The branch name need not contain the version. Compute from exact selected history
accounting for every feature, fix, performance improvement, and breaking change.
Use `next_version` from `scripts/compute_release.py`, not ticket counts.
The final PR title must represent the entire batch's Conventional Commit impact
so computation after squash merge stays consistent. Never hide features in a
`chore` title merely because the PR includes release preparation.

## Pull request discipline

Require implementer acceptance, independent exact-content review, passing gates,
and human merge. Automation must never merge. Changes invalidate review. Release
PRs must be squash merged, preserving linear history without bypass. Other merge
methods are not supported. Prove merged and reviewed trees match.
Prepared but unpublished versions follow `.rules/release-routine.md` recovery
and receive fresh human merge authorization.

## Candidate and stable publication

Candidate identities use:

* Python version `X.Y.ZrcN`.
* GHCR and GitHub prerelease tag `X.Y.Z-rc.N`.

`rc-publish.yml` receives an exact source SHA and candidate number. It publishes
the complete candidate transaction and moves the `rc` channel only after every
surface verifies.

`tag-release.yml` is invoked only by explicit `workflow_dispatch` with the
`candidate_tag` input naming the highest complete candidate. It requires the
candidate source to be an ancestor of `main`. It enters the protected `pypi` environment,
publishes stable Python artifacts from the same source, creates `vX.Y.Z` at that
source, and promotes the exact OCI digest without rebuilding it.

`set-channel.yml` moves registry channels to an existing image digest. It does
not build an image or deploy a workload.

## Immutability

Published versions, Git tags, GitHub releases, and OCI version tags are
immutable.

`v0.6.0` is already published and reconciled into `main`. It must never be
rebuilt, retagged, or republished. The next valid release is always the forward
version returned by `scripts/compute_release.py` for the exact admitted
`origin/main` history. No rule document hard codes the next version.

Any collision or source mismatch blocks the release. Recovery uses a new
version or a higher candidate number, never replacement of an existing
artifact.
