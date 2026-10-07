# Data Olympus release contract

Status: active
Since: 2026-09-05

## Approval

Target Monday availability. The agent holding the operator's authorization
merges after the required review and gates. Reserved cases, including migrations
and destructive changes, require explicit operator authorization. Never bypass
protections. External announcements are human-published.

The target model follows STD-U-821 v1.2 and `.rules/versioning.md`: feature PRs
target `release/new`, integration builds content-derived RCs, and the batch is
squashed to `main` with `release: X.Y.Z`. Do not commit a target package version.
Keep it open and unmerged until the implementer and
independent reviewer agree all blockers are resolved. Record review of `H`, the
exact PR head, its tree `T`, and base `B`. Any content change invalidates approval.
Recheck expected head `H` and unchanged base before authorized merge.

## New-model delivery and activation

The workflow names below describe the target pipeline, delivered separately
from these rules, not yet in the repository. New publication and branch-management
workflows remain disabled behind `SDLC_PIPELINE=enabled` until the bot or GitHub
App is provisioned with protected-branch deletion/recut permissions, the `release/new` ruleset
(PR required, code-owner review for `scripts/` and `.github/`), and the
`pypi-rc` environment with its own PyPI Trusted Publisher. Missing prerequisites
mean BLOCKED; never substitute the operator's personal token. Product static
token machine identity is separate work and is not granted here.
The adoption-cut and public-product preview proposals need operator
ratification. An adoption issue is still to be opened; neither that issue nor
the internal-tool exception authorizes these proposals. CI runs tests and
wheel/sdist smoke checks per PR. There is no hosted preview and no per-PR image.
The RC image and wheel will come from `rc-build.yml` once it is enabled.

1. `rc-build.yml` builds wheel, sdist, OCI archive and provenance at exact `H`
   on `release/new` or `hotfix/new`, with `contents: read`, no secrets and no
   OIDC token. Upload workflow artifacts; `N=0` stops without publication.
2. `rc-publish-stage.yml` runs from trusted `main` via `workflow_run`, after a
   successful allowed-branch build. Treat `H` and its history as data; never
   run its code with publish credentials. Recompute the version and verify
   provenance, wheel/sdist hashes and OCI archive digest before publishing
   through `pypi-rc`, GHCR and a GitHub prerelease. Move `rc` only for normal
   release heads. Select staging by current `H` and digest, prioritizing
   `hotfix/new` while it exists, never by version order.
3. Under the exclusive `data-olympus-promotion` lock, recheck head, base,
   staging and STD-U-822 gates. Squash to main with `release: X.Y.Z` and
   generated notes. Prove sole parent `B`, tree equality with reviewed `H`,
   and absence of `release/ADOPTION.json` at `H`.
4. `promote-release.yml` creates annotated `vX.Y.Z` on that squash, builds
   stable Python metadata from it and checks payload equivalence to the RC.
   Keep the protected `pypi` environment and MCP registry publication gate.
   Reuse the tested OCI digest, retaining its RC label. Provenance maps `H`,
   squash SHA, stable tag and digest. Move `stable`, `vX.Y.Z` and `latest`
   only after verification, as on the transitional promotion path.
5. Deploy stable-tag-authorized artifacts through reviewed digest-pinned
   gitops changes; the publication workflow itself does not deploy. Record
   the prior digest and verify delivery before recutting `release/new`.
   Move open PRs and preserve pending work with fresh checks and review.

Stage 2, promotion, hotfix and recut share the lock with cancellation disabled.
It covers staging validation, merge, tag, delivery verification and recut;
stale jobs recheck head/base after acquiring it. Unresolved state must be
reconciled, never discarded by cancellation. Hotfixes use `hotfix/new` from
current stable main, fixes only and `X.Y.Z-hotfix.rc.N`, with the same gates.
After verification delete the hotfix branch and recut the normal branch.

## Required gates

Use Python 3.13 and install the editable development distribution. Run lint first:

```bash
uv venv --python 3.13
uv pip install -e '.[dev]'
uv run ruff check .
uv run mypy src
uv run pytest -v
bats -r tests
uv run python scripts/okf_conformance.py verify-pin
uv run python scripts/check_benchmark_docs.py
uv run data-olympus lint example-bundle
```

Require isolated installed wheel and sdist smoke tests, exact SHA CI success,
and zero open security alerts using `scripts/security_alerts.py`. Dependency
vulnerability remediation and CodeQL fixes are mandatory. Require the aggregate
`CodeQL` check and successful required language analyses with zero findings.
Missing, stale, unreadable, or ambiguous evidence blocks. GitHub Code Quality is
not a dependency. Resolve all review threads and failed required checks. Never
dismiss alerts, waive tests, or bypass the entire GitHub ruleset to meet a date.

### Candidate remediation versus post-merge closure

Publication requires zero open security alerts. That requirement is never
waived, satisfied by prediction, or replaced by any check below.

A repository security alert is raised against the default branch, so an alert
whose fix lives on an unmerged branch can stay open until that branch merges.
That mechanical lag is the only reason a final release pull request may be
handed to its authorized merger while an alert is still open, and only when all of
the following hold:

* The open alert's fix is present in the exact reviewed candidate.
* Its closure is waiting only for that fix to reach the default branch. An
  alert that is unresolved, unrelated, unreadable, or fixed somewhere other
  than this candidate never qualifies.
* Per-alert remediation proof is recorded against the exact candidate: the
  advisory, its vulnerable and fixed version range, the resolved lockfile and
  built-artifact dependency versions, the affected code paths, candidate CodeQL
  results, and regression and compatibility checks.
* The implementer and the independent reviewer both agree, on that evidence,
  that every open finding is fixed in this candidate.
* The pull request states plainly that closure is pending its authorized merge.

No other exception exists. A pending merge is not remediation, an anticipated
automatic closure is not evidence, and a dependency bump alone is not security
clearance. A failed live security scan is retained as failed; it is never
rewritten as passing.

Building and inspecting an unpublished distribution or image locally is part
of assembling that proof and is expected before handoff. It is validation, not
release: nothing built this way is uploaded, tagged, promoted, or served.

After merge, publication still requires fresh security clearance showing zero
open alerts, plus exact-source CI and CodeQL success, before any candidate or
stable artifact is *published*. If closure lags behind the merge, reconcile it
later. Never dismiss an alert or bypass the gate to reach a date.

## Merge and content proof

Authorized merge permits delivery of reviewed content only. Fetch squash SHA `R`;
prove its tree exactly equals reviewed tree `T`. Require a squash merge with
sole parent `B`, preserving linear history. Other merge methods are not supported.
Changed base or content requires new review. Candidate fields remain `H`;
provenance maps `H` to `R`. Rerun checks and security clearance on `R`.
The version engine's `M` remains the captured pre-merge main SHA.

## Transitional path until the first new-model release

The existing `rc-publish.yml`, `tag-release.yml` and `set-channel.yml` retain
their behavior until the first new-model release, including 0.11.x hotfix and
rollback capability. This section applies only to that old path. Prepare a
short-lived branch such as `codex/release-YYYY-MM-DD` with the completed batch,
computed version, lockfile, changelog and release notes. Use
`scripts/compute_release.py` here, not the new engine. The planned safeguard
for legacy workflows removes tag-push triggers from `release-image.yml` and `publish-pypi.yml` and
adds duplicate-identity refusal to their dispatches, so bot-created stable
tags cannot rebuild published artifacts or move `latest` inadvertently.
In this transitional section only, `M` denotes the fetched main SHA after merge.

### When transitional main has advanced past the reviewed revision

The rule above assumes the release merge is still the tip of `main`. It is not
always: another pull request can merge between review and publication. Unless
that pull request itself carried a qualifying release review and authorized merge,
no revision then satisfies the rule as written, because the reviewed revision
is no longer `main` and `main`'s tree has not been reviewed as a unit. That is
a real gap, not a technicality to argue past, and it must be closed
deliberately rather than by publishing anyway.

The preferred resolution is to make the reviewed revision the tip again, by
preparing the next reviewed change on top and publishing from that. Coordinate
a short pause on other merges around final validation and dispatch, because
each new merge restarts this. If merges keep landing, the release is delayed;
that is the correct outcome. Never weaken an identity check to guarantee
progress.

Publication from a revision that is not `main` is a fallback. It relaxes only
the requirement to publish from the tip: the squash-parent and reviewed-tree
proofs, and every other requirement in this contract, still apply in full. It
is permitted only when every one of the following holds and is recorded:

* The chosen source `S` is the exact revision whose tree was reviewed and
  approved as a unit, and its digest is stated in the release record.
* `S` is an ancestor of `main`.
* Every commit between `S` and `main` was independently reviewed and merged by
  an authorized merger through the normal pull-request path. An unreviewed commit in that
  range blocks publication outright.
* Those commits change no input to a published artifact, directly or
  transitively. Absence from the wheel and sdist manifests and from the image
  build is necessary but never sufficient on its own. Inputs include, and are
  not limited to: packaged files, and note that `CHANGELOG.md` is in the sdist
  manifest; build helpers and anything they read; generated metadata and
  provenance; release notes, which the publication workflows consume; and the
  publication and release workflows themselves, which control how artifacts
  are produced and therefore always block this fallback. A change that alters
  release authorization or validation blocks it too, even when no artifact
  byte would differ.
* The release record names `S` explicitly and notes that `main` is ahead of it,
  listing the intervening commits. Provenance and the OCI
  `org.opencontainers.image.revision` label name `S`, so the published image
  identity is `S` and not `main`. Never describe the release as built from
  `main` when it was not.
* Gates and security clearance are rerun on `S`, not inherited from `main` or
  from the pre-merge candidate.

If any condition fails, prepare a fresh reviewed pull request and obtain a
fresh authorized merge instead. Never resolve this by relabelling a failed
identity check as passing.

Delivery is a direct continuation after authorized merge. Workflows use explicit
`workflow_dispatch`, not push-triggered tagging:

1. Dispatch `rc-publish.yml` with the exact source SHA, which is `M`
   normally and `S` under the fallback above, and a positive candidate
   number. Publish wheel, sdist, OCI image, `release-provenance.json`, PyPI
   version, and GitHub prerelease.
2. Verify hashes, provenance, and digest. Record a rollback digest, deploy the
   candidate to the validation environment, and verify rollout, health,
   readiness, MCP search, and enforcement.
3. Dispatch `tag-release.yml` with `candidate_tag` naming the highest complete
   candidate. Respect the protected `pypi` environment. Environment approval
   must be limited to the exact authorized workflow run and source revision.
4. Verify stable PyPI artifacts, GitHub release/tag, and GHCR digest. Stable OCI
   promotion reuses the candidate digest without rebuilding.
5. Verify deployment at that digest and provide a benefit-led announcement draft
   with a verified release link for human publication.

Dispatch or upload alone is not completion. Record source, reviews, authorized merge,
artifact identities, deployment, and verification outcomes.

## Immutability and recovery

Published versions, tags, and assets are immutable. Same-content retries verify
existing bytes and upload missing assets; different bytes block. GitHub uploads
use `scripts/release_upload.py`. Never replace assets or move version tags.
Prepared but unpublished versions require a fresh reviewed PR and authorized merge.
Validate documents against the computed change set and inventory PyPI, GHCR,
Git tags, and GitHub releases. Collisions or ambiguous partial publication need
a reviewed recovery decision. A previous merge under another process does not
authorize publication. Record failures and follow `.rules/release-rollback.md`.
Never report blocked or failed delivery as complete.
