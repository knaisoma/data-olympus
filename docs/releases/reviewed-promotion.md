# Reviewed release promotion

`promote-release.yml` implements STD-U-821 stable promotion beside the
transitional `tag-release.yml`, which shares its promotion lock. Every job is skipped unless the
repository variable `SDLC_PIPELINE` is `enabled` and the dispatch ref is
`main`. Dispatch it with these inputs:

- `squash`: the full SHA of the release squash `S`, which must be the head of `main`.
- `reviewed_head`: the full reviewed SHA `H` recorded by the published candidate.
- `candidate_tag`: the existing `X.Y.Z-rc.N` or `X.Y.Z-hotfix.rc.N` prerelease.
- `resume_pypi`: `false` (the default) for every normal promotion. Set it to
  `true` only to complete a promotion that uploaded the stable files to PyPI
  and stopped before or after the stable tag; see
  [resuming after the PyPI upload](#resuming-after-the-pypi-upload).

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
- The `pypi` environment, also restricted to `main` (deployment policy
  `main`, type branch; no `v*` tag policy), with no required reviewers
  (see [repository settings](repository-settings.md#3-environments)). Add a PyPI Trusted Publisher for `promote-release.yml` and
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
- A repository ruleset that limits who can create `v*` tags. It is not in
  place: the `w10-protected-tags` ruleset makes `v*` tags undeletable and
  unmovable but does not restrict their creation, because a creation rule
  cannot exempt `github-actions[bot]` (see
  [repository settings](repository-settings.md) and the `v*` tag creation item
  under [tracked prerequisites](#tracked-prerequisites)). The tagger check is
  self-asserted metadata and only an audit hint. The control that holds today
  is content equality: an existing tag is accepted only with the exact name
  `vX.Y.Z`, as an annotated tag whose object is `S`, and with a message that
  is byte for byte the generated notes, so any accepted tag is identical in
  content to the one the App would create.
- `SDLC_REQUIRED_CODEQL_CHECKS`: the exact required language-analysis check
  names, comma-separated, without spaces around the commas. An unset or
  malformed value fails closed. GitHub produces the aggregate `CodeQL` check
  run only on pull request heads (from the GitHub Advanced Security app); a
  push to `main` gets only the per-language analyses. `H`, the head of the
  release pull request, therefore needs successful `test` and aggregate
  `CodeQL`; `S`, the push to `main`, needs successful `test` and each of
  those analyses. Pull request code scanning analyses the merge commit of the
  pull request, not `H` itself. That merge has the tree of `H`, because the
  version engine requires the pre-squash `main` to be an ancestor of `H` (it
  refuses with `recut_required` otherwise), and the proof shows that `S`, whose
  sole parent is the cut `B`, has the tree of `H`. So the aggregate result on
  `H` and the analyses of `S` cover the same code. The aggregate `CodeQL`
  result means no new alerts relative to the base; zero open alerts is a
  separate requirement checked by `scripts/security_alerts.py`. A check that
  is not required for the role (the analyses on `H`, `CodeQL` on `S`) may be
  absent, but if it ran it must have succeeded.
  `release_record.py checks` applies the rule: a check run counts only when its
  `head_sha` is exactly the commit and its app is GitHub Actions or code
  scanning (for the aggregate `CodeQL`, only GitHub Advanced Security, the
  only observed producer, or code scanning as the alternate slug; a workflow
  posting a run named `CodeQL` is refused), and the latest such run (highest
  id) of each name decides; it must be completed with conclusion `success`
  (neutral, skipped, cancelled, timed out, action required and stale are
  refused).

  Lesson: the earlier design required the aggregate `CodeQL` on `S` and
  assumed it exists on pushes to `main`. It exists only on pull request heads,
  which was discovered when the first promotion gate was about to run.
  Fail-closed behaviour made the error safe, because the gate refuses with
  `missing required check: CodeQL` instead of publishing; the rule now asks
  each commit for the evidence GitHub actually produces there.
- Adoption ratification needs no repository setting. This workflow's checkout
  is the squash `S`, whose tree equals `H`, so the proof does not use its own
  copy: when the cut carries `release/ADOPTION.json` it reads
  `scripts/adoption_ratification.py` and both vendored amendments (1.3 and 1.5)
  as blobs of the RC's recorded `M`, and it still refuses while the record exists at `H`. See
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
other notes, is refused. Git tagger metadata is self-asserted, so the tagger
check is an audit hint and not an access control, and no ruleset restricts who
may create `v*` tags today (a tracked prerequisite). The binding that holds is
the content: exact name, object `S`, and the generated notes as the message.
This keeps "Re-run failed jobs" usable after a partial
publication. Re-running all jobs is refused once anything is published.

The engine counts every stable tag as a released version. Once `vX.Y.Z` exists
on `S`, recomputing the same release would see a stable version above its own
base: an adoption record must name the highest stable tag, a hotfix the
current one, and a first release none. The resume phases therefore pass
exactly that one tag to the engine as `ignore_tags`, and only after verifying
it: it must be annotated, its tag object must name `S` (not another tag or
commit) and `vX.Y.Z`, and its tagger must be the App bot identity. After the
notes are generated, the proof also requires its message to be byte for byte
those notes. If any check fails, the tag is not ignored and the proof fails
closed. The engine refuses any name that is not an exact `vX.Y.Z` as well as an
ignored tag on the cut itself, ignores nothing by default, and no other caller
passes the option. Notes generation and base selection are unchanged.

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
   files already present. `release_record.py verify-pypi` then requires PyPI
   to list exactly the wheel and the sdist named by the stable provenance, with
   the hashes of the build of `S`. The publish action writes a
   `<file>.publish.attestation` next to each uploaded file; PyPI does not list
   those, so the verifier excludes them from the comparison, requires both
   (non-empty) when the upload ran and their absence when it did not, and
   refuses any other file in the directory. Bumping the pin of
   `pypa/gh-action-pypi-publish` requires re-checking the names of the files
   it writes next to the distributions (today `<file>.publish.attestation`),
   because any other name makes the verifier fail closed.
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

## Resuming after the PyPI upload

Use `resume_pypi: true` when a promotion published the stable wheel and sdist
to PyPI and then stopped, either before the stable tag or after it. A normal
dispatch is refused in that state: the `initial` proof requires `main` head to
equal `S` (which no longer holds once `main` has advanced) and requires the
stable tag to be absent, and `version_free.py` requires the version to be
absent from PyPI. "Re-run failed jobs" reuses the run's original workflow
definition and scripts, so it can continue only runs that started on code that
already handles their state.

Operator checklist:

- Dispatch from `main` with `squash`, `reviewed_head` and `candidate_tag`
  equal to the stopped run's. For the first live promotion that is run
  37942200995: `squash` `bd26f087fcbed98ed16aaf0731db47eef111163d`,
  `reviewed_head` `9d91f8525c002f9aedf6ac1b2a68cc16a74d389c`, `candidate_tag`
  `0.11.1-rc.5`. The proof binds the three to each other (the candidate tag
  must resolve to the recorded `H`, and `S` must be the squash of that `H` on
  the recorded `B`), and `verify-pypi` requires the files on PyPI to equal the
  rebuild of that `S` byte for byte, but nothing compares the inputs with the
  stopped run itself.
- Approve the `pypi` environment once more when `publish-pypi` waits, if the
  environment has a required reviewer.
- `set-channel.yml` and `tag-release.yml` take the `data-olympus-promotion`
  lock, so they wait for the running promotion instead of moving `stable` or
  `latest` or creating a `vX.Y.Z` release between `prove` and the later jobs.
  Do not dispatch them while a resume is queued: GitHub keeps one pending run
  per group, and the new dispatch would replace the queued resume. Dispatch the
  resume again if that happens.
- After the run, check that the new release is the latest one:
  `gh api repos/knaisoma/data-olympus/releases/latest --jq .tag_name` must
  print the promoted `vX.Y.Z` (`v0.11.1` for the first live promotion). The
  release job publishes the draft without setting `make_latest`, so GitHub's
  default decides; correct it by hand if needed.

The resumed run proves everything a normal run proves about `S`, `H`, `B`, `M`,
the candidate tag, its provenance, image digest, labels and stage-two
attestations, the exact-source gate (`test` and aggregate `CodeQL` on `H`,
`test` and the analyses on `S`) and zero open alerts, with two differences:

- The proof runs in the `resume-pypi` phase. `S` must be an ancestor of
  `main`, which may have advanced past it (for example by the fix that made
  the resume necessary). A stable tag is accepted exactly as in `resume`: only
  the App's own annotated tag on `S` carrying the generated notes, verified
  before the engine ignores it (see [Proofs](#proofs)). The `prove` job
  resolves the App bot identity before its proof for this reason. A
  lightweight tag, a tag on another commit or object, another tagger or other
  notes are refused.
- `release_record.py resume-state` replaces `version_free.py`. PyPI must
  already hold the stable version with exactly one wheel and one sdist of that
  version and nothing else (none yanked, and an entry without a `yanked`
  state is refused). Any registry or release that cannot be read fails
  closed. If PyPI
  lacks the version, resume is impossible and the normal dispatch is the path.
  The rest depends on the tag the proof saw, which the step passes as the
  local tag object (no fetch happens between the proof and this check):
  - Without a stable tag, nothing after PyPI may exist. The Git tag on GitHub,
    the GHCR `vX.Y.Z` tag and a GitHub release `vX.Y.Z` among the releases
    visible to the job's read-only token must be absent, and GHCR `stable`
    and `latest` must not point at the candidate digest. GitHub lists draft
    releases only to tokens with push access, so a draft is probably
    invisible here; the `release` job refuses a foreign draft (one whose body
    or assets are not this run's) and never publishes it.
  - With the verified tag, GitHub's ref `refs/tags/vX.Y.Z` must name that same
    tag object. The states the later jobs leave behind are accepted, so the
    resume stays idempotent: GHCR `vX.Y.Z` is absent or on the candidate
    digest, never on another one; `stable` or `latest` may point at the
    candidate only if `vX.Y.Z` already does, because `promote-image` writes
    the version tag first; otherwise they keep whatever digest they had.
    There is no recorded previous digest to compare them with, and
    `set-channel.yml` moves the two channels independently, so equality
    between them cannot be required. `promote-image` sets and reads back all
    three. A GitHub release visible to the job's token (a published one, or
    a draft if the token can see drafts) is checked here, before
    `promote-image` can move a channel. Its body must be the generated notes,
    it must not be a prerelease, every asset name must be one of the four
    this run uploads (the PyPI wheel and sdist, `release-provenance.json` and
    `release-record.json`), and an asset digest that GitHub reports must equal
    the PyPI hash or this run's release record. A published release must
    already hold all four, since an immutable release can never gain an
    asset. The `release` job checks every byte again, completes a draft and
    publishes it last, and refuses anything else, including a foreign draft
    this token could not see.

`build-stable` runs unchanged and compares the rebuild of `S` with both
candidate payloads. `publish-pypi` stays bound to the `pypi` environment.
After any approval it rechecks the proof in the `resume-pypi` phase, rechecks
the alerts, skips only the upload step, and runs `verify-pypi` without
attestations: the files already on PyPI must equal the rebuild of `S` byte for
byte. `create-tag` rechecks the proof. If the verified tag already exists, it
neither pushes nor moves it; otherwise it creates and pushes it and rechecks
again. The image channels, the GitHub release and the MCP registry then run as
in a normal promotion. A run started on this code that fails later can be
continued with "Re-run failed jobs" or with another `resume_pypi` dispatch.

Lesson 1: the verifier assumed a clean distribution directory, and no live
promotion had run before the first one. In run 37942200995 (`0.11.1-rc.5`) the
upload succeeded and the verifier failed, because the directory also held the
two attestation files the publish action writes. That left PyPI with `0.11.1`
(wheel `7fbbfa13...`, sdist `b0d5853f...`, equal to the build of `S`) and
nothing else: no tag, no channel move and no GitHub release.

Lesson 2: the recheck after the tag ran the adoption proof again. The adoption
record is single use, since it names `v0.11.0` as the highest stable tag, so
every promotion of an adoption cut failed its own post-tag recheck. The same
held for hotfix promotions (the hotfix scope requires the base to be the
highest stable tag) and first releases. No test caught it, because the proof
fixtures with an existing tag used an ordinary release cut. The first live
promotion found it. Resume run 37958694040 (from `5f348d9`) published nothing
new on PyPI and verified the files. It then pushed the App's `v0.11.1` on `S`
(tag object `bcf25d21...`, message byte for byte the 61,199 bytes of generated
notes), and its post-push recheck failed with `bad_adoption`. The image
channels, the GitHub release and the MCP registry were skipped. "Re-run failed
jobs" of that run would fail again, because it runs `5f348d9`'s code. A normal
dispatch is refused because `main` has advanced past `S`. Once the fix is on
`main`, the way on is one new `resume_pypi: true` dispatch with the inputs
above. `tests/test_promotion_resume_after_tag.py` reproduces that live shape
and runs the real step scripts of every remaining job against fake GHCR,
GitHub, PyPI and registry services.

## Old and new promotion paths during the R9 window

Until the first new-model release, `tag-release.yml` remains a supported
promotion path beside `promote-release.yml`. Both paths move `stable` and
`latest`, so running them at the same time could leave the channels on
whichever finished last, which may be the lower version. The mutual exclusion
is enforced by the shared concurrency group: `tag-release.yml` and
`set-channel.yml` take the workflow-level group `data-olympus-promotion` with
`cancel-in-progress: false`, the same lock that `promote-release.yml`,
`rc-publish-stage.yml`, `recut-release-branch.yml` and `hotfix-cut.yml` take.
`tag-release.yml` no longer has its own `tag-release-<candidate>` group, so two
dispatches for different candidates now serialise as well, which is intended.

- `tag-release.yml` enters the lock only when dispatched from `main` (its
  `decide` job refuses any other ref); otherwise it gets a private
  `tag-release-noop-<run id>` group and cannot replace a pending run. A `main`
  dispatch with a malformed `candidate_tag` still enters the lock, because the
  input format cannot be tested in a concurrency expression; it then fails in
  `decide` and may replace a pending run (dispatch the replaced run again).
- `set-channel.yml` is unconditional: it has no ref guard and acts from any
  ref, so every run must hold the lock.

Recorded R9 deviation (operator follow-up decision of 2026-10-10): R9 kept both
files unchanged, and this change alters their concurrency group inside the R9
window. It changes grouping only. Inputs, permissions, jobs, steps and
published artifacts are byte-identical, and the R9 golden tests are unaffected.

One limit remains: GitHub keeps only one pending run per concurrency group, and
a newer pending run cancels an older pending one (never an in-progress one).
The replaced run may be a promotion or a stage 2 publication, not only an
old-path run. If a queued dispatch of any lock member disappears, check the run
list and dispatch it again once the current run completes. After a run completes, verify
the channels with `docker buildx imagetools inspect
ghcr.io/knaisoma/data-olympus:stable` and `:latest`.

## Tracked prerequisites

These are not implemented and block activation:

- `v*` tag creation restriction. Candidate tags (`X.Y.Z-rc.N`) have no `v`
  prefix, so the rule concerns stable tags only. The old `tag-release.yml`
  pushes `v*` tags as `github-actions[bot]`, which a ruleset creation rule
  cannot exempt, so the rule is infeasible while the old path is in use. On
  the new path stable tags are created by the `sdlc-bot` App, which the rule
  would need as a bypass actor. Until it exists, the tagger check above remains
  an audit check and not an access control.
- Pin the build backend (hatchling). The stable rebuild of `S` must equal the
  candidate payloads and, on a resumed promotion, the files already on PyPI
  byte for byte; an unpinned build backend can drift between the candidate
  build and the rebuild and make an otherwise valid promotion fail.
- Check runs tied to their producing workflow. The gate accepts a `test` or
  `Analyze (...)` check run from the GitHub Actions app by name and exact
  `head_sha`, so a check run that an unrelated workflow posts through the
  checks API under the same name could satisfy it. Tie each to the workflow
  run that produced it (for example through
  `actions/runs?head_sha=...`, matching the CI and CodeQL workflow paths)
  before relying on these names alone.
