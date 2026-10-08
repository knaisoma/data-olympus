# Placeholder package version on cycle branches

Status: active

## Rule

After each cut, the first commit on `release/new` (and on `hotfix/new`) sets
`pyproject.toml` `[project].version`, and the matching `data-olympus` root
version in `uv.lock`, to the local placeholder `0.0.0+unreleased`. No target
version is committed on those branches. Build tooling injects the computed
version through the `release_artifacts.py` overlay, and PyPI rejects local
versions, so the placeholder can never be published. See
[`.rules/versioning.md`](../../.rules/versioning.md).

The placeholder is accepted only in that cycle. The decision lives in
`scripts/placeholder_version.py` and is tested in
`tests/test_placeholder_version.py`.

- Branch context is the pull request base ref (`GITHUB_BASE_REF`). On a push
  there is no base ref and the pushed branch (`GITHUB_REF_NAME`) applies. The
  head branch name is never used.
- The placeholder is accepted when the branch context is exactly `release/new`
  or `hotfix/new`.
- It is refused on `main`, on feature branches (including stacked pull
  requests whose base is a feature branch), and when there is no branch context
  at all, such as a plain local test run.
- Any other local or non `X.Y.Z` declared version, for example `0.0.0+other`
  or `0.12.0+unreleased`, is refused everywhere.
- A concrete `X.Y.Z` declared version behaves as before.

## version-free-guard

The `version-free-guard` job in `.github/workflows/ci.yaml` asks
`scripts/placeholder_version.py guard` what to do, passing the base ref from
the job environment:

- placeholder into `release/new` or `hotfix/new`: exit 0 with an explicit
  message, and no registry query;
- placeholder into any other base, even when the base already declares it:
  fail closed;
- any other non `X.Y.Z` version: fail closed;
- unchanged `X.Y.Z`: exit 0, as before;
- changed `X.Y.Z`: the existing tag reconciliation and the fail closed
  registry check in `scripts/check_version_free.py`, as before.

Before this rule, the placeholder reached `check_version_free.py`, whose
version grammar refused it with an argparse usage error (exit 2) before any
registry was contacted, so the first commit after a cut could not pass. A
placeholder already present on the base was skipped as unchanged on any base.

## server.json

`tests/test_server_json.py` normally requires both `server.json` version fields
to equal `[project].version`. When the declared version is the placeholder and
the branch context is `release/new` or `hotfix/new`, that equality is waived and
both fields must instead be one identical concrete stable `X.Y.Z`. They keep
the last released version, which is the entry the MCP registry serves until the
next release record updates it. The placeholder, a candidate version or a range
never belongs in `server.json`. Checking the value against the latest release
tag was not chosen because the CI test checkout is shallow and carries no tags.

To run the suite locally on a cycle branch, set the context explicitly, for
example `GITHUB_BASE_REF=release/new uv run pytest`.

## Cut precondition

Before the first `release/new` or `hotfix/new` cut under the engine-managed
cycle, this rule must be on `main`; otherwise the placeholder commit fails
`version-free-guard` and the `server.json` tests. The adoption cut runbook must
list it as a precondition.

A release pull request into `main` must not carry the placeholder. The
release record that lands on `main` declares the concrete released version in
`pyproject.toml`, `uv.lock` and `server.json`.
