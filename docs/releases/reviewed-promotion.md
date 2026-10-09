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

- The W9 GitHub App, installed on this repository only, with exactly these
  permissions: contents write, Dependabot alerts read and code scanning alerts
  read. The App pushes the stable tag and reads the alerts, which
  `GITHUB_TOKEN` cannot read.
- The environment `sdlc-bot`, with a deployment branch policy that allows
  only `main`. Its environment secret `SDLC_APP_PRIVATE_KEY` and environment
  variable `SDLC_APP_ID` hold the App credentials. The `prove` and
  `create-tag` jobs bind this environment, which has no required reviewers
  (the real approval is the `pypi` environment's).
- The protected `pypi` environment, also restricted to `main`, with required
  reviewers. Add a PyPI Trusted Publisher for `promote-release.yml` and
  environment `pypi`, and keep the existing publishers registered until
  adoption completes, because the R9 old path (`publish-pypi.yml`,
  `rc-publish.yml`, `tag-release.yml`) still publishes through them. A job binds only one
  environment, so `publish-pypi`
  (which rechecks the alerts and the tag after approval) needs its own copy of
  `SDLC_APP_PRIVATE_KEY` and `SDLC_APP_ID` as `pypi` environment secret and
  variable. Rotate both copies together.
- The App private key is never a repository or organization Actions secret.
  Those are available to workflows on every branch a writer can push, where
  the main-only guard lives in a workflow file the writer controls, so any
  writer could mint the release identity. Environment secrets are released
  only to jobs that run on an allowed deployment branch.
- Recorded R9 deviation (operator-delegated ruling 2026-10-08): `publish-pypi.yml`
  has no branch guard and accepts a dispatch from any ref. Restricting the
  `pypi` environment to `main` means manual recovery runs through
  `publish-pypi.yml` must be dispatched from `main`; the release tag is still
  selected by its `ref` input. `rc-publish.yml` and `tag-release.yml` already
  require `main`.
- A repository ruleset that limits who can create `v*` tags. The tagger check
  is self-asserted metadata, so this ruleset is the actual access control. It
  is not in place yet; see the `v*` tag creation item under
  [tracked prerequisites](#tracked-prerequisites).
- `SDLC_REQUIRED_CODEQL_CHECKS`: the exact required language-analysis check
  names, comma-separated, without spaces around the commas. An unset or
  malformed value fails closed. `H` needs a successful `test`; `S` needs
  successful `test`, aggregate `CodeQL` and each of those checks. Code
  scanning is guaranteed only on `main` and on pull requests into `main`, so
  `H` on `release/new` is not guaranteed CodeQL; `S` is. The proof shows that
  the trees of `S` and
  `H` are equal, so the code scanned on `S` is byte for byte the reviewed `H`.
  `release_record.py checks` applies the rule: a check run counts only when its
  `head_sha` is exactly the commit and its app is GitHub Actions or code
  scanning, and the latest such run (highest id) of each name decides; it must
  be completed with conclusion `success` (neutral, skipped, cancelled,
  timed out, action required and stale are refused).
- Adoption ratification needs no repository setting. This workflow's checkout
  is the squash `S`, whose tree equals `H`, so the proof does not use its own
  copy: when the cut carries `release/ADOPTION.json` it reads
  `scripts/adoption_ratification.py` and the vendored amendment as blobs of the
  RC's recorded `M`, and it still refuses while the record exists at `H`. See
  [the adoption cut runbook](adoption-cut-runbook.md).

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
cancellation (R-CONC), only for admitted runs (`main` with the pipeline
enabled); any other dispatch gets a run-scoped group and cannot cancel a
pending run. Right after acquiring it, the `initial` proof also
requires `main` head to equal `S` and the stable tag to be absent, and
`version_free.py` requires the version to be absent from PyPI, GHCR, GitHub
releases and Git tags. GitHub keeps only one pending run per concurrency group,
so a second dispatch queued behind a running promotion replaces any earlier
pending one; dispatch again if that happens.

The publishing jobs repeat the proof in the `resume` phase after `pypi`
environment approval and before tagging, and require the record to be byte
equal. `resume` accepts a `main` that has advanced past `S` (but not one that
no longer contains `S`) and an existing stable tag only if it is annotated and
on `S`, its tag object names `S` and `vX.Y.Z`, its message is byte for byte
the generated notes, and its tagger is the App bot identity
(`<app-slug>[bot]` with the `<id>+<app-slug>[bot]@users.noreply.github.com`
address, resolved from the App token). A tag pushed by anyone else, or with
other notes, is refused. Git tagger metadata is self-asserted, so this is an
audit check and not an access control: the W9 tag ruleset must restrict who may
create `v*` tags. This keeps "Re-run failed jobs" usable after a partial
publication. Re-running all jobs is refused once anything is published.

The proof job also requires zero open security alerts (repeated after
environment approval), that the RC GHCR tag still resolves to the recorded
digest, that the image's `org.opencontainers.image.version` and `revision`
labels equal the candidate tag and `H` on every platform, and that the
candidate wheel and sdist hashes match the provenance and PyPI.

Before the promotion inputs reach any later job, the proof job verifies the
GitHub artifact attestations that stage 2 signs (see
[build provenance attestations](rc-publication-stage.md#build-provenance-attestations-contract)).
For the candidate wheel and sdist (the exact files it just checked against
the provenance and PyPI) and for
`oci://ghcr.io/knaisoma/data-olympus@<recorded digest>` it runs:

```bash
gh attestation verify <subject> --repo knaisoma/data-olympus \
  --signer-workflow knaisoma/data-olympus/.github/workflows/rc-publish-stage.yml \
  --source-ref refs/heads/main --deny-self-hosted-runners
```

Any failure stops the run. The job's existing `ghcr.io` login serves the image
read, and the job adds `attestations: read` so `GITHUB_TOKEN` can read the
attestations; its permissions are `contents: read`, `checks: read`,
`packages: read` and `attestations: read`. Every later job needs `prove`, so
nothing is published before this check. The exact certificate identity
(`--cert-identity` with
`https://github.com/knaisoma/data-olympus/.github/workflows/rc-publish-stage.yml@refs/heads/main`)
is not used because `gh` refuses it together with `--signer-workflow`, which
matches only the workflow path. `--source-ref refs/heads/main` pins the ref,
and since stage 2 is not a reusable workflow its signing workflow file comes
from that same ref. This binds the promoted digest and files to
the trusted stage-two workflow on `main`. The candidate's
`release-provenance.json` is not itself attested: it is anchored by the
published, immutable candidate release and by the candidate tag message,
which records its SHA-256 (promotion does not compare that hash today).

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
2. The App creates the annotated `vX.Y.Z` tag on `S` as its bot identity,
   carrying the notes verbatim (`--cleanup=verbatim`; the default cleanup
   would strip the Markdown headings). The checkout credential is the App
   token, never `GITHUB_TOKEN` or a human token. After the push the tag is
   fetched back without force and the resume proof runs again.
3. Exactly `vX.Y.Z`, `stable` and `latest` move to the RC digest, as
   `tag-release.yml` does today, without rebuilding. The image keeps its RC
   version and revision labels; the stable provenance records the alias.
4. The GitHub release with the generated notes, stable files, stable
   provenance and release record. `release_record.py release` binds every
   asset to the run: the stable provenance must carry the release record's
   `H`, `S`, `B`, `M`, tag, candidate and digest, and its stable wheel and
   sdist hashes must equal the files. Immutable releases are enabled, so the
   workflow creates the release as a draft without assets, and the script
   uploads the missing assets, verifies the complete draft, and publishes it
   last (`gh release edit --draft=false`), then verifies it again as
   published. An existing release (draft or published) is accepted only if it
   is not a prerelease, its body equals the generated notes and every asset it
   has is one of these four files with the same SHA-256 (the downloaded bytes
   and, when GitHub reports it, the asset digest). Foreign assets are refused
   and a foreign draft is never published. Only missing assets are uploaded,
   without replacement, and only to a draft: a published release that lacks an
   asset cannot be completed, is refused as burned, and needs a new stable
   version.
5. The MCP registry, last, with the pinned publisher, ownership marker, OIDC
   login and read-back of `tag-release.yml`. Only the job's workspace copy of
   `server.json` receives the version.

Recovery from a partial publication never replaces or re-tags a published
item. The workflow never deploys: production stays digest-pinned in reviewed
gitops, and rollback uses the recorded prior digest with `set-channel.yml`.

## Old and new promotion paths during the R9 window

Until the first new-model release, `tag-release.yml` stays unchanged (R9) and
does not take the `data-olympus-promotion` lock; it uses its own
`tag-release-<candidate>` group. Both paths move `stable` and `latest`, so
running them at the same time can leave the channels on whichever finished
last, which may be the lower version. Code cannot prevent this without
changing `tag-release.yml`, so the operator keeps them mutually exclusive:

1. Never dispatch `tag-release.yml` while a `promote-release.yml` run exists
   in any non-completed state, and the reverse. A run waiting for `pypi`
   approval counts as running.
2. Before dispatching either workflow, check both are idle:

   ```bash
   for workflow in tag-release.yml promote-release.yml; do
     for status in queued in_progress waiting pending requested action_required; do
       gh run list --workflow "$workflow" --status "$status" \
         --json databaseId,status,headBranch,createdAt
     done
   done
   ```

   Every list must be empty (`[]`). If one is not, wait for it to complete
   or cancel the queued run that should not proceed.
3. After a run completes, verify the channels with
   `docker buildx imagetools inspect ghcr.io/knaisoma/data-olympus:stable`
   and `:latest` before dispatching the other path.

## Tracked prerequisites

These are not implemented and block activation:

- `v*` tag creation restriction. Candidate tags (`X.Y.Z-rc.N`) have no `v`
  prefix, so the rule concerns stable tags only. The old `tag-release.yml`
  pushes `v*` tags as `github-actions[bot]`, which a ruleset creation rule
  cannot exempt, so the rule is infeasible while the old path is in use. On
  the new path stable tags are created by the `sdlc-bot` App, which the rule
  would need as a bypass actor. Until it exists, the tagger check above remains
  an audit check and not an access control.
- Shared lock on the old path. After adoption, when `tag-release.yml` is
  retired or changed, the remaining stable promotion path must take the
  `data-olympus-promotion` lock (Task 6). Until then the procedure above is
  the only mutual exclusion.
