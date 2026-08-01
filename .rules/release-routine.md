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

The complete project command is:

`python3.13 scripts/operations/release.py`

It reads the bounded run document from `AI_OPERATIONS_RUN_INPUT`. It emits
newline delimited JSON with contiguous sequence numbers. The only event types
are:

* `heartbeat`
* `milestone`
* `result`

The terminal result status is one of:

* `delivered`
* `no_action`
* `blocked`
* `failed`

The fixed milestones are:

* `admission`
* `prepare`
* `validate`
* `domain_review`
* `deliver`
* `verify`

The project command owns Git, release artifact, workflow, registry, and kn dev
evidence. Supported external mutations and reads go through FastMCP. The three
current GitHub capability gaps are security alert enumeration, container
package digest lookup, and a pull request merge with an expected head SHA
precondition. They reuse governed repository commands and record the direct
GitHub fallback because the gateway exposes none of those exact operations.
An unconditional gateway merge is not an acceptable substitute for the
expected head precondition.

The central runner owns authority admission, immutable run control, model
tickets, exact candidate approval, the durable ledger, Telegram delivery, and
exact Telegram readback.

Rollback is the same executable with the `rollback` argument. It reads
`AI_OPERATIONS_RECOVERY_INPUT` and returns one closed recovery result.

## Monday sequence

1. Bind the authority revision, contract digest, run identifier, and exact
   `origin/main` source SHA.
2. Rerun `scripts/compute_release.py` from that exact clean source.
3. If the repository proves that no unreleased work exists, close the
   candidate as `No action`. Never manufacture a release to satisfy the
   schedule.
4. Prepare the version change and release notes on one short lived
   `chore/release-vX.Y.Z-RUN` branch from the admitted source SHA.
5. Set `pyproject.toml` to `X.Y.Z`, update `uv.lock`, close the matching
   changelog block, open a
   new empty `[Unreleased]` block, and write `docs/releases/vX.Y.Z.md`.
6. Open one public pull request through FastMCP. Require every repository check
   to complete successfully. Confirm that remote `main` still equals the
   admitted SHA, then squash merge the one deterministic release change.
7. Bind the candidate to the resulting exact `main` SHA and require its parent
   to equal the admitted SHA. A concurrent main change blocks the run.
8. Rerun all release gates against that final SHA:

   * Complete test suite.
   * Ruff.
   * mypy.
   * Bats.
   * Benchmark documentation checks.
   * Installed wheel and sdist smoke tests.
   * `scripts/security_alerts.py`.
   * `scripts/ci_status.py`.
   * Version availability across PyPI, GHCR, GitHub tags, and GitHub releases.

9. The deterministic executor requests one central, run controlled Claude
   review of the exact final SHA. Self review is prohibited.
10. Require the candidate approval ledger entry to bind the run, contract
    digest, final SHA, and consumed Claude review. The approved standing
    delegation may materialize that exact entry only after the review passes.
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
17. Return a delivered project result with the release pull request, previous
   rollback digest, published version, exact candidate SHA, image digest, and
   verification receipts.
18. The central runner sends one Telegram completion message to the semantic
   destination `data-olympus-operations`. Completion requires exact independent
   readback of the destination, message identifier, run marker, and content.
19. The durable runner ledger records the truthful terminal state. No private
   issue is an authority or completion dependency.

## Exact source rule

The release branch SHA is premerge evidence only because squash merge produces
a new commit. Final review, exact approval, workflow receipts, provenance,
tags, artifacts, deployment, verification, and notification all name the same
resulting `main` source SHA.

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
* The runner ledger records the same terminal project, review, approval,
  deployment, and notification evidence.
