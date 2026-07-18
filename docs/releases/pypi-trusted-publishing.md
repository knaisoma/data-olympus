# PyPI Trusted Publishing setup

Data Olympus publishes Python distributions with PyPI Trusted Publishing.
GitHub mints a short lived OIDC token for a protected environment, and PyPI
exchanges it for an upload credential. The repository stores no PyPI token.

Release publication is fail closed. A candidate is not finalized and stable
channels are not promoted when the corresponding PyPI upload or registry
verification fails.

## Publisher identities

Create a pending GitHub publisher on PyPI for each top level workflow that can
upload. Every other field is identical.

* PyPI project name: `data-olympus`
* GitHub owner: `knaisoma`
* GitHub repository: `data-olympus`
* Environment: `pypi`
* Candidate workflow: `rc-publish.yml`
* Stable workflow: `tag-release.yml`
* Manual fallback workflow: `publish-pypi.yml`

The reusable workflow is not a publisher identity. PyPI evaluates the top level
workflow that started the run.

## PyPI setup

1. Sign in to <https://pypi.org> with two factor authentication enabled.
2. Open <https://pypi.org/manage/account/publishing/>.
3. Add a pending GitHub publisher for `rc-publish.yml` using the values above.
4. Repeat for `tag-release.yml`.
5. Repeat for `publish-pypi.yml` only when the manual tag fallback is retained.

## GitHub environment

Create an environment named `pypi` in the repository settings.

1. Require an operator reviewer for deployments.
2. Allow deployment only from `main` and approved protected release branches.
3. Dispatch `rc-publish.yml` itself from `main`, even when its `ref` input names
   another exact source SHA.
4. Do not add a PyPI password or API token secret.

The candidate and stable jobs scope `id-token: write` to this environment. Other
jobs use read only repository permissions unless they must publish a Git tag,
GitHub release, or GHCR tag.

## Candidate publication

Run `rc-publish.yml` with these inputs:

* `ref`: the exact reviewed source SHA or a ref that resolves to it
* `number`: the positive candidate number, such as `3`

The workflow builds `0.6.0rc3` for PyPI and `0.6.0-rc.3` for GHCR and GitHub.
It publishes an immutable image, wheel, source distribution, and provenance
receipt. It verifies PyPI file hashes and the image digest before moving `:rc`
or creating the GitHub prerelease.

A rerun with the same number reuses an existing image only when its embedded
source revision equals the requested SHA. PyPI upload uses `skip-existing`, then
reads the registry back and compares SHA256 values. A source mismatch fails.

## Stable promotion

`tag-release.yml` runs after the release change reaches `main`. It selects the
highest complete prerelease for the declared project version. A complete
candidate must have all of these assets:

* `release-provenance.json`
* candidate wheel
* candidate source distribution
* verified image digest

The candidate source SHA must be an ancestor of `main`. The stable Git tag is
created at that candidate SHA. The workflow rebuilds only the stable Python
version overlay, compares the wheel payload with the candidate, publishes PyPI,
and retags the verified image digest as the stable version, `stable`, and
`latest`. It never rebuilds the image during promotion.

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

For a failed upload, rerun the same workflow with the same source and candidate
number. The jobs reconcile existing artifacts and verify their hashes.

For an unsuitable candidate on PyPI, yank that candidate from its PyPI release
page and publish a higher candidate number. Do not delete or overwrite it.

To restore a moving GHCR channel, retag a previously verified digest:

```bash
docker buildx imagetools create \
  --tag ghcr.io/knaisoma/data-olympus:rc \
  ghcr.io/knaisoma/data-olympus@sha256:<verified-digest>
```

Use the same pattern for `stable` and `latest`. Immutable version tags and PyPI
versions are never overwritten. A faulty stable PyPI release should be yanked,
followed by a new patch release.
