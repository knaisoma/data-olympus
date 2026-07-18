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

* `scripts/compute_release.py` computes the bump as the single source of the
  bump rules.
* The release cutter prepares the final version commit on the integration
  branch before any candidate is published.
* `rc-publish.yml` receives an exact source SHA and explicit candidate number.
  It publishes a wheel, sdist, `release-provenance.json`, GHCR image, PyPI
  prerelease, and GitHub prerelease from that exact source SHA. The moving `rc`
  channel changes only after every candidate surface verifies successfully.
* Candidate Python versions use `X.Y.ZrcN`. GHCR and GitHub candidates use
  `X.Y.Z-rc.N`.
* After canary verification and human approval, the integration branch is
  merged to `main`. Stable publication does not start from the merge event.
  The operator explicitly dispatches `tag-release.yml` with `candidate_tag`
  naming the highest complete candidate.
* Stable promotion requires the candidate source to be an ancestor of `main`.
  It rebuilds the wheel and sdist from the candidate source, permits only
  normalized version metadata to differ, publishes through the protected
  `pypi` environment, creates the stable tag at the candidate source SHA, and
  promotes the exact candidate image digest without rebuilding it.
* `pyproject.toml` is the single version source of truth (STD-U-810 §11.4).

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
  the constituent commits. Stable promotion is dispatched explicitly after the
  merge and checks that the candidate source is an ancestor of `main`, so it is
  also unaffected by the merge method.
  (`compute_release.py` walks `git log --no-merges`, so the merge commit is
  invisible to the bump computation by construction.)
- **Do not confuse the two release branch kinds.** The daily release-cutter
  routine (`.rules/release-routine.md`) opens `chore/release-v<next>` PR
  branches containing a single version-cut commit; those are one logical
  change and stay squash-merged. `release/X.Y.Z` integration branches, used
  when a coordinated program lands many features/fixes together before one
  gated merge to `main`, are the case the merge-commit rule above exists for.
- Every functional PR updates the CHANGELOG `[Unreleased]` block (see
  `.rules/changelog-per-release.md`).
