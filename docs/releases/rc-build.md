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
Pushing a `v*` tag no longer launches a rebuild publication.

R9 compatibility notes: the manual PyPI fallback preserves `skip-existing: true`
and the post-upload SHA256 verification. After building, it compares every
existing PyPI file with the local filename and SHA256 before upload. Matching
files allow a retry, including a partially uploaded release; conflicting files
or an unreadable registry fail closed. A version's existence alone is not a
refusal. Pre-upload validation and post-upload readback run in separate read-only
jobs. The OIDC upload job only downloads the validated artifacts and publishes.
Nonrelease refs remain build-only: resolution tries the fetched
`refs/remotes/origin/<ref>`, then a full 40-hex commit SHA, then the local name.
Every build receives the resolved immutable SHA. Branches such as `vendor-bump`
are allowed; malformed `v`-prefixed tags and names beginning with `v` followed
by a digit are refused. Release tags must exist exactly and match the package
metadata version before upload is allowed.

Manual image builds require a source branch of `main`, `release/new`, or
`hotfix/new` and build the dispatch's exact `github.sha`. Candidate identities
(`X.Y.Z-rc.N` and `X.Y.Z-hotfix.rc.N`, also with a legacy `v` prefix) are reserved
for the candidate pipeline. Every stable identity (`X.Y.Z` or `vX.Y.Z`) is
reserved for promotion, including identities absent from Git and the registry.
Existence checks supply diagnostics only. Channel tags remain reserved.

Refusing `v<digit>`-prefixed refs that are not release tags (for example a
branch named `v2-packaging`) on manual PyPI dispatch is intentional: it keeps
the release-tag namespace unambiguous and only removes build-only runs, never
enabling an upload. To build such a ref, dispatch by its full commit SHA.

## Task 9 prerequisites

No dispatched end-to-end dry run has been performed for this workflow. Task 9
must first wire operator adoption ratification into the workflow and make
`rc-build.yml` available for dispatch from the default branch. Then dispatch a
dry run on `release/new` and retain evidence of:

- Buildx multi-architecture OCI export for `linux/amd64` and `linux/arm64`.
- The `containerimage.digest` metadata key and its recorded digest.
- Artifact size and successful upload within the runner's artifact limits.
- Successful installed-package smoke tests for the wheel and source distribution.
- The `set-channel` digest lookup: run `docker buildx imagetools inspect
  --format '{{.Manifest.Digest}}'` against a real published tag (for example
  `v0.11.0`) and retain the output, since only mocks exercise it today.

This runner verification remains deferred until those prerequisites are met.
