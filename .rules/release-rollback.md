# Data Olympus release rollback

Status: active
Since: 2026-09-05

Before deployment record previous digest, source provenance, container names,
workload generation, rollout, health, and readiness. All init and main containers
must use the same immutable digest. Record update policy and prevent races.

1. Pause automatic workload updates.
2. Restore the recorded digest to all init and main containers.
3. Wait for rollout and verify observed digests.
4. Verify health, readiness, MCP access, search, and enforcement.
5. Restore intended update policy only after verification.
6. Record failed and restored digests, verification, and outcome.

Channel movement is not deployment verification. Restore changed channels after
service recovery. `set-channel.yml` changes registry tags only.
Published files and version tags are never replaced. Leave unsuitable GitHub
prereleases immutable; yank unsafe PyPI candidates and use a higher candidate
number. For stable failures restore service, yank unsafe versions when required,
record the incident, and prepare a new patch release.
Ambiguous apply, missing containers, incomplete rollout, digest mismatch, or
failed health checks mean failed recovery. Stop publication, escalate the exact
failure, and resume only after reviewed recovery assessment.
