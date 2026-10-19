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

The workflows below are in the repository. They run only while the repository
variable `SDLC_PIPELINE` is `enabled`, which it has been since the 0.11.1 cycle
(first new-model release, 2026-10-09). Steps that act as the bot additionally
require the bot or GitHub App to be provisioned with protected-branch
deletion/recut permissions; they also rely on the `release/new` ruleset
(PR required, code-owner review for `scripts/`, `.github/`, `release/` and
`docs/releases/std-u-821-amendment-1.3.md`), and the
`pypi-rc` environment with its own PyPI Trusted Publisher. Missing prerequisites
mean BLOCKED; never substitute the operator's personal token. Product static
token machine identity is separate work and is not granted here.
The adoption cut and the public-product preview exception were ratified on
2026-10-07 as STD-U-821 amendment 1.3. An adoption issue is still to be
opened; it records the anchor, base and reason and grants nothing beyond the
amendment. CI runs tests and
wheel/sdist smoke checks per PR. There is no hosted preview and no per-PR image.
The RC image and wheel come from `rc-build.yml`.

1. `rc-build.yml` builds wheel, sdist, OCI archive and
   `release-provenance.json` at the exact source SHA `H`
   on `release/new` or `hotfix/new`, with `contents: read`, no secrets and no
   OIDC token. Upload workflow artifacts; `N=0` stops without publication.
2. `rc-publish-stage.yml` runs from trusted `main` via `workflow_run`, after a
   successful allowed-branch build. Treat `H` and its history as data; never
   run its code with publish credentials. Recompute the version and verify
   provenance, wheel/sdist hashes and OCI archive digest before publishing
   through `pypi-rc`, GHCR and a GitHub prerelease. Move `rc` only for normal
   release heads and only while `SDLC_RC_CHANNEL` is `enabled`; it is unset,
   so no workflow moves `rc` today. Select staging by current `H` and digest, prioritizing
   `hotfix/new` while it exists, never by version order.
3. Under the exclusive `data-olympus-promotion` lock, recheck head, base,
   staging and STD-U-822 gates. Squash to main with `release: X.Y.Z` and
   generated notes. Prove sole parent `B`, tree equality with reviewed `H`,
   and absence of `release/ADOPTION.json` at `H`.
4. `promote-release.yml`, an explicit `workflow_dispatch` from `main` with
   `squash`, `reviewed_head` and `candidate_tag`, creates annotated `vX.Y.Z`
   on that squash, builds
   stable Python metadata from it and checks payload equivalence to the RC.
   Keep the protected `pypi` environment and MCP registry publication gate.
   Reuse the tested OCI digest, retaining its RC label. Provenance maps `H`,
   squash SHA, stable tag and digest. Move `stable`, `vX.Y.Z` and `latest`
   only after verification.
5. Deploy stable-tag-authorized artifacts through reviewed digest-pinned
   gitops changes; the publication workflow itself does not deploy. Record
   the prior digest and verify delivery before recutting `release/new`.
   Move open PRs and preserve pending work with fresh checks and review.

Delivery is a direct continuation after authorized merge, through explicit
`workflow_dispatch`, never push-triggered tagging. Dispatch or upload alone is
not completion. Record source, reviews, authorized merge, artifact identities,
deployment, and verification outcomes.

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
vulnerability remediation and CodeQL fixes are mandatory, with zero findings.
The reviewed head `H` (the release pull request head) needs successful `test`
and the aggregate `CodeQL` check result, which GitHub produces only on pull
request heads. The release squash `S` on `main` needs successful `test` and every
per-language `Analyze (...)` check named in `SDLC_REQUIRED_CODEQL_CHECKS`;
those are push-time checks, and the aggregate `CodeQL` is not produced on
pushes to `main`. The proof that the trees of `S` and `H` are equal ties both
results to the same code (see `docs/releases/reviewed-promotion.md`).
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

## Retired old path

The old release path was retired in 2026-10, after the new model shipped
0.11.1 and 0.11.2. Its workflows (`rc-publish.yml`, `tag-release.yml`,
`set-channel.yml`, `release-image.yml`, `publish-pypi.yml` and their reusable
companions) and its scripts (`rc_tag.py`, `release_upload.py`,
`workflow_validation.py`) were removed. Releases up to 0.11.0 were published
by that path; their records stay as history. Hotfixes use `hotfix-cut.yml`,
which cuts `hotfix/new` from the current stable tag, and then follow the
new-model delivery above. A rollback re-deploys the recorded prior digest (see
`.rules/release-rollback.md`).

## Immutability and recovery

Published versions, tags, and assets are immutable. Same-content retries verify
existing bytes and upload missing assets; different bytes block. GitHub uploads
use `scripts/rc_verify_and_publish.py` for candidates and
`scripts/release_record.py` for stable releases. Never replace assets or move
version tags.
Prepared but unpublished versions require a fresh reviewed PR and authorized merge.
Validate documents against the computed change set and inventory PyPI, GHCR,
Git tags, and GitHub releases. Collisions or ambiguous partial publication need
a reviewed recovery decision. A previous merge under another process does not
authorize publication. Record failures and follow `.rules/release-rollback.md`.
Never report blocked or failed delivery as complete.
