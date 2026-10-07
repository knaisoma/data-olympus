# Contributing to data-olympus

Thank you for your interest in contributing. This document covers the two kinds of contribution, the dev setup, and the requirements every PR must meet.

## Two kinds of contribution

### (a) Tool and code changes

Bug fixes, new CLI commands, MCP server improvements, deploy configuration, test coverage, documentation fixes. These follow the standard fork-and-PR flow.

### (b) Spec and format changes

Any change to the OKF-compatible format (bundle layout, frontmatter schema, reserved filenames, type/status/tier controlled vocabularies, serving contracts) is a **spec change** and must go through a Spec Proposal issue first.

Why the extra step: the format is the primary contribution of this project. Spec changes affect every bundle author and every downstream OKF consumer. A Spec Proposal gives maintainers and the community a chance to evaluate backward-compatibility and OKF-compatibility impact before implementation begins.

To propose a spec change, open an issue using the **Spec Proposal** template. Discuss it there before writing code. When there is rough consensus, implementation can proceed under a linked PR.

## Development setup

Requirements: Python 3.13+, [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/knaisoma/data-olympus.git
cd data-olympus
uv venv
uv pip install -e '.[dev]'
```

Run the linter and tests:

```bash
uv run ruff check .
uv run pytest
```

Lint the example bundle to confirm the format tools work:

```bash
uv run data-olympus lint example-bundle
```

The expected output is `0 errors across 0 files (N linted)`, where `N` is the
number of concept files found (nonzero for `example-bundle`). If you get
errors, fix them before committing.

## PR requirements

STD-U-821 states:

> Start feature branches from release/new and open pull requests against release/new. Use Conventional Commits. Each pull request gets a preview; integration builds an RC for staging. The release process squash-merges the batch to main and deploys production from its stable version tag.

Today, this repository's CI runs tests and wheel/sdist smoke checks per pull
request. There is no hosted preview and no per-PR image. The RC image and wheel
will come from `rc-build.yml` once it is enabled; that pipeline is delivered
separately, not yet in the repository. The target production flow pins artifacts
by digest through reviewed gitops changes.

For a fork, fetch the upstream `release/new`, create your feature branch from
that revision, push to your fork, and select upstream `release/new` as the PR
base. External contributors target `release/new`, never `main`. If the branch
has not yet been cut, wait for the maintainers to make it available. After a
release recut, move your open PR onto the new cut and rerun checks and review.
Fork PRs must not need publishing credentials or write tokens.

The RC pipeline is being introduced alongside the transitional release path.
Data Olympus is a public product,
so STD-U-821's internal-tool preview exception does not apply; a separate
preview exception request is pending operator ratification. New publication
and recut automation remain disabled until the required machine identity,
protections and publishing environments exist.

Every pull request must satisfy all of the following before it will be reviewed:

- Tests pass (`uv run pytest`).
- `ruff` reports no errors (`uv run ruff check .`).
- `data-olympus lint` exits 0 on any bundle you touch (zero errors, warnings are acceptable).
- Documentation is updated if the behaviour you changed is documented.
- `SPEC.md` and the validator stay consistent: if you change the schema (allowed field values, required fields, reserved names), update `SPEC.md` and the corresponding validator code together in the same PR.
- `CHANGELOG.md` is updated. Any PR that makes a functional change (CLI, MCP tools, REST endpoints, format/schema, enforcement, security, or a behaviour-changing bug fix) MUST add an entry under the topmost `## [Unreleased]` block. This is mandatory: every release must ship a changelog of its important functional changes. See [`.rules/changelog-per-release.md`](.rules/changelog-per-release.md).

For spec changes specifically, a Spec Proposal issue must be linked in the PR description and must show maintainer sign-off before the PR is merged.

## Commit style

This project uses [Conventional Commits](https://www.conventionalcommits.org/). Commit messages must use a type prefix:

- `feat:` for new features
- `fix:` for bug fixes
- `docs:` for documentation-only changes
- `ci:` for CI configuration changes
- `chore:` for maintenance work (dependency bumps, tooling)
- `test:` for test-only changes
- `refactor:` for code restructuring without behaviour change

Example: `feat(cli): add export command`

Every non-merge source commit and the proposed squash message must use
`type(scope)!: description`, with scope and `!` optional. Use `!` or a nonempty
`BREAKING CHANGE:` / `BREAKING-CHANGE:` footer for breaking changes. Malformed
messages fail lint. The PR title supplies the proposed squash subject; its
impact must be at least the highest impact of the source commits, and the
squash body must preserve breaking details. Do not squash a feature under
`chore:` or discard a breaking footer. Rewrite default revert subjects as
`revert: <original subject>`; reverts do not cancel earlier version impact.

Below 1.0.0, features and fixes bump patch, breaking changes bump minor, and
other valid types floor to patch for a nonempty integration. The release
batch uses the special `release: X.Y.Z` squash record with generated notes;
contributors do not choose or commit the target package version.

## Code of Conduct

By contributing you agree to abide by the project's [Code of Conduct](CODE_OF_CONDUCT.md).

## Releases

The target flow follows STD-U-821 v1.2 and [`.rules/versioning.md`](.rules/versioning.md).
The engine `scripts/sdlc_version.py` and the `release_artifacts.py` version
overlay are delivered separately, not yet in the repository. The engine will
derive `X.Y.Z-rc.N` from content and commit count, mapped to `X.Y.ZrcN` in Python
metadata. After the cut, the first commit on `release/new` will set the
`pyproject.toml` version to `0.0.0+unreleased`; builds will inject the computed
version through the overlay.

After review and gates, the agent holding operator authorization squash-merges
the batch to `main` with `release: X.Y.Z` and generated notes. Reserved cases
(including migrations and destructive changes) need explicit operator
authorization. The release proves the squash tree equals the reviewed tree,
creates annotated `vX.Y.Z`, and promotes the tested image digest. Production
deployment is a separate reviewed gitops change. Verified delivery is followed
by deleting and recutting `release/new` from tagged `main`.

Maintainers cut urgent fixes on `hotfix/new` from the current stable main tag,
with fixes only and the same gates. Candidates use `X.Y.Z-hotfix.rc.N`, mapped
to Python `X.Y.Z.devN`; features and breaking changes use the normal path.
Coordinate hotfix contributions with maintainers, still targeting `release/new`
for external PRs. After a hotfix, preserve pending work and recut the normal
branch. Staging selection uses the active branch head and digest, not version
ordering. A cut-only candidate (`N=0`) is never published or promoted.

Until the first new-model release, the existing explicit candidate/stable
dispatch path remains available for 0.11.x maintenance and rollback. It uses
`compute_release.py` and committed versions with its older pre-1.0 mapping;
do not mix the two engines. See [the release routine](.rules/release-routine.md)
for activation prerequisites, security gates and transitional delivery.
