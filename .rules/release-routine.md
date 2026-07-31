# Data Olympus release routine

Status: active
Since: 2026-07-31
Schedule: Monday at 05:00 Europe/Bucharest
Automation status: disabled until baseline certification

## Outcome

Publish one immutable release from already merged, reviewed, green,
unreleased work, then prove that every artifact and the kn dev service match
one exact reviewed source revision.

The routine is outcome based. It is not a weekly cutter and has no obligation
to release when the evidence says no release is due.

## Command interface

The project command is:

`python3.13 scripts/operations/release.py <stage>`

It reads one JSON object from `AI_OPERATIONS_STAGE_INPUT` and emits one JSON
object with these fields:

* `status`
* `reason`
* `evidence`
* `outputs`

Accepted statuses are:

* `pass`
* `no_action`
* `blocked`
* `failed`

`no_action` is valid only during admission. The command supports the fixed
lifecycle stages plus rollback:

* `authority`
* `admission`
* `prepare`
* `validate`
* `review`
* `deliver`
* `verify`
* `notify`
* `rollback`

## Monday sequence

1. Load the single private release issue and the exact Friday source SHA.
2. Fetch `origin/main` and rerun `scripts/compute_release.py`.
3. Stop as `Blocked` when the source SHA, computed version, changelog, security
   state, test evidence, or rollback point changed.
4. If the repository now proves that no unreleased work exists, close the
   candidate as `No action`. Never manufacture a release to satisfy the
   schedule.
5. Prepare the version change and release notes on one short lived
   `chore/release-vX.Y.Z` branch from the approved source SHA.
6. Set `pyproject.toml` to `X.Y.Z`, close the matching changelog block, open a
   new empty `[Unreleased]` block, and write `docs/releases/vX.Y.Z.md`.
7. Obtain independent crossed review of the exact branch SHA, merge the one
   logical release change through the repository rules, and bind the candidate
   source to the resulting exact `main` SHA.
8. Rerun all release gates against that SHA:

   * Complete test suite.
   * Ruff.
   * mypy.
   * Bats.
   * Benchmark documentation checks.
   * Installed wheel and sdist smoke tests.
   * `scripts/security_alerts.py`.
   * `scripts/ci_status.py`.
   * Version availability across PyPI, GHCR, GitHub tags, and GitHub releases.

9. Require one Claude and Codex crossed pair. One family executes and the other
   independently reviews. Self review is prohibited.
10. Require operator approval bound to the exact candidate source SHA.
11. Dispatch `rc-publish.yml` for that SHA and the next unused candidate
    number. Do not reproduce version computation or publication logic in the
    runner.
12. Verify the complete candidate transaction:

    * GitHub prerelease.
    * Candidate wheel.
    * Candidate sdist.
    * PyPI prerelease.
    * `release-provenance.json`.
    * GHCR image digest.

13. Keep Keel at `never`. Deploy the candidate exact digest to both StatefulSet
    containers through the kn dev FastMCP gateway, wait for rollout, and verify
    health, readiness, MCP search, enforcement, and documentation.
14. If canary verification passes, dispatch `tag-release.yml` with the exact
    candidate tag. The workflow promotes the existing candidate and must not
    rebuild the OCI image.
15. Dispatch `set-channel.yml` with `source=vX.Y.Z` for registry channel
    traceability. The moving channel does not drive the kn dev deployment while
    Keel is `never`.
16. Deploy the same stable digest explicitly to kn dev and run the full
    external verification stage.
17. Send one Telegram completion message to the semantic destination
    `data-olympus-operations`. Completion requires exact independent readback
    of the destination, message identifier, run marker, and content.
18. Update the private release issue with immutable evidence and the truthful
    terminal state.

## Exact source rule

Candidate preparation, review, operator approval, workflow receipts,
provenance, tags, artifacts, deployment, verification, and notification all
name the same source SHA.

Any source change invalidates approval and restarts the release assessment.

## Existing release mechanisms

The routine reuses:

* `scripts/compute_release.py` for version computation.
* `scripts/security_alerts.py` for security clearance.
* `scripts/ci_status.py` for exact SHA CI status.
* `scripts/release_readiness.py` for evidence evaluation where its schema
  applies.
* `scripts/release_artifacts.py` for artifact identity.
* `rc-publish.yml` for candidate publication.
* `tag-release.yml` for stable promotion.
* `set-channel.yml` for moving registry channels.

The runner does not contain a second implementation of these rules.

## Failure

Any missing, stale, mismatched, unparseable, or unreachable gate fails closed.

If external state changed before failure, run `.rules/release-rollback.md`,
verify the restored exact digest, and record `Failed`. A blocked or failed run
is never relabeled as complete because a workflow started.

## Completion

The release is complete only when:

* GitHub release `vX.Y.Z` points at the reviewed SHA.
* PyPI `X.Y.Z` provenance points at the reviewed SHA.
* GHCR `vX.Y.Z` points at the reviewed candidate digest.
* kn dev runs that exact digest in both containers.
* Health, readiness, MCP search, enforcement, and documentation pass.
* Telegram send and exact readback are confirmed.
* The runner ledger and private release issue record the same terminal state.
