from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

import pytest

from scripts.operations.release_runtime import (
    FastMCPGateway,
    ReleaseDeliveryError,
    ReleaseRuntime,
    candidate_release_evidence,
    completed_check_evidence,
    default_release_dependencies,
    deployment_manifest_for_digest,
    deployment_state,
    render_release_documents,
    stable_release_evidence,
)

if TYPE_CHECKING:
    from pathlib import Path

SOURCE_SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


class StubGateway:
    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, arguments))
        response = self.responses[name]
        if type(response) is tuple:
            next_response, *remaining = response
            self.responses[name] = tuple(remaining)
            return next_response
        return response


def test_fastmcp_gateway_uses_the_gateway_control_plane() -> None:
    calls: list[list[str]] = []

    def run_command(command: list[str], _cwd: Path, _timeout: int) -> str:
        calls.append(command)
        return json.dumps(
            {
                "is_error": False,
                "structured_content": {
                    "result": json.dumps(
                        {"tool": "get_latest_release", "result": "{\"tag\":\"v1\"}"}
                    )
                },
            }
        )

    gateway = FastMCPGateway(run_command=run_command)

    result = gateway.execute(
        "get_latest_release",
        {"owner": "knaisoma", "repo": "data-olympus"},
    )

    assert result == {"tag": "v1"}
    assert calls[0][:5] == [
        "fastmcp",
        "call",
        "--server-spec",
        "http://fastmcp-gateway.mcp-gateway.apps.172.30.1.2.nip.io/mcp",
        "--target",
    ]
    assert calls[0][5] == "execute_tool"


def test_collect_admission_binds_exact_remote_main_and_governed_computation(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        commands.append(tuple(command))
        if command[:2] == ["git", "fetch"]:
            return ""
        if command == ["git", "rev-parse", "HEAD"]:
            return SOURCE_SHA
        if command == ["git", "rev-parse", "origin/main"]:
            return SOURCE_SHA
        if command[0] == "uv":
            return json.dumps(
                {
                    "releasable": True,
                    "bump": "minor",
                    "current_version": "0.6.0",
                    "next_version": "0.7.0",
                    "functional_changed": False,
                    "changes": {
                        "features": [
                            "feat(automation): make weekly releases outcome based"
                        ],
                        "fixes": [],
                        "breaking": [],
                    },
                }
            )
        raise AssertionError(command)

    runtime = ReleaseRuntime(
        repository_root=tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
    )

    evidence = runtime.collect_admission({"source_revision": SOURCE_SHA})

    assert evidence["branch"] == "main"
    assert evidence["source_revision"] == SOURCE_SHA
    assert evidence["remote_main_revision"] == SOURCE_SHA
    assert evidence["computed_release"]["next_version"] == "0.7.0"
    assert commands[0] == ("git", "fetch", "--tags", "origin", "main")


def test_collect_admission_fails_when_the_managed_worktree_is_stale(
    tmp_path: Path,
) -> None:
    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        if command[:2] == ["git", "fetch"]:
            return ""
        if command == ["git", "rev-parse", "HEAD"]:
            return SOURCE_SHA
        if command == ["git", "rev-parse", "origin/main"]:
            return "f" * 40
        raise AssertionError(command)

    runtime = ReleaseRuntime(
        repository_root=tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
    )

    with pytest.raises(ValueError, match="remote main changed after admission"):
        runtime.collect_admission({"source_revision": SOURCE_SHA})


def test_render_release_documents_updates_only_the_governed_release_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs" / "releases").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "data-olympus"\nversion = "0.6.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [0.6.0] - 2026-07-18\n",
        encoding="utf-8",
    )

    evidence = render_release_documents(
        tmp_path,
        "0.7.0",
        {
            "features": ["feat(automation): make releases outcome based"],
            "fixes": ["fix(mcp): preserve exact source evidence"],
            "breaking": [],
        },
        date(2026, 8, 1),
    )

    assert 'version = "0.7.0"' in (tmp_path / "pyproject.toml").read_text()
    changelog = (tmp_path / "CHANGELOG.md").read_text()
    assert changelog.index("## [Unreleased]") < changelog.index(
        "## [0.7.0] - 2026-08-01"
    )
    assert "feat(automation): make releases outcome based" in changelog
    note = (tmp_path / "docs" / "releases" / "v0.7.0.md").read_text()
    assert note.startswith("# data-olympus 0.7.0")
    assert evidence["content_hash"] == evidence["release_note_hash"]
    assert len(evidence["changelog_hash"]) == 64


def test_completed_check_evidence_requires_every_expected_check_and_no_failures() -> None:
    payload = {
        "check_runs": [
            {"name": "test", "status": "completed", "conclusion": "success"},
            {
                "name": "version-free-guard",
                "status": "completed",
                "conclusion": "success",
            },
            {"name": "CodeQL", "status": "completed", "conclusion": "success"},
        ]
    }

    evidence = completed_check_evidence(
        payload,
        required={"test", "version-free-guard"},
    )

    assert evidence["all_success"] is True
    assert evidence["missing_required"] == []

    payload["check_runs"][2]["conclusion"] = "failure"
    with pytest.raises(ValueError, match="did not succeed"):
        completed_check_evidence(
            payload,
            required={"test", "version-free-guard"},
        )


def test_deployment_state_parses_gateway_yaml_and_binds_both_images() -> None:
    state = deployment_state(
        f"""
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: data-olympus-mcp
  namespace: data-olympus
  annotations:
    keel.sh/policy: never
spec:
  template:
    spec:
      initContainers:
        - name: prepare-git
          image: ghcr.io/knaisoma/data-olympus@{DIGEST}
      containers:
        - name: data-olympus-mcp
          image: ghcr.io/knaisoma/data-olympus@{DIGEST}
status:
  observedGeneration: 7
  readyReplicas: 1
  replicas: 1
  updatedReplicas: 1
"""
    )

    assert state["digest"] == DIGEST
    assert state["keel_policy"] == "never"
    assert state["rollout_complete"] is True


def test_deployment_state_rejects_mixed_container_digests() -> None:
    document = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": "data-olympus-mcp",
            "namespace": "data-olympus",
            "annotations": {"keel.sh/policy": "never"},
        },
        "spec": {
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "kb-main"},
                    "spec": {"accessModes": ["ReadWriteOnce"]},
                    "status": {"phase": "Pending"},
                }
            ],
            "template": {
                "spec": {
                    "initContainers": [
                        {
                            "name": "prepare-git",
                            "image": f"ghcr.io/knaisoma/data-olympus@{DIGEST}",
                        }
                    ],
                    "containers": [
                        {
                            "name": "data-olympus-mcp",
                            "image": (
                                "ghcr.io/knaisoma/data-olympus@sha256:"
                                + "c" * 64
                            ),
                        }
                    ],
                }
            }
        },
        "status": {"readyReplicas": 1, "replicas": 1, "updatedReplicas": 1},
    }

    with pytest.raises(ValueError, match="same exact digest"):
        deployment_state(document)


def test_candidate_release_evidence_binds_assets_source_and_digest() -> None:
    tag = "0.7.0-rc.1"
    evidence = candidate_release_evidence(
        {
            "tag_name": tag,
            "target_commitish": SOURCE_SHA,
            "draft": False,
            "prerelease": True,
            "assets": [
                {"name": "data_olympus-0.7.0rc1-py3-none-any.whl"},
                {"name": "data_olympus-0.7.0rc1.tar.gz"},
                {"name": "release-provenance.json"},
            ],
        },
        {
            "source_sha": SOURCE_SHA,
            "candidate_tag": tag,
            "image_digest": DIGEST,
            "candidate": {
                "version": "0.7.0rc1",
                "source_sha": SOURCE_SHA,
                "wheel": "data_olympus-0.7.0rc1-py3-none-any.whl",
                "sdist": "data_olympus-0.7.0rc1.tar.gz",
            },
        },
        source_revision=SOURCE_SHA,
        version="0.7.0",
        candidate_tag=tag,
        registry_digest=DIGEST,
    )

    assert evidence["candidate_tag"] == tag
    assert evidence["image_digest"] == DIGEST


def test_stable_release_evidence_requires_exact_pypi_hashes() -> None:
    stable = {
        "version": "0.7.0",
        "source_sha": SOURCE_SHA,
        "wheel": "data_olympus-0.7.0-py3-none-any.whl",
        "wheel_sha256": "d" * 64,
        "sdist": "data_olympus-0.7.0.tar.gz",
        "sdist_sha256": "e" * 64,
    }
    evidence = stable_release_evidence(
        {
            "tag_name": "v0.7.0",
            "target_commitish": SOURCE_SHA,
            "draft": False,
            "prerelease": False,
            "assets": [
                {"name": stable["wheel"]},
                {"name": stable["sdist"]},
                {"name": "release-provenance.json"},
            ],
        },
        {"source_sha": SOURCE_SHA, "stable": stable},
        {
            "info": {"version": "0.7.0"},
            "urls": [
                {
                    "filename": stable["wheel"],
                    "digests": {"sha256": stable["wheel_sha256"]},
                },
                {
                    "filename": stable["sdist"],
                    "digests": {"sha256": stable["sdist_sha256"]},
                },
            ],
        },
        source_revision=SOURCE_SHA,
        version="0.7.0",
    )

    assert evidence["source_revision"] == SOURCE_SHA
    assert evidence["pypi_version"] == "0.7.0"


def test_default_dependencies_exposes_every_release_lifecycle_operation() -> None:
    dependencies = default_release_dependencies()

    for name in (
        "collect_admission",
        "prepare",
        "validate",
        "deliver",
        "verify",
        "rollback",
        "set_heartbeat",
    ):
        assert callable(dependencies[name])
    assert "collect_operator_approval" not in dependencies


def test_deployment_manifest_pins_both_containers_and_keeps_keel_disabled() -> None:
    live = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": "data-olympus-mcp",
            "namespace": "data-olympus",
            "annotations": {
                "keel.sh/policy": "never",
                "keel.sh/trigger": "poll",
            },
            "resourceVersion": "123",
            "uid": "server-owned",
        },
        "spec": {
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "kb-main"},
                    "spec": {"accessModes": ["ReadWriteOnce"]},
                    "status": {"phase": "Pending"},
                }
            ],
            "template": {
                "spec": {
                    "initContainers": [
                        {"name": "prepare-git", "image": "old-init"}
                    ],
                    "containers": [
                        {"name": "data-olympus-mcp", "image": "old-main"}
                    ],
                }
            }
        },
        "status": {"readyReplicas": 1},
    }

    manifest = deployment_manifest_for_digest(live, DIGEST)

    expected_image = f"ghcr.io/knaisoma/data-olympus@{DIGEST}"
    assert manifest["metadata"]["annotations"]["keel.sh/policy"] == "never"
    assert "resourceVersion" not in manifest["metadata"]
    assert "uid" not in manifest["metadata"]
    assert "status" not in manifest
    assert "status" not in manifest["spec"]["volumeClaimTemplates"][0]
    assert manifest["spec"]["template"]["spec"]["initContainers"][0][
        "image"
    ] == expected_image
    assert manifest["spec"]["template"]["spec"]["containers"][0][
        "image"
    ] == expected_image


def test_workflow_receipt_is_new_completed_and_bound_to_exact_source(
    tmp_path: Path,
) -> None:
    gateway = StubGateway(
        {
            "actions_list": (
                {"workflow_runs": [{"id": 4, "head_sha": SOURCE_SHA}]},
                {
                    "workflow_runs": [
                        {
                            "id": 5,
                            "head_sha": SOURCE_SHA,
                            "event": "workflow_dispatch",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                },
            ),
            "actions_run_trigger": {},
            "actions_get": {
                "id": 5,
                "head_sha": SOURCE_SHA,
                "conclusion": "success",
            },
        }
    )
    runtime = ReleaseRuntime(tmp_path, gateway=gateway, sleep=lambda _seconds: None)

    receipt = runtime._workflow_run(
        "rc-publish.yml",
        SOURCE_SHA,
        {"number": 1, "ref": SOURCE_SHA},
    )

    assert receipt == {
        "conclusion": "success",
        "run_id": 5,
        "source_revision": SOURCE_SHA,
    }
    assert gateway.calls[1] == (
        "actions_run_trigger",
        {
            "inputs": {"number": 1, "ref": SOURCE_SHA},
            "method": "run_workflow",
            "owner": "knaisoma",
            "ref": "main",
            "repo": "data-olympus",
            "workflow_id": "rc-publish.yml",
        },
    )


def test_merge_uses_github_expected_head_precondition(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        commands.append(command)
        return json.dumps({"merged": True, "sha": SOURCE_SHA})

    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
    )

    result = runtime._merge_exact(182, "0.7.0", "f" * 40)

    assert result == {"merged": True, "sha": SOURCE_SHA}
    assert commands == [
        [
            "gh",
            "api",
            "--method",
            "PUT",
            "repos/knaisoma/data-olympus/pulls/182/merge",
            "--field",
            "merge_method=squash",
            "--field",
            "commit_title=chore(release): prepare v0.7.0",
            "--field",
            "sha=" + "f" * 40,
        ]
    ]


@pytest.mark.parametrize("merge_confirmed", [False, True])
def test_prepare_reports_failed_recovery_evidence_after_merge_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    merge_confirmed: bool,
) -> None:
    head_revision = "c" * 40
    final_revision = "d" * 40
    gateway = StubGateway({"create_pull_request": {"number": 182}})
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=gateway,
        command_output=lambda _command, _cwd, _timeout: "",
    )
    monkeypatch.setattr(
        "scripts.operations.release_runtime.render_release_documents",
        lambda *_arguments: {
            "changelog_hash": "1" * 64,
            "content_hash": "2" * 64,
            "release_note_hash": "2" * 64,
        },
    )
    monkeypatch.setattr(runtime, "source_revision", lambda: head_revision)
    remote_revisions = iter([SOURCE_SHA, final_revision])
    monkeypatch.setattr(runtime, "candidate_revision", lambda: next(remote_revisions))
    monkeypatch.setattr(
        runtime,
        "_wait_pull_request",
        lambda _number, _head: {"checks": {}, "pull": {}},
    )
    if merge_confirmed:
        monkeypatch.setattr(
            runtime,
            "_merge_exact",
            lambda _number, _version, _head: {
                "merged": True,
                "sha": final_revision,
            },
        )
        monkeypatch.setattr(
            runtime,
            "_final_local_gates",
            lambda _source, _version: (_ for _ in ()).throw(
                ValueError("post merge gates failed")
            ),
        )
    else:
        monkeypatch.setattr(
            runtime,
            "_merge_exact",
            lambda _number, _version, _head: (_ for _ in ()).throw(
                ValueError("merge response lost")
            ),
        )

    with pytest.raises(ReleaseDeliveryError) as raised:
        runtime.prepare(
            {
                "extra_context": "No extra context for this run",
                "run_id": "11111111-2222-4333-8444-555555555555",
                "source_revision": SOURCE_SHA,
            },
            {
                "evidence": {
                    "computed_release": {
                        "changes": {"breaking": [], "features": [], "fixes": []}
                    }
                },
                "outputs": {"candidate": {"version": "0.7.0"}},
            },
        )

    assert raised.value.external_state_changed is True
    assert raised.value.evidence["external_state_changed"] is True
    assert raised.value.evidence["merge_confirmed"] is merge_confirmed
    assert raised.value.evidence["release_pr_number"] == 182
    assert raised.value.evidence["rollback_completed"] is False
    if merge_confirmed:
        assert raised.value.evidence["merged_revision"] == final_revision


@pytest.mark.parametrize("dispatch_started", [False, True])
def test_delivery_failure_classification_tracks_the_workflow_dispatch_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dispatch_started: bool,
) -> None:
    previous = "sha256:" + "e" * 64
    runtime = ReleaseRuntime(tmp_path, gateway=StubGateway({"list_tags": []}))
    runtime._prepared = {
        "raw": {
            "candidate": {"version": "0.7.0", "source_revision": SOURCE_SHA},
            "rollback_point": {"digest": previous},
        }
    }

    def workflow_run(
        _name: str,
        _source: str,
        _inputs: dict[str, object],
        *,
        on_dispatch: object = None,
    ) -> dict[str, object]:
        if dispatch_started:
            assert callable(on_dispatch)
            on_dispatch()
        raise ValueError("workflow preflight or dispatch failed")

    monkeypatch.setattr(runtime, "_workflow_run", workflow_run)

    with pytest.raises(ValueError) as raised:
        runtime.deliver({}, {}, {}, {})

    if dispatch_started:
        assert isinstance(raised.value, ReleaseDeliveryError)
        assert raised.value.evidence == {
            "deployment_started": False,
            "external_state_changed": True,
            "rollback_completed": False,
        }
    else:
        assert type(raised.value) is ValueError
        assert getattr(raised.value, "external_state_changed", False) is False


def test_partial_delivery_failure_restores_the_exact_previous_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = "sha256:" + "e" * 64
    gateway = StubGateway({"list_tags": []})
    runtime = ReleaseRuntime(tmp_path, gateway=gateway)
    runtime._prepared = {
        "raw": {
            "candidate": {"version": "0.7.0", "source_revision": SOURCE_SHA},
            "rollback_point": {"digest": previous},
        }
    }
    applied: list[str] = []
    acceptance = {
        "documentation": True,
        "enforcement": True,
        "healthy": True,
        "mcp_search": True,
        "ready": True,
    }
    def workflow_run(
        _name: str,
        source: str,
        _inputs: dict[str, object],
        *,
        on_dispatch: object = None,
    ) -> dict[str, object]:
        assert callable(on_dispatch)
        on_dispatch()
        return {
            "conclusion": "success",
            "run_id": 1,
            "source_revision": source,
        }

    monkeypatch.setattr(runtime, "_workflow_run", workflow_run)
    monkeypatch.setattr(
        runtime,
        "_candidate_publication",
        lambda _source, _version, _tag: {"image_digest": DIGEST},
    )
    monkeypatch.setattr(
        runtime,
        "_apply_digest",
        lambda digest: (
            applied.append(digest)
            or {"digest": digest, "keel_policy": "never", "rollout_complete": True}
        ),
    )
    monkeypatch.setattr(runtime, "_service_acceptance", lambda: acceptance)
    monkeypatch.setattr(
        runtime,
        "_stable_publication",
        lambda _source, _version: (_ for _ in ()).throw(ValueError("stable failed")),
    )

    with pytest.raises(ReleaseDeliveryError, match="stable failed") as raised:
        runtime.deliver({}, {}, {}, {})

    assert applied == [DIGEST, previous]
    assert raised.value.external_state_changed is True
    assert raised.value.evidence == {
        "deployment_started": True,
        "external_state_changed": True,
        "rollback_completed": True,
    }
