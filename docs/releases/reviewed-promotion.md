# Reviewed release promotion

`promote-release.yml` implements STD-U-821 stable promotion beside the
unchanged transitional `tag-release.yml`. Every job is skipped unless the
repository variable `SDLC_PIPELINE` is `enabled` and the dispatch ref is
`main`. Dispatch it with three inputs:

- `squash`: the full SHA of the release squash `S`, which must be the head of `main`.
- `reviewed_head`: the full reviewed SHA `H` recorded by the published candidate.
- `candidate_tag`: the existing `X.Y.Z-rc.N` or `X.Y.Z-hotfix.rc.N` prerelease.

## Prerequisites (blocked until W9)

Activation needs all of the following. A missing item fails closed and there is
no fallback to a human token:

- The W9 GitHub App: repository variable `SDLC_APP_ID` and Actions secret
  `SDLC_APP_PRIVATE_KEY`. The App pushes the stable tag and reads Dependabot
  and code scanning alerts, which `GITHUB_TOKEN` cannot read.
- The protected `pypi` environment with its Trusted Publisher.
- `SDLC_REQUIRED_CODEQL_CHECKS`: the exact required language-analysis check
  names, comma-separated. `H` and `S` both need successful `test`, aggregate
  `CodeQL` and each of those checks.
- Trusted adoption ratification (Task 9). Until then a cut that carries
  `release/ADOPTION.json` is refused, matching the stage-one build.

## Preparing the release squash

Generate the body with the trusted helper:

```bash
uv run python scripts/release_record.py notes \
  --head "$H" --main "$M" --branch release/new \
  --output to-delete/release-notes.md
```

The subject is exactly `release: X.Y.Z` (engine target) and the body is exactly
the generated notes. When squash merging on GitHub, remove the `(#N)` suffix
GitHub appends to the title and any `Co-authored-by` trailers it adds to the
body; either makes the proof fail. Notes group the engine's non-merge commit
set into breaking changes, features, fixes and other changes, keeping commit
bodies with issue links and migration guidance. The reviewed tree is never
edited for release metadata.

## Proofs

`scripts/release_record.py prove` recomputes the version with the engine from
`H` and the candidate's recorded pre-squash main `M`, and proves:

- `S` has exactly one parent, the candidate's recorded cut `B`.
- The trees of `S` and `H` are equal.
- The candidate tag resolves to `H`, and the candidate provenance records the
  same `B`, `H`, `M` and `N`, a positive `N`, `promotable: true` and
  `dry_run: false`.
- The subject and body equal the engine target and the generated notes.
- `release/ADOPTION.json` is absent at `H`.

The JSON release record maps `H`, `S`, `B`, `M`, the candidate, the stable tag
and the image digest. The proof only reads Git.

The workflow holds the shared `data-olympus-promotion` lock without
cancellation (R-CONC). Right after acquiring it, the `initial` proof also
requires `main` head to equal `S` and the stable tag to be absent, and
`version_free.py` requires the version to be absent from PyPI, GHCR, GitHub
releases and Git tags. GitHub keeps only one pending run per concurrency group,
so a second dispatch queued behind a running promotion replaces any earlier
pending one; dispatch again if that happens.

The publishing jobs repeat the proof in the `resume` phase after `pypi`
environment approval and before tagging, and require the record to be byte
equal. `resume` accepts a `main` that has advanced past `S` (but not one that
no longer contains `S`) and an existing stable tag only if it is annotated and
on `S`. This keeps "Re-run failed jobs" usable after a partial publication.
Re-running all jobs is refused once anything is published.

The proof job also requires zero open security alerts (repeated after
environment approval), that the RC GHCR tag still resolves to the recorded
digest, that the image's `org.opencontainers.image.version` and `revision`
labels equal the candidate tag and `H` on every platform, and that the
candidate wheel and sdist hashes match the provenance and PyPI.

## Stable Python artifacts

A read-only job runs `release_artifacts.py stable-promotion`, which builds the
wheel and sdist from `S` with the stable version injected. It verifies the
candidate hashes, the source-tree and normalized lock hashes, then compares
every regular file and its mode. Only these version-related differences are
allowed:

- Wheel: the `.dist-info` directory name, the single metadata `Version` header
  and the RECORD line derived from METADATA. Every RECORD hash and size is
  validated first, on both wheels.
- Sdist: the root directory name, the single `PKG-INFO` `Version` header, the
  `project.version` assignment and the `uv.lock` root package version.

Compression, member order, timestamps and ownership are not payload. All other
bytes, including metadata bodies and dependencies, must match. Both artifacts
are smoke tested before the protected OIDC job publishes them. The existing
`candidate` and `stable` modes are unchanged; a golden-value test pins their
output to the module at commit c341940.

## Publication order

1. PyPI through Trusted Publishing in the `pypi` environment. A re-run skips
   files already present, and the hash check then requires every remote file
   to equal the local build.
2. The App creates the annotated `vX.Y.Z` tag on `S`, carrying the notes.
3. Exactly `vX.Y.Z`, `stable` and `latest` move to the RC digest, as
   `tag-release.yml` does today, without rebuilding. The image keeps its RC
   version and revision labels; the stable provenance records the alias.
4. The GitHub release with the generated notes, stable files, stable
   provenance and release record. A re-run uploads only missing assets and
   refuses different bytes.
5. The MCP registry, last, with the pinned publisher, ownership marker, OIDC
   login and read-back of `tag-release.yml`. Only the job's workspace copy of
   `server.json` receives the version.

Recovery from a partial publication never replaces or re-tags a published
item. The workflow never deploys: production stays digest-pinned in reviewed
gitops, and rollback uses the recorded prior digest with `set-channel.yml`.
