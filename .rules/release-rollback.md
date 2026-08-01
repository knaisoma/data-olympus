# Data Olympus release rollback

Status: active
Since: 2026-07-31
Applies to: Data Olympus release canary and stable deployment on kn dev

## Deployment model

The kn dev StatefulSet runs immutable GHCR digests. Keel policy is `never`.

The `kndev` moving registry tag may be updated for traceability, but it does not
deploy the workload. Canary, stable deployment, and rollback apply an exact
digest to both StatefulSet containers through the kn dev FastMCP gateway.

This rule replaces the former assumption that Keel `force` plus `matchTag`
would follow the moving `kndev` tag.

## Required rollback point

Before candidate deployment, record:

* Exact image reference.
* Exact image digest.
* Source revision from provenance.
* Main container name.
* Init container name.
* Current StatefulSet generation.
* Intended Keel policy.
* Health and readiness result.

The rollback point is invalid if either container is missing or the recorded
digest cannot be resolved.

## Rollback sequence

Execute these steps in order:

1. Set or confirm `keel.sh/policy: never`.
2. Apply the previous exact digest to:

   * `data-olympus-mcp`
   * `prepare-git`

3. Wait for the StatefulSet rollout to finish.
4. Verify:

   * Both containers use the rollback digest.
   * Pod health passes.
   * Readiness passes.
   * The public MCP endpoint responds.
   * Search works.
   * Enforcement works.

5. Restore the intended Keel policy. The approved intended policy is `never`,
   so this step confirms the policy rather than enabling automatic upgrades.
6. Record the failed digest, restored digest, rollout evidence, verification
   evidence, gateway calls, and terminal state.

The project command accepts rollback only when the evidence names these events
in the same order:

* `keel-paused`
* `digest-restored`
* `deployment-verified`
* `keel-policy-restored`

## Registry channels

If a failed candidate or stable release changed `kndev`, restore that channel
to the prior known good tag through `set-channel.yml` after service recovery.
Channel repair does not replace direct digest rollback and does not count as
deployment verification.

## Published artifacts

Published files and immutable version tags are never replaced.

When a candidate is unsuitable:

* Leave the GitHub prerelease immutable.
* Yank the PyPI candidate when continued installation would be unsafe.
* Correct the defect under a new source SHA.
* Publish a higher candidate number.

When a stable release is unsuitable:

* Restore kn dev first.
* Yank the PyPI stable version when required.
* Mark the GitHub release as a prerelease when required.
* Record the immutable failure and recovery evidence in the runner ledger.
* Prepare a new patch version. Do not move the existing stable tag.

## Failure

A rollback is failed when:

* Keel was not paused first.
* Only one container was changed.
* A tag was applied without an exact digest.
* Rollout did not complete.
* The observed digest differs.
* Health or readiness failed.
* Policy was restored before deployment verification.
* Required evidence is missing.

In every such case, keep the release schedule disabled and notify the operator
with the exact failed step.
