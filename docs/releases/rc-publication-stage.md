# Trusted release candidate publication (stage 2)

`rc-publish-stage.yml` publishes a candidate built by `rc-build.yml`. It is
disabled unless the repository variable `SDLC_PIPELINE` is exactly `enabled`.
Activation also requires the W9 machine identity, the `release/new` ruleset,
and the `pypi-rc` environment with its own PyPI Trusted Publisher. It uses only
the job's `GITHUB_TOKEN` and PyPI OIDC and never falls back to a personal
token. The existing `rc-publish.yml`, `tag-release.yml` and `set-channel.yml`
keep their behaviour.

## Admission and trust

The workflow is triggered by `workflow_run`, so its definition is always the
one on `main`. Every job checks out that same `main` commit (`github.sha`) for
the scripts. Publication accepts only a successful `push` run of
`.github/workflows/rc-build.yml` from this repository's `release/new` or
`hotfix/new`, and downloads the artifact of that exact run and attempt.
Candidate history is fetched without credentials into a bare repository and is
read as Git data only. Nothing from `H` is checked out, built, installed or
executed. Downloaded wheels, sdists and the OCI archive are hashed and parsed
but never extracted to disk or run.

## Verification

The `main` copy of the version engine recomputes `B`, `H`, `M`, `N`, the
candidate tag and the Python version from `H`'s history. These must equal the
provenance, which must also say `promotable: true` and `dry_run: false`. Cut
builds (`N=0`), dry runs and unratified adoption cuts never publish. The
verifier then checks the wheel and sdist SHA256, file names and embedded
metadata, the OCI archive SHA256, and its root manifest digest. It also checks
every blob the descriptor graph references.

Identities are `X.Y.Z-rc.N` (PyPI `X.Y.ZrcN`) for `release/new` and
`X.Y.Z-hotfix.rc.N` (PyPI `X.Y.Z.devN`) for `hotfix/new`.

## Phases and permissions

All jobs hold the shared `data-olympus-promotion` lock with cancellation
disabled. Each job fetches history again and repeats the full verification, so
a moved branch head or a moved `main` fails closed.

| Job | Permissions | Action |
|---|---|---|
| `reserve` | `actions: read`, `contents: write`, `packages: read` | Creates an annotated tag at `H` and a public prerelease whose wheel, sdist and `release-provenance.json` assets bind the identity to `H` |
| `pypi` | `actions: read`, `contents: read`, `packages: read`, `id-token: write`, environment `pypi-rc` | Stages only the files missing from PyPI and uploads them with Trusted Publishing |
| `publish` | `actions: read`, `contents: read`, `packages: write` | Waits for the PyPI readback, copies the OCI archive with `skopeo copy --all --preserve-digests`, reads the remote digest back, moves `rc` by digest for `release/new` only, and writes the staging selection |

Before any write, every surface is read. A Git tag at another `H`, different
PyPI file hashes, a different image digest, or release assets with different
bytes are duplicate identities and fail the run. Objects that already exist
must carry the same `H` provenance. Registry errors other than an explicit
"not found" are outages and also fail closed.

## Retries

To retry a partial publication, re-run the failed `rc-publish-stage` jobs.
Existing objects are verified and reused, and only missing ones are written.
Do not re-run `rc-build` for a partially published `H`: wheel and sdist bytes
are not reproducible, so the rebuilt files collide with those already on PyPI
and are refused. If `main` or the branch head moved, publication of that `H`
is refused, and the next candidate is the way forward.

A GitHub prerelease can exist while publication is incomplete. The staging
selection record, not the prerelease, is the completion evidence.

## Staging selection

The `publish` job uploads `staging-selection.json` as a workflow artifact and
writes it to the job summary. The record lists `H`, `B`, `M`, branch, version,
Python version, image reference by digest, build run, and `selected`. While
`hotfix/new` exists, only its current head is selected (STD-U-821 1.2), and a
`release/new` candidate records `selected: false`. Consumers must recheck the
current heads under the same lock before using the record. This workflow
never deploys.

## Known limits

- GitHub keeps at most one pending run per concurrency group. A newer pending
  run in `data-olympus-promotion` cancels an older pending one, which is
  harmless because a superseded head is refused anyway.
- The Skopeo copy from a multi-platform Buildx OCI archive and the GHCR digest
  readback have only been exercised against fakes. The first enabled run after
  the R6 prerequisites is the live verification.
