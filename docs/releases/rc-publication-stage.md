# Trusted release candidate publication (stage 2)

`rc-publish-stage.yml` publishes a candidate built by `rc-build.yml`. It is
disabled unless the repository variable `SDLC_PIPELINE` is exactly `enabled`.
Stage 2 itself needs no bot credential: it uses only the job's `GITHUB_TOKEN`
and OIDC (PyPI Trusted Publishing and Sigstore) and never falls back to a
personal token. The W9 machine identity is not a stage-2 credential. It is
needed for what surrounds this workflow: the R6 `release/new` ruleset with
code-owner review of `.github/`, `scripts/`, `release/` and the vendored
amendment `docs/releases/std-u-821-amendment-1.3.md` (or owner action to create it),
and the unattended merges, recuts and pushes of the pipeline. The existing
`rc-publish.yml`, `tag-release.yml` and `set-channel.yml` keep their behaviour
(R9).

## Activation prerequisites (MUST)

These carry the R8 boundary ("code merged to `release/new` must never run with
publish credentials"). `SDLC_PIPELINE` MUST NOT be set to `enabled` until every
item holds:

Set `SDLC_PIPELINE=enabled` only while no `rc-build` run is in progress: the
lock group is evaluated when a run is queued and the job gate when it starts,
so a run admitted across the switch would execute outside the shared lock.
Re-run any `rc-publish-stage` run that was skipped around activation.

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
- GitHub immutable releases stay enabled on the repository. Stage 2 relies on
  them (a published candidate release can never change) and is built for them:
  see [Release ordering under immutable releases](#release-ordering-under-immutable-releases).
- The R6 ruleset on `release/new` (pull request required, code-owner review of
  `.github/`, `scripts/`, `release/` and
  `docs/releases/std-u-821-amendment-1.3.md`) MUST exist. Activation is blocked until it does,
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
| `reserve` | `actions: read`, `contents: write`, `packages: read` | Creates an annotated tag at `H` and a draft prerelease, uploads the wheel, sdist and `release-provenance.json` and reads every asset back byte for byte; the release stays a draft |
| `pypi` | `actions: read`, `contents: read`, `packages: read`, `id-token: write`, environment `pypi-rc` | Stages only the files missing from PyPI and uploads them with Trusted Publishing |
| `publish` | `actions: read`, `contents: read`, `packages: write` | Waits for the PyPI readback, copies the OCI archive with `skopeo copy --all --preserve-digests`, reads the remote digest back, moves `rc` by digest for `release/new` only when `SDLC_RC_CHANNEL` is `enabled`, writes the staging selection, and outputs the validated image digest and the wheel and sdist names and SHA256 |
| `attest` | `actions: read`, `attestations: write`, `id-token: write`, `packages: write` | Runs no repository code and parses no archive; re-checks the wheel and sdist with `sha256sum --check` against the `publish` outputs, then attests those copies and the image digest |
| `finalize` | `actions: read`, `contents: write`, `packages: read` | Runs last (after `publish` and `attest`); repeats the full draft-aware verification, requires every release asset, every PyPI file and the image digest, then publishes the draft as a prerelease and reads it back as published |

Before any write, every surface is read. A Git tag at another `H`, different
PyPI file hashes, a different image digest, or release assets with different
bytes are duplicate identities and fail the run. Objects that already exist
must carry the same `H` provenance. Registry errors other than an explicit
"not found" are outages and also fail closed.

## Release ordering under immutable releases

The repository has GitHub immutable releases enabled. Once a release is
published, its assets can never be added, replaced or deleted, and its tag is
locked. Drafts stay mutable. The stage therefore runs in this order:

1. `reserve`: annotated tag at `H` whose message binds every asset hash (see
   below), then a draft prerelease
   (`gh release create --draft --verify-tag --prerelease`), read back at once
   and refused unless it is still a draft, then every missing asset uploaded
   without `--clobber`, then a byte-for-byte readback of every asset against
   the verified files. All of this happens before `pypi` starts.
2. `pypi`: Trusted Publishing upload of the missing Python files.
3. `publish`: PyPI readback, image push by digest, optional `rc` move,
   staging selection.
4. `attest`: provenance attestations for the image and the Python files.
5. `finalize`: full re-verification, then the draft is published
   (`draft: false`, still a prerelease, never marked latest).

`attest` and `finalize` carry explicit conditions
(`!cancelled() && needs.publish.result == 'success'`, and for `finalize` also
`needs.attest.result == 'success'`). Without them a skipped `pypi` job (PyPI
already holds the files, for example after "Re-run all jobs") would skip both,
and the run would end green with an unpublished draft and no attestation.

A published candidate release therefore always means a complete publication,
and promotion, which requires a published prerelease, never sees a partial one.

Drafts are not returned by `GET releases/tags/{tag}`, so the stage finds the
release by listing `releases` page by page and matching `tag_name` exactly; more
than one match fails closed. Assets are read by asset id. An asset whose upload
was interrupted (state other than `uploaded`) or whose bytes differ from the
verified file is never repaired automatically: the run fails and names the
draft asset for an operator to review and delete.

Draft releases are listed only to tokens with push access. `pypi` and
`publish` hold `contents: read`, so for them a missing release is not proof of
absence. They bind the reservation through the annotated candidate tag, which
`contents: read` can read, and refuse any published release that is
incomplete. The tag message is exactly these lines joined by a newline, with
no trailing newline (Git may append one, which is the only tolerated
difference):

```text
Candidate <version>
H: <H>

sha256 <64 lowercase hex>  <asset name>
```

with one `sha256` line per asset (wheel, sdist, `release-provenance.json`),
sorted by name. Every phase requires an existing tag to carry exactly the
message computed from this run's verified files, so the bytes that PyPI and
GHCR receive are bound to the reservation even when the draft is invisible.
A tag in the older format (`Candidate <version>` and `H:` only, as on
`0.11.1-rc.2`) or with other hashes is refused; such a tag either belongs to a
burned candidate or to a reservation of other bytes for the same `H`.
`reserve` and `finalize` hold `contents: write` and always see the draft, so
the full byte comparison against the release assets is done before PyPI and
again before publication.

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

The `rc` move and the staging selection run in `publish`, before `attest` and
`finalize`. A published candidate release implies a complete publication, but
the reverse does not hold: if `attest` or `finalize` fails, or a head moves,
`rc` (once enabled) and the selection can point at a candidate whose release
is still a draft without attestations. The completion evidence is a
successful `finalize`. This is dormant while `SDLC_RC_CHANNEL` is unset.
Enabling `SDLC_RC_CHANNEL` therefore requires moving the `rc` move (and the
selection, if consumers treat it as completion) into a job that runs after
`finalize`, or enabling it only together with that change.

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
stays the PyPI environment and is restricted to `main` in the same way.
`workflow_run` jobs run with the `main` ref, so these policies admit this
workflow and reject a definition from any other branch.
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

Promotion (`promote-release`, Task 5) MUST verify each object before reusing
it:

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

A draft release can exist while publication is incomplete; it is invisible to
the public and reused (missing assets only) by a retry. A published release is
complete by construction.

A published candidate release that lacks an asset or carries different bytes
cannot be repaired under immutable releases. Every phase refuses it with a
message that the candidate is burned. Its tag and release are permanent; the
next push to `release/new` (or `hotfix/new`) yields the next rc number, and
that candidate is the way forward.

When a `gh` or `skopeo` command fails, the refusal carries a sanitized excerpt
of its stderr: one line of printable ASCII, at most 300 characters, with
credential-shaped text and the `GH_TOKEN`/`GITHUB_TOKEN` values redacted and
workflow-command markers (`::`, `##[`) broken up. Stdout, arguments and input
are never shown.

## Known burned candidate: 0.11.1-rc.2 (empty immutable release and tag, created by the first live run before the draft-first fix)

The first live run created and published the `0.11.1-rc.2` prerelease
(release id 407441249) before uploading any asset, the order used before the
draft-first fix. Immutability then refused the asset upload, so that release
is published, immutable and empty, and its tag is permanent. It is never
reused: every phase refuses it as burned. The first complete candidate this
stage can publish under the adoption sequence is therefore `0.11.1-rc.3` or
later, from the next push to `release/new`.

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

## Adoption cycle

Both stages pass the same pinned ratification, so they compute the same
identity for an adoption cut. This stage imports `RATIFIED` and `STANDARD_FILE`
from `scripts/adoption_ratification.py` in its own `main` checkout and resolves
the vendored amendment relative to it; stage 1 reads the same two files as
blobs of `origin/main`, and promotion as blobs of the recorded `M`. Nothing comes from `H`, the artifacts or workflow
inputs, and this stage has no adoption dry-run mode. The cut build (`N=0`) and
the head after the R4 placeholder (record still at `H`, not promotable) are
refused here; the first candidate this stage can publish is the head after the
record is retired (`0.11.1-rc.2` in the planned sequence; that candidate is
burned, see above, so the first complete candidate is `0.11.1-rc.3` or later). The threat model and
the cut sequence are in [the adoption cut runbook](adoption-cut-runbook.md).
`SDLC_RC_CHANNEL` stays unset until the first new-model release.
