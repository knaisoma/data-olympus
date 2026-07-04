# Rule: how data-olympus versions and releases

Status: active
Since: 2026-07-01
Applies to: the data-olympus product (this repository)
Governing standard: STD-U-810

## Bump mapping (pre-1.0)

data-olympus is pre-1.0. STD-U-810 §3.1.1 lets a pre-1.0 project adopt a
features-as-minor mapping instead of the squashed default, provided the choice is
documented in the project's local rules. data-olympus adopts it: a `feat:` drives
a MINOR bump, so shipped features are visible in the version number. Conventional
Commit types on commits since the last tag drive the bump:

- `feat:` -> minor (new functionality; resets patch, e.g. 0.1.x -> 0.2.0)
- `feat!:`, any `type!:`, or a `BREAKING CHANGE:` footer -> minor (a breaking change pre-1.0 stays pre-1.0; it does NOT force 1.0.0)
- `fix:` / `perf:` -> patch
- `chore/docs/ci/build/refactor/test/style` -> no release, EXCEPT floored to patch when a functional path changed

This DEVIATES from the STD-U-810 §3.1.1 default (which maps pre-1.0 `feat:` to a
patch); the deviation is the documented project choice §3.1.1 requires. Pre-1.0 a
breaking change is a minor bump, not major, so the project stays in the `0.x`
series until `v1.0.0` is cut deliberately.

"Functional path" is the exact set `scripts/check_changelog.py` defines: `src/`,
`bin/`, `deploy/`, and `SPEC.md`. This safety net guarantees source changes are
never left unreleased even if the commit type is non-user-facing.

The project stays pre-1.0 until `v1.0.0` is cut deliberately. When it reaches
1.0, `bump_for` in `scripts/compute_release.py` switches breaking -> major
(`feat:` already maps to minor), and this rule is updated.

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

Merge method follows the granularity of the change (amended 2026-07-04; the
previous rule was squash-only):

- **Feature/fix PRs (one logical change) are squash-merged.** The PR title is
  the one Conventional Commit and is linted by
  `.github/workflows/pr-title-lint.yml` (STD-U-810 §7.1 equivalent). This
  applies equally to PRs targeting `main` and PRs targeting a release branch.
- **Release/integration branches (`release/X.Y.Z`) merge into `main` with a
  merge commit, never a squash.** Each constituent commit on the release branch
  is already one squashed Conventional Commit per feature/fix, so a merge
  commit preserves one readable commit per change on `main` (blame and bisect
  keep per-feature granularity), while squashing again would collapse the whole
  release into a single opaque commit. The release PR title still follows the
  Conventional format (`chore(release): vX.Y.Z`) for the PR-title lint, but the
  merge commit itself carries no bump-relevant type: `compute_release.py` reads
  the constituent commits, and `tag-release.yml` keys off the `pyproject.toml`
  version bump reaching `main`, so both are unaffected by the merge method.
- Every functional PR updates the CHANGELOG `[Unreleased]` block (see
  `.rules/changelog-per-release.md`).
