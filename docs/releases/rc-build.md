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

At `N=0`, the build still verifies all artifacts but records `promotable: false`.
A manual dispatch defaults to `dry_run: true` and also records that it cannot
be promoted. A dry-run dispatch on a work branch uses the normal release version
rules while checking that branch's exact head. It still requires a valid tagged
cut. A `release/ADOPTION.json` record at the cut stops the build until explicit
ratification is wired into the workflow during adoption.

The transitional candidate and stable publication workflows remain available.
Their manual fallback entry points validate tags and reject existing publication
identities; pushing a `v*` tag no longer launches a rebuild publication.
