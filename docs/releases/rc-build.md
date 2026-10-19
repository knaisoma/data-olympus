# Candidate build artifacts

The `rc-build` workflow builds candidates on pushes to `release/new` and
`hotfix/new`. It has only `contents: read` permission and uploads workflow
artifacts. Publication is a separate stage; this workflow has no registry login,
publication token, or upload to PyPI or GHCR.

The version engine computes the identity from full Git history and tags. The
build refuses a stale branch head or a main commit that is not an ancestor of
the source. It injects the computed Python version into an isolated build copy
for both Python distributions and the container image. Normal candidates map
`X.Y.Z-rc.N` to `X.Y.ZrcN`; hotfixes map `X.Y.Z-hotfix.rc.N` to `X.Y.Z.devN`.

Each workflow artifact contains:

- The wheel and source distribution, both checked by an installed-package smoke test.
- The OCI archive and Buildx metadata.
- Version-engine output and the build decision.
- Provenance recording the candidate identity, Python mapping, cut `B`, source
  `H`, main `M`, commit count `N`, distribution hashes, archive hash, and image digest.
- One SPDX 2.3 SBOM per wheel, sdist and image platform under `sbom/`, generated
  by the pinned syft and listed in the provenance under `sboms`
  (see [per-artifact SBOMs](sbom.md)).

At `N=0`, the build still verifies all artifacts but records `promotable: false`.
A manual dispatch defaults to `dry_run: true` and also records that it cannot
be promoted. A dry-run dispatch on a work branch uses the normal release version
rules while checking that branch's exact head. It still requires a valid tagged
cut or a valid adoption record.

When the cut carries `release/ADOPTION.json`, preflight reads both pinned
ratifications (`scripts/adoption_ratification.py`, STD-U-821 amendments 1.3
and 1.5) and both vendored amendments from `origin/main`'s Git blobs, never
from the checked-out `H`, and the engine validates the record with them. Only a dispatched dry run evaluates
the record in adoption dry-run mode, which is never promotable. The cut build
itself (`N=0`) builds and verifies but is not promotable. The reasoning and the
cut sequence are in [the adoption cut runbook](adoption-cut-runbook.md).

The old candidate, stable, manual PyPI and manual image workflows
(`rc-publish.yml`, `tag-release.yml`, `publish-pypi.yml`, `release-image.yml`)
were retired in 2026-10. The pull request packaging dry run they hosted now
runs in `package-dry-run.yml`, which builds and checks the wheel and sdist
through `publish-python-build.yml` and never uploads.

## Live verification still pending

No dispatched end-to-end dry run has been performed for this workflow. The
adoption ratification is wired, and `rc-build.yml` is on the default branch, so
the first runs are the push builds and the dry-run dispatch of the
[adoption cut runbook](adoption-cut-runbook.md). Retain evidence of:

- Buildx multi-architecture OCI export for `linux/amd64` and `linux/arm64`.
- The `containerimage.digest` metadata key and its recorded digest.
- Artifact size and successful upload within the runner's artifact limits.
- Successful installed-package smoke tests for the wheel and source distribution.

This runner verification remains deferred until the cut is executed.
