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

The expected output is `0 errors across 0 files`. If you get errors, fix them before committing.

## PR requirements

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

## Code of Conduct

By contributing you agree to abide by the project's [Code of Conduct](CODE_OF_CONDUCT.md).

## Releases

Releases follow STD-U-810 and are documented in `.rules/versioning.md`. In short:

- Merge PRs with squash-merge; the PR title must be a Conventional Commit.
- A daily routine opens a release PR when `main` has releasable changes.
- Merging the release PR cuts an annotated `vX.Y.Z` tag, a GitHub Release, and
  the Docker image. No tags are cut by hand under normal operation.
