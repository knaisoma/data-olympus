# Rule: how data-olympus versions and releases

Status: active
Since: 2026-07-01
Applies to: the data-olympus product (this repository)
Governing standard: STD-U-810

## Bump mapping (pre-1.0)

data-olympus is pre-1.0. Conventional Commit types on commits since the last tag
drive the bump:

- `feat!:`, any `type!:`, or a `BREAKING CHANGE:` footer -> minor (breaking, needs migration; stays pre-1.0, e.g. 0.1.x -> 0.2.0)
- `feat:` -> patch
- `fix:` / `perf:` -> patch
- `chore/docs/ci/build/refactor/test/style` -> no release, EXCEPT floored to patch when a functional path changed

"Functional path" is the exact set `scripts/check_changelog.py` defines: `src/`,
`bin/`, `deploy/`, and `SPEC.md`. This safety net guarantees source changes are
never left unreleased even if the commit type is non-user-facing.

The project stays pre-1.0 until `v1.0.0` is cut deliberately. When it reaches
1.0, `bump_for` in `scripts/compute_release.py` switches breaking -> major and
`feat:` -> minor, and this rule is updated.

## Engine

- `scripts/compute_release.py` computes the bump (single source of the rules).
- The `data-olympus-release-cutter` routine (05:00 UTC daily) prepares a release
  PR: it bumps `pyproject.toml`, renames the CHANGELOG `[Unreleased]` block, and
  writes friendly notes to `docs/releases/vX.Y.Z.md`.
- The human gate is the operator merging that PR.
- `.github/workflows/tag-release.yml`, on merge (detected via the pyproject
  version bump), cuts the annotated `vX.Y.Z` tag, builds the image, then
  publishes the GitHub Release once the image is available. The tag decision is
  reconcilable: a full rerun after a partial failure re-emits the tag while it
  points at the current commit, so the idempotent image/release jobs finish.
- `pyproject.toml` is the single version source of truth (STD-U-810 §11.4).

## PR discipline

- Squash-merge only; the PR title is the one Conventional Commit and is linted by
  `.github/workflows/pr-title-lint.yml` (STD-U-810 §7.1 equivalent).
- Every functional PR updates the CHANGELOG `[Unreleased]` block (see
  `.rules/changelog-per-release.md`).
