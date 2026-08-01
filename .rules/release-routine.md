# Data Olympus release routine

Status: active
Since: 2026-07-31
Schedule: Monday at 05:00 Europe/Bucharest
Automation status: disabled until baseline certification

## Outcome

Publish one immutable release from already merged, green, unreleased work.
Review the deterministic release candidate before it can merge, prove that the
squash merge transferred the exact reviewed tree, then prove that every
artifact and the kn dev service name the resulting delivery revision.

The routine is outcome based. It is not a weekly cutter and has no obligation
to release when the evidence says no release is due.

## Command interface

The complete project command is:

`python3.13 -m scripts.operations.release`

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
evidence. Supported external mutations and reads go through FastMCP. Current
gateway capability gaps are CodeQL analysis and alert enumeration, Code
Quality setup, ruleset details, GraphQL review threads, container package
digest lookup, and a pull request merge with an expected head SHA precondition.
They reuse governed repository commands and record the direct GitHub fallback
because the gateway exposes none of those exact operations. These gaps remain
tracked platform work and do not weaken the release gates.
An unconditional gateway merge is not an acceptable substitute for the
expected head precondition.
After the conditional merge mutation begins, an unknown merge outcome or any
confirmed postmerge gate failure is `failed`, never `blocked`. Its result
records the pull request, reviewed head, merge confirmation state, resulting
revision when known, and incomplete recovery state.
Workflow preflight reads remain safely retryable and `blocked`. The delivery
mutation boundary begins immediately before the first workflow dispatch;
dispatch ambiguity and every later failure are `failed`.
The deployment mutation boundary begins immediately before the Kubernetes
apply. A rollback preflight read may block without recovery evidence. Once the
apply begins, an ambiguous apply or failed postapply acceptance returns a
failed recovery result that records the rollback digest, apply confirmation,
rollout state, acceptance state, and incomplete recovery.

The central runner owns authority admission, immutable run control, model
tickets, exact candidate approval, the durable ledger, Telegram delivery, and
exact Telegram readback.

Rollback is the same executable with the `rollback` argument. It reads
`AI_OPERATIONS_RECOVERY_INPUT` and returns one closed recovery result.

## Runtime prerequisites

The release host requires:

* Python 3.13 and `uv` for project gates and artifact builds.
* GitHub CLI authentication for supported GitHub reads and mutations.
* Docker CLI with the Buildx plugin for exact public GHCR tag inspection and
  release digest verification. Registry inspection is bounded and
  noninteractive.
* Bats for the shell test suite.
* Explicit access to the kn dev kubeconfig and the FastMCP gateway.

## Monday sequence

1. Bind the authority revision, contract digest, run identifier, and exact
   `origin/main` source SHA.
2. Rerun `scripts/compute_release.py` from that exact clean source.
3. If the repository proves that no unreleased work exists, close the
   candidate as `No action`. Never manufacture a release to satisfy the
   schedule.
4. Before branch creation or file mutation, record a fresh explicit Data
   Olympus consultation under the runner identifier and workspace
   `data-olympus`. Require `kb_gate_check` to return `allow` for that exact
   session and workspace. Then prepare the version change and release notes on
   one short lived `chore/release-vX.Y.Z-RUN` branch from the admitted source
   SHA. The unchanged repository precommit hook remains the final staged
   authority gate.
5. Set `pyproject.toml` to `X.Y.Z`, update `uv.lock`, close the matching
   changelog block, open a
   new empty `[Unreleased]` block, and write `docs/releases/vX.Y.Z.md`.
6. Open one public pull request through FastMCP. Keep it open and unmerged.
   Require every observed repository check to reach a successful terminal
   conclusion. Require the aggregate `CodeQL` check, all three CodeQL language
   analyses with zero results, no open CodeQL alert for the pull request, no
   unresolved review thread, and the complete active default branch ruleset
   fingerprint. GitHub Code Quality is not a dependency because it is not
   available on the approved GitHub Free plan. Missing or unverifiable
   evidence blocks before review or merge.
7. Bind the review candidate to the exact pull request head SHA `H`, its Git
   tree `T`, and admitted main SHA `B`. Require `H` to have sole parent `B`.
   Rerun all local release gates against `H`:

   * Complete test suite.
   * Ruff.
   * mypy.
   * Bats.
   * Benchmark documentation checks.
   * Installed wheel and sdist smoke tests.
   * `scripts/security_alerts.py`.
   * `scripts/ci_status.py`.
   * Version availability across PyPI, GHCR, GitHub tags, and GitHub releases.

   Ruff, mypy, and pytest run through `uv` with the declared `dev` extra so the
   release can never select an unrelated global executable.

   GHCR availability uses an exact public registry tag inspection for
   `ghcr.io/knaisoma/data-olympus:vX.Y.Z`. Only an explicit missing manifest
   proves the tag is free. Client, permission, registry, and unrecognized
   failures block the release. This check does not depend on GitHub Packages
   API scopes or a truncated package version listing.

   Each isolated wheel or sdist smoke gets one fresh retry because environment
   creation and dependency retrieval can fail transiently. A second failure
   blocks the release. The other local gates remain single attempt so a real
   validation failure is never hidden.

   The first postmerge GitHub tag inventory is an idempotent FastMCP read. It
   gets one retry only when text content omits the required gateway tool binding.
   A mismatched tool, malformed result, different gateway failure, or second
   omission still fails closed. A final failure preserves the already proven
   merge parent and tree evidence and is never misclassified as a premerge
   block. No mutation call is retried by this rule.

   Pull request checks require the aggregate `CodeQL` signal and all three
   language analyses. The review packet includes `H`, `T`, `B`, exact checks,
   security evidence, release gates, rollback point, ruleset fingerprint, and
   immutable delivery procedure.
8. The deterministic executor requests one central, run controlled Claude
   review of `H`. Self review is prohibited. Review evidence hashes both the
   exact submitted packet and the ticket bound Claude response. The packet
   states the compact runner response contract and includes only controls that
   require model judgment. The native `REVIEW_REQUIRED` value is validated
   deterministically before review and is omitted from the model packet because
   it is the expected prereview state, not a release finding.
9. Require the candidate approval ledger entry to bind the run, contract
   digest, `H`, and consumed Claude review. The approved standing delegation
   may materialize that exact entry only after the review passes.
10. In the same execution, rederive `H`, `T`, `B`, remote `main`, every check
    and CodeQL result, every review thread, and the complete ruleset
    fingerprint. Any change requires a new run and review. The deliberate team
    bypass bypasses the entire GitHub ruleset, not only native approval, so
    these routine owned controls are load bearing.
11. Submit a conditional squash merge naming expected head `H`. Reconcile an
    ambiguous response only from the same pull request and current remote
    `main`. An open unchanged pull request stops safely. Every other ambiguous
    state fails closed.
12. Fetch resulting remote main SHA `M`. Require `M` to have sole parent `B`
    and exact tree `T`. Rerun final CI and every release gate against `M`.
    Publication is a direct continuation after these freshly derived proofs,
    never a replay from stored evidence.
13. Dispatch `rc-publish.yml` for `M` and the next unused candidate
    number. Do not reproduce version computation or publication logic in the
    runner.
14. Verify the complete candidate transaction:

    * GitHub prerelease.
    * Candidate wheel.
    * Candidate sdist.
    * PyPI prerelease.
    * `release-provenance.json`.
    * GHCR image digest.

15. Keep Keel at `never`. Deploy the candidate exact digest to both StatefulSet
    containers through the kn dev FastMCP gateway, wait for rollout, and verify
    health, readiness, MCP search, enforcement, and documentation.
16. If canary verification passes, dispatch `tag-release.yml` with the exact
    candidate tag. The workflow promotes the existing candidate and must not
    rebuild the OCI image.
17. Dispatch `set-channel.yml` with `source=vX.Y.Z` for registry channel
    traceability. The moving channel does not drive the kn dev deployment while
    Keel is `never`.
18. Deploy the same stable digest explicitly to kn dev and run the full
    external verification stage.
19. Return a delivered project result whose immutable candidate revision is
   `H`. Evidence separately records `B`, `T`, `M`, exact tree equality, the
   previous rollback digest, published version, image digest, and verification
   receipts.
20. The central runner sends one Telegram completion message to the semantic
   destination `data-olympus-operations`. Completion requires exact independent
   readback of the destination, message identifier, run marker, and content.
21. The durable runner ledger records the truthful terminal state. No private
   issue is an authority or completion dependency.

## Exact source rule

The release branch head `H` is the immutable reviewed candidate. The approval
stays bound to `H`. The squash merge produces delivery revision `M`, which is
authorized only when its sole parent is admitted SHA `B` and its tree exactly
equals reviewed tree `T`. Workflow receipts, provenance, tags, artifacts,
deployment, verification, and notification all name `M`. Project and central
candidate fields remain `H`; delivery evidence records both revisions and the
content transfer proof.

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

* GitHub release `vX.Y.Z` points at the proven delivery SHA.
* PyPI `X.Y.Z` provenance points at the proven delivery SHA.
* GHCR `vX.Y.Z` points at the reviewed candidate digest.
* kn dev runs that exact digest in both containers.
* Health, readiness, MCP search, enforcement, and documentation pass.
* Telegram send and exact readback are confirmed.
* The runner ledger records the same terminal project, review, approval,
  deployment, and notification evidence.
