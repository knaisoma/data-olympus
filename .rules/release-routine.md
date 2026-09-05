# Data Olympus release contract

Status: active
Since: 2026-09-05

## Approval

Target Monday availability. Development, review, and delivery may be automated;
only a human merges the final release PR. Automation must never merge or bypass
protections. External announcements are human-published.

Use a short-lived release branch, for example `codex/release-YYYY-MM-DD`.
Prepare the completed batch, version, lockfile, changelog, and release notes there.
The branch name does not require a version before the selected work is complete.
Keep it open and unmerged until the implementer and
independent reviewer agree all blockers are resolved. Record review of `H`, the
exact PR head, its tree `T`, and base `B`. Any content change invalidates approval.
Recheck expected head `H` and unchanged base before human merge.

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
handed to its human merger while an alert is still open, and only when all of
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
* The pull request states plainly that closure is pending its human merge.

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

## Human merge and delivery

Human merge authorizes delivery of reviewed content only. Fetch main SHA `M`;
prove its tree exactly equals reviewed tree `T`. Require a squash merge with
sole parent `B`, preserving linear history. Other merge methods are not supported.
Changed base or content requires new review. Candidate fields remain `H`;
provenance names `M`. Rerun checks and security clearance on `M`.

Delivery is a direct continuation after human merge. Workflows use explicit
`workflow_dispatch`, not push-triggered tagging:

1. Dispatch `rc-publish.yml` with exact source SHA `M` and a positive candidate
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

Dispatch or upload alone is not completion. Record source, reviews, human merge,
artifact identities, deployment, and verification outcomes.

## Immutability and recovery

Published versions, tags, and assets are immutable. Same-content retries verify
existing bytes and upload missing assets; different bytes block. GitHub uploads
use `scripts/release_upload.py`. Never replace assets or move version tags.
Prepared but unpublished versions require a fresh reviewed PR and human merge.
Validate documents against the computed change set and inventory PyPI, GHCR,
Git tags, and GitHub releases. Collisions or ambiguous partial publication need
a reviewed recovery decision. A previous merge under another process does not
authorize publication. Record failures and follow `.rules/release-rollback.md`.
Never report blocked or failed delivery as complete.
