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

Promotion requires the release squash on `main` to have exactly the tree of the
reviewed head (`scripts/release_record.py` refuses a tree mismatch, see
[reviewed-promotion.md](reviewed-promotion.md)). The reviewed tree is never
edited for release metadata, so `main` also declares the placeholder after
every release. The released version lives in the annotated `vX.Y.Z` tag, the
release record and the published artifacts, not in `pyproject.toml` on `main`.

The decision lives in `scripts/placeholder_version.py` and is tested in
`tests/test_placeholder_version.py` and `tests/test_server_json.py`.

- Branch context is the pull request base ref (`GITHUB_BASE_REF`). On a push
  there is no base ref and the pushed branch (`GITHUB_REF_NAME`) applies. The
  head branch name is never used. Branch names match exactly, case included.
- On pull requests into `release/new` or `hotfix/new` the placeholder is
  accepted.
- On pull requests into `main` it is accepted only when the pull request head
  tree is exactly the tree of `origin/release/new` or `origin/hotfix/new` (the
  release or hotfix squash), or when `main` already declares the placeholder
  and the pull request leaves the version unchanged. A head that contains a
  cycle tree plus any other change is refused, and a missing cycle ref never
  matches.
- It is refused on every other base: feature branches, stacked pull requests
  whose base is a feature branch, and no branch context at all, such as a plain
  local test run. Pull requests during the cycle must therefore target
  `release/new` or `hotfix/new` directly.
- Any other local or non `X.Y.Z` declared version, for example `0.0.0+other`
  or `0.12.0+unreleased`, is refused everywhere.
- A concrete `X.Y.Z` declared version behaves as before.

## version-free-guard

The `version-free-guard` job in `.github/workflows/ci.yaml` asks
`scripts/placeholder_version.py guard` what to do. The base ref and the pull
request head SHA come from the job environment, never interpolated into the
script. The head tree is taken from the pull request head commit, not from the
merge commit that `actions/checkout` produces, and compared with the trees of
the fetched remote branches. They are read by full ref,
`refs/remotes/origin/release/new` and `refs/remotes/origin/hotfix/new`, so a
local tag named `origin/release/new` cannot shadow the branch.

The guard is an early, advisory check of where the change comes from, not the
release proof. A head tree equal to a cycle branch shows that the pull request
carries that branch's reviewed content; it does not prove the tree that lands
after GitHub's squash, and a green result goes stale if `release/new` or
`hotfix/new` moves afterwards. The authoritative proof is
`scripts/release_record.py`, which refuses promotion unless the squash on
`main` has exactly the tree of the reviewed head (see
[reviewed-promotion.md](reviewed-promotion.md)).

- placeholder into `release/new` or `hotfix/new`: exit 0 with an explicit
  message, and no registry query;
- placeholder into `main` as a tree-equal cycle squash, or unchanged on `main`:
  exit 0 with an explicit message;
- placeholder into any other base, or into `main` from anywhere else: fail
  closed;
- any other non `X.Y.Z` version: fail closed;
- unchanged `X.Y.Z`: exit 0, as before;
- changed `X.Y.Z`: the existing tag reconciliation and the fail closed
  registry check in `scripts/check_version_free.py`, as before;
- an unexpected decision output: fail closed.

Before this rule, the placeholder reached `check_version_free.py`, whose
version grammar refused it with an argparse usage error (exit 2) before any
registry was contacted, so the first commit after a cut could not pass. A
placeholder already present on the base was skipped as unchanged on any base.

## server.json

`tests/test_server_json.py` normally requires both `server.json` version fields
to equal `[project].version`. When the declared version is the placeholder and
the branch context is `release/new`, `hotfix/new` or `main`, that equality is
waived and both fields must instead be one identical concrete stable `X.Y.Z`.
That value may lag the latest release: the promotion job updates only its
workspace copy of `server.json` for publication, and the tree proof forbids
editing the squash, so the committed file keeps whatever concrete version it
last carried. The placeholder, a candidate version or a range never belongs in
`server.json`.
Checking the value against the latest release tag was not chosen because the
CI test checkout is shallow and carries no tags. Whether a pull request may
bring the placeholder into `main` is decided by `version-free-guard`, not by
this test.

To run the suite locally on a cycle branch, set the context explicitly, for
example `GITHUB_BASE_REF=release/new uv run pytest`.

## Required checks

The `main` protection is only as strong as the checks that must pass. The
`version-free-guard` and `test` jobs must be required status checks on `main`
in the data-olympus repository ruleset; otherwise a pull request that brings
the placeholder into `main` by any other route could merge with a red guard.
The controller adds them to the ruleset; this change does not modify it.

## Cut precondition

Before the first `release/new` or `hotfix/new` cut under the engine-managed
cycle, this rule must be on `main`; otherwise the placeholder commit fails
`version-free-guard` and the `server.json` tests, and so does the first
release squash into `main`. The adoption cut runbook must list it as a
precondition.
