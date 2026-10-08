# Trusted release candidate publication (stage 2)

`rc-publish-stage.yml` publishes a candidate built by `rc-build.yml`. It is
disabled unless the repository variable `SDLC_PIPELINE` is exactly `enabled`.
Stage 2 itself needs no bot credential: it uses only the job's `GITHUB_TOKEN`
and OIDC (PyPI Trusted Publishing and Sigstore) and never falls back to a
personal token. The W9 machine identity is not a stage-2 credential. It is
needed for what surrounds this workflow: the R6 `release/new` ruleset with
code-owner review of `.github/` and `scripts/` (or owner action to create it),
and the unattended merges, recuts and pushes of the pipeline. The existing
`rc-publish.yml`, `tag-release.yml` and `set-channel.yml` keep their behaviour
(R9).

## Activation prerequisites (MUST)

These carry the R8 boundary ("code merged to `release/new` must never run with
publish credentials"). `SDLC_PIPELINE` MUST NOT be set to `enabled` until every
item holds:

- The PyPI Trusted Publisher for `data-olympus` MUST be owner `knaisoma`,
  repository `data-olympus`, workflow `rc-publish-stage.yml`, environment
  `pypi-rc`. PyPI matches repository and workflow file name, not the ref, so
  without the environment pin any branch could add a push-triggered file of
  that name and publish.
- The `pypi-rc` environment MUST allow deployments from `main` only.
- `promote-release` (Task 5) MUST verify every reused object with
  `gh attestation verify ... --signer-workflow
  knaisoma/data-olympus/.github/workflows/rc-publish-stage.yml --source-ref
  refs/heads/main` (see the contract below) and MUST NOT treat the existence of
  the `rc` tag, a version tag, a candidate Git tag or a GitHub prerelease as
  evidence. Push workflows can write all of those with `GITHUB_TOKEN`.
- The R6 ruleset on `release/new` (pull request required, code-owner review of
  `.github/` and `scripts/`) MUST exist. Activation is blocked until it does,
  because only that review stops merged `release/new` code from adding a
  workflow that self-grants `contents: write` or `packages: write`.

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

An admitted run (enabled, successful `push` build of this repository's
`release/new` or `hotfix/new`, the same conditions as the `reserve` gate) holds
the shared `data-olympus-promotion` lock with cancellation disabled. Every
other run gets a private group `rc-publish-noop-<run id>` and never touches the
shared lock. Each verifying job fetches history again and repeats the full
verification, so a moved branch head or a moved `main` fails closed.

| Job | Permissions | Action |
|---|---|---|
| `reserve` | `actions: read`, `contents: write`, `packages: read` | Creates an annotated tag at `H` and a public prerelease whose wheel, sdist and `release-provenance.json` assets bind the identity to `H` |
| `pypi` | `actions: read`, `contents: read`, `packages: read`, `id-token: write`, environment `pypi-rc` | Stages only the files missing from PyPI and uploads them with Trusted Publishing |
| `publish` | `actions: read`, `contents: read`, `packages: write` | Waits for the PyPI readback, copies the OCI archive with `skopeo copy --all --preserve-digests`, reads the remote digest back, moves `rc` by digest for `release/new` only when `SDLC_RC_CHANNEL` is `enabled`, writes the staging selection, and outputs the validated image digest and the wheel and sdist names and SHA256 |
| `attest` | `actions: read`, `attestations: write`, `id-token: write`, `packages: write` | Runs no repository code and parses no archive; re-checks the wheel and sdist with `sha256sum --check` against the `publish` outputs, then attests those copies and the image digest |

Before any write, every surface is read. A Git tag at another `H`, different
PyPI file hashes, a different image digest, or release assets with different
bytes are duplicate identities and fail the run. Objects that already exist
must carry the same `H` provenance. Registry errors other than an explicit
"not found" are outages and also fail closed.

`attestations: write` exists only on `attest`. `id-token: write` exists on
`pypi` (PyPI OIDC) and on `attest` (Sigstore signing), and nowhere else. The
job that parses the untrusted zip, tar, gzip and JSON (`publish`) never holds
the signing identity that promotion trusts.

## The `rc` channel

Stage 2 owns the GHCR `rc` channel for `release/new` heads, but the W6 spec
invariant keeps the `rc` digest and `set-channel.yml` semantics unchanged until
the first release under the new model. Until then the repository variable
`SDLC_RC_CHANNEL` is unset (default off), stage 2 never moves `rc`, and the old
`set-channel.yml` (its own `set-channel-rc` lock, unchanged per R9) is the only
writer of `rc`. Task 9 sets `SDLC_RC_CHANNEL=enabled` only after the first
new-model release is published. From then on `set-channel.yml` MUST NOT be used
for `rc`. A hotfix head never moves `rc`. The staging selection record carries
`rc_channel_moved`.

## Action pins

`actions/checkout`, `astral-sh/setup-uv`, `actions/attest-build-provenance`
and `pypa/gh-action-pypi-publish` (including the moving `release/v1` branch)
are pinned by full commit SHA with the tag in a trailing comment, and a test
enforces those exact SHAs. `actions/download-artifact` and
`actions/upload-artifact` keep tag pins.

## Secrets and environments

The workflow uses no secret today, and stage 2 needs no GitHub App token: its
writes are covered by `GITHUB_TOKEN` and OIDC. If a job ever needs an App
token, it follows the pipeline convention: the single `sdlc-bot` environment
(deployment branch policy `main` only, no required reviewers), the App id as
the non-secret variable `vars.SDLC_APP_ID`, and the private key as the
environment secret `SDLC_APP_PRIVATE_KEY`, declared only by the job that uses
it. A repository-wide Actions secret is not allowed. The `pypi-rc` environment
stays the PyPI environment and is restricted to `main` in the same way. `workflow_run` jobs run with the `main` ref, so these policies admit
this workflow and reject a definition from any other branch.
`tests/test_rc_verify_and_publish.py` enforces that a job referencing
`secrets.` declares an approved environment.

## Build provenance attestations (contract)

After the `publish` job succeeds, the separate `attest` job downloads the
same artifact, copies the wheel and sdist named by the `publish` outputs,
refuses symlinks, checks the copies with `sha256sum --check --strict` against
the verified SHA256 values, logs in to GHCR with `GITHUB_TOKEN` for the
registry push, and runs `actions/attest-build-provenance` twice:

- The image: subject `ghcr.io/knaisoma/data-olympus` at the verified and
  published digest, with the attestation also pushed to the registry.
- The wheel and sdist: subjects are the checked copies, byte-identical to the
  files that were hash-verified against provenance and uploaded to PyPI.

Promotion (`promote-release`, Task 5) MUST verify each object before reusing it:

```bash
gh attestation verify oci://ghcr.io/knaisoma/data-olympus@<digest> \
  --repo knaisoma/data-olympus \
  --signer-workflow knaisoma/data-olympus/.github/workflows/rc-publish-stage.yml \
  --source-ref refs/heads/main
gh attestation verify <wheel-or-sdist> --repo knaisoma/data-olympus \
  --signer-workflow knaisoma/data-olympus/.github/workflows/rc-publish-stage.yml \
  --source-ref refs/heads/main
```

The attestation proves that the trusted stage-two workflow on `main` verified
and published those bytes. It describes the stage-two run, not the stage-one
build. The link from the digest to `H`, `B` and `M` is the staging selection
record and `release-provenance.json`. A rerun of `attest` adds another
attestation for the same subjects, which is harmless.

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

- GitHub keeps at most one pending run per concurrency group and cancels the
  older pending one, whatever `cancel-in-progress` says. Disabled, failed,
  dry-run and other-branch runs no longer enter `data-olympus-promotion`, so
  they cannot cause this. Genuine contention still can: a pending `hotfix/new`
  publication can be cancelled by a later admitted `release/new` run, and once
  Tasks 5 and 6 join the group, a pending promotion or recut can be cancelled
  by an admitted stage-2 run (and the reverse). The cancelled run publishes
  nothing (fails closed), but a hotfix candidate that STD-U-821 1.2 says
  staging MUST run would then not publish. Recovery: re-run the cancelled
  `rc-publish-stage` run. It repeats the full verification and is refused if
  its head or `main` has moved, in which case the newer candidate is the way
  forward.
- The Skopeo copy from a multi-platform Buildx OCI archive, the GHCR digest
  readback, and the attestation steps have only been exercised against fakes
  and structure tests. The first enabled run after
  the R6 prerequisites is the live verification.

## Task 9 prerequisites

The adoption cycle cannot publish through this stage as it stands. The version
engine refuses an unratified adoption cut (`adoption_unratified`), and stage 1
(`rc_decide`) refuses adoption cuts. PR 330 fixes the engine heading match for
the ratified amendment (`## Amendment 1.3: ...`) in ratified mode. Task 9 MUST
wire both stages, the stage-1 build and this stage's recompute, with the same
`scripts/adoption_ratification.py` `engine_args()` so that both compute the
same identity; otherwise stage 2 refuses every adoption-cycle candidate as a
recompute mismatch. Task 9 also owns the `SDLC_RC_CHANNEL` switch above.
