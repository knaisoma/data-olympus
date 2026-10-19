# Data Olympus release rollback

Status: active
Since: 2026-09-05

The STD-U-821 model uses this same recovery procedure: re-deploy the recorded
prior digest, never retag a version. Branch recutting does not change the
rollback identity. There is no channel-moving workflow: after a rollback,
`stable` and `latest` are corrected by releasing forward, because the next
patch promotion moves both.

Before deployment record previous digest, source provenance, container names,
workload generation, rollout, health, and readiness. All init and main containers
must use the same immutable digest. Record update policy and prevent races.

1. Pause automatic workload updates.
2. Restore the recorded digest to all init and main containers.
3. Wait for rollout and verify observed digests.
4. Verify health, readiness, MCP access, search, and enforcement.
5. Restore intended update policy only after verification.
6. Record failed and restored digests, verification, and outcome.

Channel movement is not deployment verification, and registry tags never
change what is deployed.
Published files and version tags are never replaced. Leave unsuitable GitHub
prereleases immutable; yank unsafe PyPI candidates and use a higher candidate
number. For stable failures restore service, yank unsafe versions when required,
record the incident, and prepare a new patch release.
Ambiguous apply, missing containers, incomplete rollout, digest mismatch, or
failed health checks mean failed recovery. Stop publication, escalate the exact
failure, and resume only after reviewed recovery assessment.
