# PyPI Trusted Publishing setup

Data Olympus publishes Python distributions with PyPI Trusted Publishing.
GitHub mints a short lived OIDC token for a protected environment, and PyPI
exchanges it for an upload credential. The repository stores no PyPI token.

Release publication is fail closed. A candidate is not finalized and stable
channels are not promoted when the corresponding PyPI upload or registry
verification fails.

## Publisher identities

Each top level workflow that uploads has its own pending GitHub publisher on
PyPI, pinned to its own environment. Both use PyPI project `data-olympus`,
GitHub owner `knaisoma` and repository `data-olympus`.

| Workflow | Environment | Uploads |
|---|---|---|
| `rc-publish-stage.yml` | `pypi-rc` | Release candidates (stage 2) |
| `promote-release.yml` | `pypi` | Stable releases (reviewed promotion) |

The reusable workflow `publish-python-build.yml` is not a publisher identity.
PyPI evaluates the top level workflow that started the run. The publishers of
the retired old path (`rc-publish.yml`, `tag-release.yml`, `publish-pypi.yml`)
are removed on pypi.org by the owner, see
[repository settings](repository-settings.md).

## PyPI setup

1. Sign in to <https://pypi.org> with two factor authentication enabled.
2. Open <https://pypi.org/manage/account/publishing/>.
3. Add a pending GitHub publisher for each row of the table above, with its
   environment.

## GitHub environments

Create the environments `pypi-rc` and `pypi` in the repository settings.

1. Allow deployment only from `main`.
2. Do not add a PyPI password or API token secret.

See [repository settings](repository-settings.md#3-environments) for the
recorded state and read-back commands. Only the uploading jobs scope
`id-token: write` to these environments. Other jobs use read only repository
permissions unless they must publish a Git tag, GitHub release, or GHCR tag.

## Candidate publication

A push to `release/new` or `hotfix/new` runs `rc-build.yml`, and
`rc-publish-stage.yml` publishes the verified candidate from `main` through
`pypi-rc`. See [RC publication](rc-publication-stage.md).

## Stable promotion

An operator dispatches `promote-release.yml` from `main` with `squash`,
`reviewed_head` and `candidate_tag`. It rebuilds the stable Python files from
the release squash, compares their payload with the candidate, and uploads them
through `pypi` only after its proofs and gates pass. It never rebuilds the
image. See [reviewed promotion](reviewed-promotion.md).

## Verification

After candidate publication, verify:

```bash
uvx --from 'data-olympus==0.6.0rc3' data-olympus --help
gh release view 0.6.0-rc.3
docker buildx imagetools inspect ghcr.io/knaisoma/data-olympus:0.6.0-rc.3
```

After stable promotion, verify:

```bash
uvx --from 'data-olympus==0.6.0' data-olympus --help
gh release view v0.6.0
docker buildx imagetools inspect ghcr.io/knaisoma/data-olympus:stable
```

## Recovery and rollback

For a failed upload, re-run the failed jobs of the same run. The jobs reconcile
existing artifacts and verify their hashes. For a promotion that stopped after
the PyPI upload, see
[resuming after the PyPI upload](reviewed-promotion.md#resuming-after-the-pypi-upload).

For an unsuitable candidate on PyPI, yank that candidate from its PyPI release
page and publish a higher candidate number. Do not delete or overwrite it.

No workflow restores a moving GHCR channel. After a rollback, which
re-deploys the recorded prior digest, the next patch promotion moves `stable`
and `latest`. Immutable version tags and PyPI
versions are never overwritten. A faulty stable PyPI release should be yanked,
followed by a new patch release.
