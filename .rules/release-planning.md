# Data Olympus release planning

Status: active
Since: 2026-07-31
Schedule: Friday at 18:00 Europe/Bucharest
Automation status: disabled until baseline certification

## Purpose

The planning run identifies one release candidate from work that is already
merged, reviewed, green, and unreleased on `origin/main`.

It does not select future issues, create a weekly feature batch, or promise that
unfinished work will ship on Monday. Product planning and release planning are
separate outcomes.

## Authority

The run reads these sources before acting:

* The active Data Olympus records named by the AI Operations contract.
* `AGENTS.md`.
* `.rules/versioning.md`.
* `.rules/release-routine.md`.
* `.rules/release-rollback.md`.
* The exact `origin/main` revision under consideration.

The authority revision, contract digest, and source revision are recorded
before admission.

## Friday planning sequence

1. Fetch `origin/main` and work from a clean dedicated worktree.
2. Bind the candidate source to the exact remote `main` SHA.
3. Run `uv run python scripts/compute_release.py`.
4. If `releasable` is false, record `No action` with the computation and stop.
5. If a release is due, require a version greater than the current stable
   version. `v0.6.0` is immutable and can never be selected again.
6. Run `python3 scripts/security_alerts.py`. Any open or unreadable alert blocks
   planning. Alert dismissal remains a separate operator approved security
   action and is never an automatic release planning side effect.
7. Record the changelog hash and the test evidence bound to the exact source
   SHA.
8. Read the current kn dev workload image and record the exact rollback digest.
   The intended Keel policy is `never`.
9. Create or update exactly one open private GitHub issue for the candidate.
   Discover the GitHub FastMCP surface first. If it cannot create or edit
   issues, use the authenticated `gh` CLI and record the missing gateway
   capability as the fallback reason.
10. Leave the issue awaiting the Monday gates and exact SHA approval. Do not
    publish a release candidate, move a channel, or change kn dev during
    planning.

## Required release issue fields

The private release issue contains:

* Candidate version.
* Exact source SHA.
* Authority revision.
* AI Operations contract digest.
* Changelog summary and content hash.
* Security state and evidence hash.
* Test commands, result, and evidence hash.
* Current kn dev image and exact rollback digest.
* Intended Keel policy.
* Companion review state.
* Operator approval state and approved source SHA.
* Delivery, verification, notification, and rollback evidence as it becomes
  available.

The issue is a write ahead record. It is not completion evidence by itself.

## Manual context

Every manual run receives `ExtraContext` with the exact default:

`No extra context for this run`

The run may use meaningful extra context to adjust the candidate assessment,
but it records only whether the default was used, normalized length, and a
SHA256 hash. Raw extra context is not stored in the runner ledger.

## Completion

Planning is complete only when:

* `No action` is proven from the exact remote `main` revision, or
* One private release issue records the complete candidate evidence.

An empty issue, a future scope, a list of open feature requests, or an alert
query failure is not successful planning.
