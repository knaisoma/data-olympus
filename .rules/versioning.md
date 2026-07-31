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

The AI Operations runner consumes their output and never reimplements the
mapping.

## Outcome based release

The release routine examines work already merged to `origin/main`.

It does not create a feature batch, choose a quota of issues, or assume that
Friday planning must produce a Monday release.

When a release is due:

1. Create one short lived `chore/release-vX.Y.Z` branch from the approved main
   SHA.
2. Update `pyproject.toml`, the changelog, and the release note in one logical
   release change.
3. Review and merge that change through the current repository rules.
4. Bind the candidate to the resulting exact main SHA.
5. Publish and promote only through the existing release workflows.

## Pull request discipline

Feature and fix pull requests contain one logical change and are squash merged.
The pull request title is the Conventional Commit recorded on `main`.

The release pull request also contains one logical release change and is squash
merged.

The repository currently enforces linear history. Ordinary release work must
not require a merge commit or an integration branch that bypasses that rule.

The exceptional 2026-07-31 history reconciliation preserved the already
published `v0.6.0` ancestry through an explicit recovery merge. That exception
does not define the future release pattern.

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
not build an image and does not deploy kn dev while Keel policy is `never`.

## Immutability

Published versions, Git tags, GitHub releases, and OCI version tags are
immutable.

`v0.6.0` is already published and reconciled into `main`. It must never be
rebuilt, retagged, or republished. The next valid release is a forward version,
currently expected to be `v0.6.1` when the exact main history remains
releasable.

Any collision or source mismatch blocks the release. Recovery uses a new
version or a higher candidate number, never replacement of an existing
artifact.
