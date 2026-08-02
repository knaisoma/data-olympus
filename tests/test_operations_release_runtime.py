from __future__ import annotations

import json
from datetime import date
from hashlib import sha256
from typing import TYPE_CHECKING

import pytest

from scripts.operations.release_runtime import (
    REQUIRED_MAIN_CHECKS,
    REQUIRED_PULL_REQUEST_CHECKS,
    FastMCPGateway,
    ReleaseDeliveryError,
    ReleaseRuntime,
    UnboundGatewayResultError,
    _authority_consult,
    _authority_gate_check,
    candidate_release_evidence,
    completed_check_evidence,
    default_release_dependencies,
    deployment_manifest_for_digest,
    deployment_state,
    governed_release_controls,
    render_release_documents,
    stable_release_evidence,
    validate_prepared_release_documents,
)

if TYPE_CHECKING:
    from pathlib import Path

SOURCE_SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
CANDIDATE_SHA = "c" * 40
CANDIDATE_TREE = "1" * 40
DELIVERY_SHA = "d" * 40


def _release_ruleset() -> dict[str, object]:
    return {
        "id": 18080131,
        "name": "main protection",
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH"]}
        },
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 1,
                    "require_code_owner_review": True,
                    "require_last_push_approval": True,
                    "required_review_thread_resolution": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": "test"}]
                },
            },
            {"type": "required_linear_history"},
            {
                "type": "code_scanning",
                "parameters": {
                    "code_scanning_tools": [
                        {
                            "tool": "CodeQL",
                            "security_alerts_threshold": "high_or_higher",
                            "alerts_threshold": "errors",
                        }
                    ]
                },
            },
        ],
        "bypass_actors": [
            {"actor_id": 18280424, "actor_type": "Team", "bypass_mode": "always"}
        ],
        "current_user_can_bypass": "always",
    }


def _successful_pull_checks() -> dict[str, object]:
    return {
        "check_runs": [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in sorted(REQUIRED_PULL_REQUEST_CHECKS)
        ]
    }


def _review_state() -> dict[str, object]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "baseRefOid": SOURCE_SHA,
                    "headRefOid": CANDIDATE_SHA,
                    "isDraft": False,
                    "mergeStateStatus": "BLOCKED",
                    "merged": False,
                    "reviewDecision": "REVIEW_REQUIRED",
                    "state": "OPEN",
                    "reviewThreads": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False},
                        "totalCount": 0,
                    },
                }
            }
        }
    }


def _codeql_analyses() -> list[dict[str, object]]:
    return [
        {
            "analysis_key": "dynamic/github-code-scanning/codeql:analyze",
            "category": f"/language:{language}",
            "commit_sha": CANDIDATE_SHA,
            "error": "",
            "results_count": 0,
        }
        for language in ("actions", "javascript-typescript", "python")
    ]


def test_required_checks_match_pull_request_and_main_github_surfaces() -> None:
    codeql_analyses = {
        "Analyze (actions)",
        "Analyze (javascript-typescript)",
        "Analyze (python)",
    }

    assert codeql_analyses <= REQUIRED_PULL_REQUEST_CHECKS
    assert codeql_analyses <= REQUIRED_MAIN_CHECKS
    assert "CodeQL" in REQUIRED_PULL_REQUEST_CHECKS
    assert "CodeQL" not in REQUIRED_MAIN_CHECKS


def test_governed_release_controls_rederive_every_bypassed_rule() -> None:
    evidence = governed_release_controls(
        admitted_revision=SOURCE_SHA,
        candidate_revision=CANDIDATE_SHA,
        checks=_successful_pull_checks(),
        codeql_alerts=[],
        codeql_analyses=_codeql_analyses(),
        review_state=_review_state(),
        rulesets=[_release_ruleset()],
    )

    assert evidence["candidate_revision"] == CANDIDATE_SHA
    assert evidence["codeql_languages"] == [
        "actions",
        "javascript-typescript",
        "python",
    ]
    assert evidence["review_decision"] == "REVIEW_REQUIRED"
    assert evidence["unresolved_review_threads"] == 0
    assert len(evidence["ruleset_fingerprint"]) == 64


def test_governed_release_controls_block_missing_codeql_ruleset_threshold() -> None:
    ruleset = _release_ruleset()
    ruleset["rules"] = [
        rule for rule in ruleset["rules"] if rule["type"] != "code_scanning"
    ]

    with pytest.raises(ValueError, match="CodeQL ruleset thresholds"):
        governed_release_controls(
            admitted_revision=SOURCE_SHA,
            candidate_revision=CANDIDATE_SHA,
            checks=_successful_pull_checks(),
            codeql_alerts=[],
            codeql_analyses=_codeql_analyses(),
            review_state=_review_state(),
            rulesets=[ruleset],
        )


def test_governed_release_controls_do_not_depend_on_paid_code_quality() -> None:
    ruleset = _release_ruleset()
    checks = _successful_pull_checks()

    evidence = governed_release_controls(
        admitted_revision=SOURCE_SHA,
        candidate_revision=CANDIDATE_SHA,
        checks=checks,
        codeql_alerts=[],
        codeql_analyses=_codeql_analyses(),
        review_state=_review_state(),
        rulesets=[ruleset],
    )

    assert "code_quality_check" not in evidence
    assert "code_quality_state" not in evidence
    assert evidence["codeql_languages"] == [
        "actions",
        "javascript-typescript",
        "python",
    ]


def test_governed_release_controls_block_ruleset_or_candidate_drift() -> None:
    changed = _release_ruleset()
    changed["rules"] = [
        rule
        for rule in changed["rules"]
        if rule["type"] != "required_linear_history"
    ]

    with pytest.raises(ValueError, match="required linear history"):
        governed_release_controls(
            admitted_revision=SOURCE_SHA,
            candidate_revision=CANDIDATE_SHA,
            checks=_successful_pull_checks(),
            codeql_alerts=[],
            codeql_analyses=_codeql_analyses(),
            review_state=_review_state(),
            rulesets=[changed],
        )


def test_ruleset_fingerprint_excludes_transport_metadata() -> None:
    original = _release_ruleset()
    original.update(
        {
            "_links": {"self": {"href": "https://api.github.test/old"}},
            "created_at": "2026-06-24T18:47:03Z",
            "node_id": "old-node",
            "updated_at": "2026-07-01T09:08:06Z",
        }
    )
    refreshed = {**original}
    refreshed.update(
        {
            "_links": {"self": {"href": "https://api.github.test/new"}},
            "node_id": "new-node",
            "updated_at": "2026-08-01T16:00:00Z",
        }
    )
    arguments = {
        "admitted_revision": SOURCE_SHA,
        "candidate_revision": CANDIDATE_SHA,
        "checks": _successful_pull_checks(),
        "codeql_alerts": [],
        "codeql_analyses": _codeql_analyses(),
        "review_state": _review_state(),
    }

    first = governed_release_controls(**arguments, rulesets=[original])
    second = governed_release_controls(**arguments, rulesets=[refreshed])

    assert first["ruleset_fingerprint"] == second["ruleset_fingerprint"]


def test_collect_release_controls_queries_codeql_without_code_quality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway(),
        command_output=lambda _command, _cwd, _timeout: "",
    )

    def github_json(arguments: list[str], *, timeout: int = 60) -> object:
        del timeout
        commands.append(arguments)
        endpoint = arguments[-1]
        if "analyses" in endpoint:
            return [_codeql_analyses()]
        if "alerts" in endpoint:
            return [[]]
        raise AssertionError(f"unexpected GitHub API call: {arguments}")

    monkeypatch.setattr(runtime, "_github_json", github_json)
    monkeypatch.setattr(runtime, "_review_state", lambda _number: _review_state())
    monkeypatch.setattr(runtime, "_active_rulesets", lambda: [_release_ruleset()])

    evidence = runtime._collect_release_controls(
        number=195,
        head_revision=CANDIDATE_SHA,
        base_revision=SOURCE_SHA,
        checks=_successful_pull_checks(),
    )

    assert evidence["codeql_languages"] == [
        "actions",
        "javascript-typescript",
        "python",
    ]
    assert all("code-quality" not in " ".join(command) for command in commands)


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


def test_authority_consult_calls_data_olympus_mcp_directly() -> None:
    calls: list[list[str]] = []
    arguments = {
        "agent_identity": "ai-operations-release",
        "intent": "Prepare governed release.",
        "source_session": "run-id",
        "trigger": "explicit",
        "workspace": "data-olympus",
    }

    def run_command(command: list[str], _cwd: Path, _timeout: int) -> str:
        calls.append(command)
        return json.dumps(
            {
                "content": [{"type": "text", "text": "consulted"}],
                "is_error": False,
                "structured_content": {
                    "consulted_at": 1.0,
                    "ttl_seconds": 300,
                },
            }
        )

    result = _authority_consult(arguments, run_command)

    assert result == {"consulted_at": 1.0, "ttl_seconds": 300}
    assert calls[0][:6] == [
        "fastmcp",
        "call",
        "--server-spec",
        (
            "http://data-olympus-mcp.data-olympus."
            "apps.172.30.1.2.nip.io/mcp"
        ),
        "--target",
        "kb_consult",
    ]
    assert json.loads(calls[0][7]) == arguments


def test_authority_gate_check_binds_session_and_workspace_directly() -> None:
    calls: list[list[str]] = []
    arguments = {
        "action_diff": "Prepare governed release v0.7.0.",
        "action_path": "pyproject.toml",
        "session_id": "run-id",
        "tool_name": "git commit",
        "workspace": "data-olympus",
    }
    receipt = {
        "verdict": "allow",
        "reason": "fresh explicit consultation on record",
        "rules": [],
        "session_id": "run-id",
        "workspace": "data-olympus",
    }

    def run_command(command: list[str], _cwd: Path, _timeout: int) -> str:
        calls.append(command)
        return json.dumps(
            {
                "content": [{"type": "text", "text": "allowed"}],
                "is_error": False,
                "structured_content": receipt,
            }
        )

    result = _authority_gate_check(arguments, run_command)

    assert result == receipt
    assert calls[0][:6] == [
        "fastmcp",
        "call",
        "--server-spec",
        (
            "http://data-olympus-mcp.data-olympus."
            "apps.172.30.1.2.nip.io/mcp"
        ),
        "--target",
        "kb_gate_check",
    ]
    assert json.loads(calls[0][7]) == arguments


def test_fastmcp_gateway_accepts_the_standard_text_content_envelope() -> None:
    def run_command(_command: list[str], _cwd: Path, _timeout: int) -> str:
        return json.dumps(
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "tool": "create_pull_request",
                                "result": json.dumps(
                                    {
                                        "id": "PR_kwDOQFQYxM6Yxw",
                                        "url": (
                                            "https://github.com/knaisoma/"
                                            "data-olympus/pull/185"
                                        ),
                                    }
                                ),
                            }
                        ),
                    }
                ],
                "is_error": False,
            }
        )

    result = FastMCPGateway(run_command=run_command).execute(
        "create_pull_request",
        {"owner": "knaisoma", "repo": "data-olympus"},
    )

    assert result == {
        "id": "PR_kwDOQFQYxM6Yxw",
        "url": "https://github.com/knaisoma/data-olympus/pull/185",
    }


def test_fastmcp_gateway_rejects_unbound_text_content() -> None:
    def run_command(_command: list[str], _cwd: Path, _timeout: int) -> str:
        return json.dumps(
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"id": "unbound-result"}),
                    }
                ],
                "is_error": False,
            }
        )

    with pytest.raises(UnboundGatewayResultError, match="omitted gateway result"):
        FastMCPGateway(run_command=run_command).execute(
            "create_pull_request",
            {"owner": "knaisoma", "repo": "data-olympus"},
        )


def test_fastmcp_gateway_rejects_a_different_tool_result() -> None:
    def run_command(_command: list[str], _cwd: Path, _timeout: int) -> str:
        return json.dumps(
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"tool": "list_tags", "result": "[]"}
                        ),
                    }
                ],
                "is_error": False,
            }
        )

    with pytest.raises(ValueError, match="tool does not match request"):
        FastMCPGateway(run_command=run_command).execute(
            "create_pull_request",
            {"owner": "knaisoma", "repo": "data-olympus"},
        )


def test_gateway_object_never_retries_an_unbound_mutation_response(
    tmp_path: Path,
) -> None:
    class UnboundMutationGateway:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _name: str, _arguments: dict[str, object]) -> object:
            self.calls += 1
            raise UnboundGatewayResultError(
                "FastMCP text content omitted gateway result"
            )

    sleeps: list[float] = []
    gateway = UnboundMutationGateway()
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=gateway,
        sleep=sleeps.append,
    )

    with pytest.raises(UnboundGatewayResultError, match="omitted gateway result"):
        runtime.gateway_object("create_pull_request", {})

    assert gateway.calls == 1
    assert sleeps == []


def test_list_tags_retries_one_unbound_fastmcp_text_response(
    tmp_path: Path,
) -> None:
    class FlakyReadGateway:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, name: str, arguments: dict[str, object]) -> object:
            assert name == "list_tags"
            assert arguments == {
                "owner": "knaisoma",
                "page": 1,
                "perPage": 100,
                "repo": "data-olympus",
            }
            self.calls += 1
            if self.calls == 1:
                raise UnboundGatewayResultError(
                    "FastMCP text content omitted gateway result"
                )
            return [{"name": "v0.6.0"}]

    sleeps: list[float] = []
    gateway = FlakyReadGateway()
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=gateway,
        sleep=sleeps.append,
    )

    assert runtime._list_tags() == [{"name": "v0.6.0"}]
    assert gateway.calls == 2
    assert sleeps == [1.0]


def test_list_tags_does_not_retry_other_gateway_failures(tmp_path: Path) -> None:
    class InvalidGateway:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _name: str, _arguments: dict[str, object]) -> object:
            self.calls += 1
            raise ValueError("FastMCP result tool does not match request")

    sleeps: list[float] = []
    gateway = InvalidGateway()
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=gateway,
        sleep=sleeps.append,
    )

    with pytest.raises(ValueError, match="tool does not match request"):
        runtime._list_tags()

    assert gateway.calls == 1
    assert sleeps == []


def test_list_tags_stops_after_one_unbound_response_retry(
    tmp_path: Path,
) -> None:
    class UnboundGateway:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _name: str, _arguments: dict[str, object]) -> object:
            self.calls += 1
            raise UnboundGatewayResultError(
                "FastMCP text content omitted gateway result"
            )

    sleeps: list[float] = []
    gateway = UnboundGateway()
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=gateway,
        sleep=sleeps.append,
    )

    with pytest.raises(ValueError, match="omitted gateway result"):
        runtime._list_tags()

    assert gateway.calls == 2
    assert sleeps == [1.0]


def test_prepare_fails_before_mutation_without_authority_consult_receipt(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        commands.append(command)
        if command == ["git", "status", "--porcelain"]:
            return ""
        raise AssertionError(command)

    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
        authority_consult=lambda _arguments: {},
    )

    with pytest.raises(ValueError, match="authority consultation receipt"):
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

    assert commands == [["git", "status", "--porcelain"]]


def test_prepare_fails_before_mutation_on_stale_authority_consultation(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        commands.append(command)
        if command == ["git", "status", "--porcelain"]:
            return ""
        raise AssertionError(command)

    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
        authority_consult=lambda _arguments: {
            "consulted_at": 100.0,
            "ttl_seconds": 300,
        },
        authority_gate_check=lambda _arguments: pytest.fail(
            "stale consultation must not reach the gate check"
        ),
        clock=lambda: 401.0,
    )

    with pytest.raises(ValueError, match="authority consultation is not fresh"):
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

    assert commands == [["git", "status", "--porcelain"]]


def test_prepare_fails_before_mutation_on_unbound_authority_gate_receipt(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        commands.append(command)
        if command == ["git", "status", "--porcelain"]:
            return ""
        raise AssertionError(command)

    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
        authority_consult=lambda _arguments: {
            "consulted_at": 100.0,
            "ttl_seconds": 300,
        },
        authority_gate_check=lambda _arguments: {
            "verdict": "allow",
            "session_id": "different-run",
            "workspace": "data-olympus",
        },
        clock=lambda: 100.0,
    )

    with pytest.raises(ValueError, match="authority gate receipt is invalid"):
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

    assert commands == [["git", "status", "--porcelain"]]


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


def test_distribution_smoke_retries_one_transient_failure(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    sleeps: list[float] = []

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        commands.append(command)
        if len(commands) == 1:
            raise ValueError("command failed: uv")
        return "installed artifact smoke: ok"

    runtime = ReleaseRuntime(
        repository_root=tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
        sleep=sleeps.append,
    )
    artifact = tmp_path / "data_olympus-0.7.0.tar.gz"

    output = runtime._smoke_distribution(artifact, "0.7.0")

    assert output == "installed artifact smoke: ok"
    assert len(commands) == 2
    assert commands[0] == commands[1]
    assert sleeps == [2.0]


def test_distribution_smoke_stops_after_one_retry(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        commands.append(command)
        raise ValueError("command failed: uv")

    runtime = ReleaseRuntime(
        repository_root=tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(ValueError, match="command failed: uv"):
        runtime._smoke_distribution(
            tmp_path / "data_olympus-0.7.0.tar.gz",
            "0.7.0",
        )

    assert len(commands) == 2


def test_final_local_gates_install_declared_dev_extra_for_python_test_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        commands.append(command)
        if command[:2] == ["uv", "build"]:
            output_directory = tmp_path / "to-delete" / "aiops-release-build"
            (output_directory / "data_olympus-0.7.0-py3-none-any.whl").touch()
            (output_directory / "data_olympus-0.7.0.tar.gz").touch()
        return "ok"

    runtime = ReleaseRuntime(
        repository_root=tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
    )
    monkeypatch.setattr(runtime, "_smoke_distribution", lambda *_arguments: "ok")
    monkeypatch.setattr(
        runtime,
        "_wait_main_checks",
        lambda _revision: {"all_success": True, "missing_required": []},
    )

    runtime._final_local_gates(SOURCE_SHA, "0.7.0")

    expected_prefix = ["uv", "run", "--extra", "dev", "--python", "3.13"]
    expected_tools = {"ruff", "mypy", "pytest"}
    actual = {
        command[len(expected_prefix)]
        for command in commands
        if command[: len(expected_prefix)] == expected_prefix
        and len(command) > len(expected_prefix)
        and command[len(expected_prefix)] in expected_tools
    }
    assert actual == expected_tools


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
    assert evidence["document_mode"] == "normal"


def _write_prepared_release_documents(
    root: Path,
    *,
    changelog_fixed_item: str = "fix(mcp): preserve exact source evidence",
    include_note: bool = True,
    include_target: bool = True,
) -> tuple[Path, Path, Path]:
    note_path = root / "docs" / "releases" / "v0.7.0.md"
    note_path.parent.mkdir(parents=True)
    pyproject_path = root / "pyproject.toml"
    pyproject_path.write_text(
        '[project]\nname = "data-olympus"\nversion = "0.7.0"\n',
        encoding="utf-8",
    )
    target = ""
    if include_target:
        target = (
            "## [0.7.0] - 2026-08-01\n\n"
            "### New features\n\n"
            "* feat(automation): make releases outcome based\n"
            "### Fixed\n\n"
            f"* {changelog_fixed_item}\n\n\n"
            "### Fixed\n\n"
            "* **Preserved prior detailed release prose.** Exact details remain.\n\n"
        )
    changelog_path = root / "CHANGELOG.md"
    changelog_path.write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "### Fixed\n\n"
        "* Retry one transient bound tag inventory response.\n\n"
        f"{target}"
        "## [0.6.0] - 2026-07-18\n",
        encoding="utf-8",
    )
    if include_note:
        note_path.write_text(
            "# data-olympus 0.7.0\n\n"
            "## New features\n\n"
            "* feat(automation): make releases outcome based\n\n"
            "## Fixed\n\n"
            "* fix(mcp): preserve exact source evidence\n",
            encoding="utf-8",
        )
    return pyproject_path, changelog_path, note_path


def _roll_forward_changes() -> dict[str, list[str]]:
    return {
        "breaking": [],
        "features": ["feat(automation): make releases outcome based"],
        "fixes": [
            "fix(mcp): preserve exact source evidence",
            "fix(release): retry bound tag inventory read (#208)",
        ],
    }


def test_render_release_documents_rolls_forward_one_unpublished_preparation(
    tmp_path: Path,
) -> None:
    _, changelog_path, note_path = _write_prepared_release_documents(tmp_path)

    evidence = render_release_documents(
        tmp_path,
        "0.7.0",
        _roll_forward_changes(),
        date(2026, 8, 2),
    )

    changelog = changelog_path.read_text(encoding="utf-8")
    note = note_path.read_text(encoding="utf-8")
    assert evidence["document_mode"] == "roll_forward"
    assert changelog.count("## [Unreleased]") == 1
    assert "## [Unreleased]\n\n## [0.7.0] - 2026-08-02" in changelog
    assert changelog.count("## [0.7.0] - 2026-08-02") == 1
    assert changelog.count("fix(release): retry bound tag inventory read (#208)") == 1
    assert note.count("fix(release): retry bound tag inventory read (#208)") == 1
    assert "**Preserved prior detailed release prose.**" in changelog
    assert "Retry one transient bound tag inventory response." in changelog
    assert changelog.index("**Preserved prior detailed release prose.**") < (
        changelog.index("Retry one transient bound tag inventory response.")
    )


@pytest.mark.parametrize(
    ("include_note", "include_target", "current_fixed", "changelog_fixed"),
    [
        (True, False, "fix(mcp): preserve exact source evidence", None),
        (False, True, "fix(mcp): preserve exact source evidence", None),
        (True, True, "fix(other): absent from current computation", None),
        (
            True,
            True,
            "fix(mcp): preserve exact source evidence",
            "fix(mcp): preserve exact source evidence but altered",
        ),
    ],
)
def test_render_release_documents_rejects_inconsistent_preparation_atomically(
    tmp_path: Path,
    include_note: bool,
    include_target: bool,
    current_fixed: str,
    changelog_fixed: str | None,
) -> None:
    paths = _write_prepared_release_documents(
        tmp_path,
        include_note=include_note,
        include_target=include_target,
        changelog_fixed_item=(
            changelog_fixed or "fix(mcp): preserve exact source evidence"
        ),
    )
    note_path = paths[2]
    if include_note and current_fixed != "fix(mcp): preserve exact source evidence":
        note_path.write_text(
            "# data-olympus 0.7.0\n\n"
            "## New features\n\n"
            "* feat(automation): make releases outcome based\n\n"
            "## Fixed\n\n"
            f"* {current_fixed}\n",
            encoding="utf-8",
        )
    before = {
        path: path.read_bytes() if path.exists() else None
        for path in paths
    }

    with pytest.raises(ValueError):
        render_release_documents(
            tmp_path,
            "0.7.0",
            _roll_forward_changes(),
            date(2026, 8, 2),
        )

    assert {
        path: path.read_bytes() if path.exists() else None
        for path in paths
    } == before


def test_render_release_documents_rejects_roll_forward_replay_atomically(
    tmp_path: Path,
) -> None:
    paths = _write_prepared_release_documents(tmp_path)
    before = {path: path.read_bytes() for path in paths}
    changes = _roll_forward_changes()
    changes["fixes"] = ["fix(mcp): preserve exact source evidence"]

    with pytest.raises(ValueError, match="new release item"):
        render_release_documents(
            tmp_path,
            "0.7.0",
            changes,
            date(2026, 8, 2),
        )

    assert {path: path.read_bytes() for path in paths} == before


def _write_resumable_release_documents(root: Path) -> None:
    _write_prepared_release_documents(root)
    changelog_path = root / "CHANGELOG.md"
    changelog_path.write_text(
        changelog_path.read_text(encoding="utf-8").replace(
            "## [Unreleased]\n\n"
            "### Fixed\n\n"
            "* Retry one transient bound tag inventory response.\n\n",
            "## [Unreleased]\n\n",
            1,
        ),
        encoding="utf-8",
    )


def test_validate_prepared_release_documents_accepts_exact_unpublished_state(
    tmp_path: Path,
) -> None:
    _write_resumable_release_documents(tmp_path)

    evidence = validate_prepared_release_documents(
        tmp_path,
        "0.7.0",
        {
            "breaking": [],
            "features": ["feat(automation): make releases outcome based"],
            "fixes": ["fix(mcp): preserve exact source evidence"],
        },
    )

    assert evidence["document_mode"] == "prepared_unpublished"
    assert evidence["release_date"] == "2026-08-01"
    assert len(evidence["changelog_hash"]) == 64
    assert len(evidence["release_note_hash"]) == 64


@pytest.mark.parametrize(
    ("target", "replacement", "reason"),
    [
        (
            "## [Unreleased]\n\n",
            "## [Unreleased]\n\n### Fixed\n\n* late change\n\n",
            "Unreleased",
        ),
        (
            "* fix(mcp): preserve exact source evidence\n",
            "* fix(mcp): altered release note\n",
            "release note",
        ),
        (
            "* fix(mcp): preserve exact source evidence\n\n\n### Fixed",
            "* fix(mcp): altered canonical prefix\n\n\n### Fixed",
            "canonical prefix",
        ),
    ],
)
def test_validate_prepared_release_documents_rejects_any_state_drift(
    tmp_path: Path,
    target: str,
    replacement: str,
    reason: str,
) -> None:
    _write_resumable_release_documents(tmp_path)
    path = (
        tmp_path / "docs" / "releases" / "v0.7.0.md"
        if "release note" in reason
        else tmp_path / "CHANGELOG.md"
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace(target, replacement, 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=reason):
        validate_prepared_release_documents(
            tmp_path,
            "0.7.0",
            {
                "breaking": [],
                "features": ["feat(automation): make releases outcome based"],
                "fixes": ["fix(mcp): preserve exact source evidence"],
            },
        )


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


def test_pull_request_waiter_waits_for_complete_check_matrix(
    tmp_path: Path,
) -> None:
    pending = {
        "check_runs": [
            {
                "name": name,
                "status": "in_progress" if name == "test" else "completed",
                "conclusion": (
                    "failure"
                    if name == "CodeQL"
                    else None if name == "test" else "success"
                ),
            }
            for name in sorted(REQUIRED_PULL_REQUEST_CHECKS)
        ]
    }
    completed = {
        "check_runs": [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
            for name in sorted(REQUIRED_PULL_REQUEST_CHECKS)
        ]
    }
    pull = {
        "base": {"sha": "b" * 40},
        "draft": False,
        "head": {"sha": SOURCE_SHA},
        "mergeable_state": "blocked",
        "merged": False,
        "state": "open",
    }
    gateway = StubGateway(
        {"pull_request_read": (pending, completed, pull)}
    )
    sleeps: list[float] = []
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=gateway,
        sleep=sleeps.append,
    )
    controls = {"ruleset_fingerprint": "4" * 64}
    runtime._collect_release_controls = lambda **_arguments: controls

    result = runtime._wait_pull_request(190, SOURCE_SHA, "b" * 40)

    assert result == {"checks": completed_check_evidence(
        completed,
        required=REQUIRED_PULL_REQUEST_CHECKS,
    ), "controls": controls, "pull": pull}
    assert sleeps == [10]


def test_pull_request_waiter_fails_when_complete_matrix_is_not_green(
    tmp_path: Path,
) -> None:
    failed = {
        "check_runs": [
            {
                "name": name,
                "status": "completed",
                "conclusion": "failure" if name == "CodeQL" else "success",
            }
            for name in sorted(REQUIRED_PULL_REQUEST_CHECKS)
        ]
    }
    sleeps: list[float] = []
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway({"pull_request_read": failed}),
        sleep=sleeps.append,
    )

    with pytest.raises(ValueError, match="GitHub check runs did not succeed"):
        runtime._wait_pull_request(192, SOURCE_SHA, "b" * 40)

    assert sleeps == []


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


def test_workflow_retries_one_unbound_read_without_replaying_dispatch(
    tmp_path: Path,
) -> None:
    class FlakyReadGateway:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.list_calls = 0

        def execute(self, name: str, arguments: dict[str, object]) -> object:
            self.calls.append((name, arguments))
            if name == "actions_list":
                self.list_calls += 1
                if self.list_calls == 1:
                    raise UnboundGatewayResultError(
                        "FastMCP text content omitted gateway result"
                    )
                if self.list_calls == 2:
                    return {"workflow_runs": [{"id": 4, "head_sha": SOURCE_SHA}]}
                return {
                    "workflow_runs": [
                        {
                            "id": 5,
                            "head_sha": SOURCE_SHA,
                            "event": "workflow_dispatch",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                }
            if name == "actions_run_trigger":
                return {}
            if name == "actions_get":
                return {
                    "id": 5,
                    "head_sha": SOURCE_SHA,
                    "conclusion": "success",
                }
            raise AssertionError(f"unexpected gateway tool: {name}")

    sleeps: list[float] = []
    gateway = FlakyReadGateway()
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=gateway,
        sleep=sleeps.append,
    )

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
    assert [name for name, _arguments in gateway.calls] == [
        "actions_list",
        "actions_list",
        "actions_run_trigger",
        "actions_list",
        "actions_get",
    ]
    assert sleeps == [1.0]


def test_workflow_stops_after_one_unbound_read_retry_before_dispatch(
    tmp_path: Path,
) -> None:
    class UnboundReadGateway:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def execute(self, name: str, _arguments: dict[str, object]) -> object:
            self.calls.append(name)
            raise UnboundGatewayResultError(
                "FastMCP text content omitted gateway result"
            )

    sleeps: list[float] = []
    gateway = UnboundReadGateway()
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=gateway,
        sleep=sleeps.append,
    )

    with pytest.raises(UnboundGatewayResultError, match="omitted gateway result"):
        runtime._workflow_run(
            "rc-publish.yml",
            SOURCE_SHA,
            {"number": 1, "ref": SOURCE_SHA},
        )

    assert gateway.calls == ["actions_list", "actions_list"]
    assert sleeps == [1.0]


def test_workflow_never_retries_an_unbound_dispatch_response(tmp_path: Path) -> None:
    class UnboundDispatchGateway:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def execute(self, name: str, _arguments: dict[str, object]) -> object:
            self.calls.append(name)
            if name == "actions_list":
                return {"workflow_runs": []}
            raise UnboundGatewayResultError(
                "FastMCP text content omitted gateway result"
            )

    sleeps: list[float] = []
    gateway = UnboundDispatchGateway()
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=gateway,
        sleep=sleeps.append,
    )

    with pytest.raises(UnboundGatewayResultError, match="omitted gateway result"):
        runtime._workflow_run(
            "rc-publish.yml",
            SOURCE_SHA,
            {"number": 1, "ref": SOURCE_SHA},
        )

    assert gateway.calls == ["actions_list", "actions_run_trigger"]
    assert sleeps == []


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


def test_merge_response_distinguishes_no_merge_from_confirmed_missing_identity(
    tmp_path: Path,
) -> None:
    runtime = ReleaseRuntime(tmp_path, gateway=StubGateway())

    with pytest.raises(ValueError) as unmerged:
        runtime._confirmed_merge_revision(
            {"merged": False, "message": "head changed"},
            number=182,
            head_revision="f" * 40,
        )

    assert type(unmerged.value) is ValueError
    assert getattr(unmerged.value, "external_state_changed", False) is False

    with pytest.raises(ReleaseDeliveryError) as confirmed:
        runtime._confirmed_merge_revision(
            {"merged": True},
            number=182,
            head_revision="f" * 40,
        )

    assert confirmed.value.evidence == {
        "external_state_changed": True,
        "merge_confirmed": True,
        "merge_outcome": "merged",
        "release_pr_head_revision": "f" * 40,
        "release_pr_number": 182,
        "rollback_completed": False,
    }


def test_prepare_rolls_forward_new_changes_to_unmerged_review_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_prepared_release_documents(tmp_path)
    consultations: list[dict[str, object]] = []
    gate_checks: list[dict[str, object]] = []
    gateway = StubGateway(
        {
            "create_pull_request": {
                "id": "PR_kwDOQFQYxM6Yxw",
                "url": "https://github.com/knaisoma/data-olympus/pull/182",
            }
        }
    )
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=gateway,
        command_output=lambda _command, _cwd, _timeout: "",
        authority_consult=lambda arguments: (
            consultations.append(arguments)
            or {"consulted_at": 1.0, "ttl_seconds": 300}
        ),
        authority_gate_check=lambda arguments: (
            gate_checks.append(arguments)
            or {
                "verdict": "allow",
                "session_id": "11111111-2222-4333-8444-555555555555",
                "workspace": "data-olympus",
            }
        ),
        clock=lambda: 1.0,
    )
    monkeypatch.setattr(
        "scripts.operations.release_runtime.render_release_documents",
        lambda *_arguments: {
            "changelog_hash": "1" * 64,
            "content_hash": "2" * 64,
            "document_mode": "roll_forward",
            "release_note_hash": "2" * 64,
        },
    )
    monkeypatch.setattr(runtime, "source_revision", lambda: CANDIDATE_SHA)
    monkeypatch.setattr(runtime, "remote_main_revision", lambda: SOURCE_SHA)
    monkeypatch.setattr(runtime, "_tree_revision", lambda _revision: CANDIDATE_TREE)
    monkeypatch.setattr(runtime, "_parent_revisions", lambda _revision: [SOURCE_SHA])
    monkeypatch.setattr(
        runtime,
        "_review_material",
        lambda _version, source, _base: {
            "mode": "pull_request_diff",
            "source_revision": source,
        },
    )
    monkeypatch.setattr(
        runtime,
        "_wait_pull_request",
        lambda _number, _head, _base: {
            "checks": completed_check_evidence(
                _successful_pull_checks(),
                required=REQUIRED_PULL_REQUEST_CHECKS,
            ),
            "controls": {
                "candidate_revision": CANDIDATE_SHA,
                "ruleset_fingerprint": "4" * 64,
            },
            "pull": {},
        },
    )
    monkeypatch.setattr(
        runtime,
        "_final_local_gates",
        lambda _source, _version: {
            "ci": {"all_success": True, "missing_required": []},
            "security": {"exit_code": 0, "report_hash": "5" * 64},
            "tests": {
                "source_revision": CANDIDATE_SHA,
                "passed": True,
                "evidence_hash": "6" * 64,
            },
            "version_free": {"version": "0.7.0", "free": True},
        },
    )
    monkeypatch.setattr(
        "scripts.operations.release_runtime.deployment_state",
        lambda _statefulset: {
            "digest": DIGEST,
            "keel_policy": "never",
            "rollout_complete": True,
        },
    )
    monkeypatch.setattr(runtime, "live_statefulset", lambda: {})
    monkeypatch.setattr(
        runtime,
        "_merge_exact",
        lambda *_arguments: (_ for _ in ()).throw(
            AssertionError("prepare must not merge")
        ),
    )

    result = runtime.prepare(
        {
            "extra_context": "No extra context for this run",
            "run_id": "11111111-2222-4333-8444-555555555555",
            "source_revision": SOURCE_SHA,
        },
        {
            "evidence": {
                "computed_release": {
                    "changes": _roll_forward_changes()
                }
            },
            "outputs": {"candidate": {"version": "0.7.0"}},
        },
    )

    assert result["candidate"] == {
        "source_revision": CANDIDATE_SHA,
        "version": "0.7.0",
    }
    assert result["release_pr"] == {
        "base_source_revision": SOURCE_SHA,
        "candidate_version": "0.7.0",
        "head_revision": CANDIDATE_SHA,
        "head_tree_revision": CANDIDATE_TREE,
        "merged": False,
        "number": 182,
        "source_revision": CANDIDATE_SHA,
        "url": "https://github.com/knaisoma/data-olympus/pull/182",
    }
    assert result["changelog"] == {
        "content_hash": "1" * 64,
        "document_mode": "roll_forward",
        "release_note_hash": "2" * 64,
        "source_revision": CANDIDATE_SHA,
    }
    assert result["review_material"] == {
        "mode": "pull_request_diff",
        "source_revision": CANDIDATE_SHA,
    }
    assert runtime.candidate_revision() == CANDIDATE_SHA
    assert consultations == [
        {
            "agent_identity": "ai-operations-release",
            "intent": (
                "Prepare governed Data Olympus release v0.7.0 from exact "
                f"source {SOURCE_SHA}."
            ),
            "source_session": "11111111-2222-4333-8444-555555555555",
            "trigger": "explicit",
            "workspace": "data-olympus",
        }
    ]
    assert gate_checks == [
        {
            "action_diff": (
                "Prepare governed Data Olympus release v0.7.0 from exact "
                f"source {SOURCE_SHA}."
            ),
            "action_path": "pyproject.toml",
            "session_id": "11111111-2222-4333-8444-555555555555",
            "tool_name": "git commit",
            "workspace": "data-olympus",
        }
    ]


def test_review_material_reads_documents_from_the_bound_revision(
    tmp_path: Path,
) -> None:
    committed_changelog = (
        "# Changelog\n\n## [Unreleased]\n\n"
        "## [0.7.0] - 2026-08-02\n\n### Fixed\n\n"
        "* Committed release evidence.\n\n"
        "## [0.6.0] - 2026-07-18\n\n* Prior release.\n"
    )
    committed_note = (
        "# data-olympus 0.7.0\n\n## Fixed\n\n"
        "* Committed release evidence.\n"
    )
    candidate_diff = "diff --git a/CHANGELOG.md b/CHANGELOG.md\n+committed"
    (tmp_path / "docs" / "releases").mkdir(parents=True)
    (tmp_path / "CHANGELOG.md").write_text(
        committed_changelog.replace("Committed", "Working tree drift"),
        encoding="utf-8",
    )
    (tmp_path / "docs" / "releases" / "v0.7.0.md").write_text(
        committed_note.replace("Committed", "Working tree drift"),
        encoding="utf-8",
    )

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        if command == ["git", "show", f"{CANDIDATE_SHA}:CHANGELOG.md"]:
            return committed_changelog
        if command == [
            "git",
            "show",
            f"{CANDIDATE_SHA}:docs/releases/v0.7.0.md",
        ]:
            return committed_note
        if command[:2] == ["git", "diff"]:
            return candidate_diff
        raise AssertionError(command)

    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
    )

    material = runtime._review_material(
        "0.7.0",
        CANDIDATE_SHA,
        SOURCE_SHA,
    )

    assert material["release_note"] == committed_note
    assert "Committed release evidence" in material["changelog_section"]
    assert "Working tree drift" not in json.dumps(material)
    assert material["candidate_diff"] == candidate_diff


def test_review_material_rejects_an_oversize_candidate_diff(
    tmp_path: Path,
) -> None:
    changelog = (
        "# Changelog\n\n## [Unreleased]\n\n"
        "## [0.7.0] - 2026-08-02\n\n### Fixed\n\n* Evidence.\n"
    )
    note = "# data-olympus 0.7.0\n\n## Fixed\n\n* Evidence.\n"

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        if command == ["git", "show", f"{CANDIDATE_SHA}:CHANGELOG.md"]:
            return changelog
        if command == [
            "git",
            "show",
            f"{CANDIDATE_SHA}:docs/releases/v0.7.0.md",
        ]:
            return note
        if command[:2] == ["git", "diff"]:
            return "x" * (128 * 1024)
        raise AssertionError(command)

    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
    )

    with pytest.raises(ValueError, match="bounded packet limit"):
        runtime._review_material("0.7.0", CANDIDATE_SHA, SOURCE_SHA)


def test_review_material_rejects_an_empty_pull_request_diff(
    tmp_path: Path,
) -> None:
    changelog = (
        "# Changelog\n\n## [Unreleased]\n\n"
        "## [0.7.0] - 2026-08-02\n\n### Fixed\n\n* Evidence.\n"
    )
    note = "# data-olympus 0.7.0\n\n## Fixed\n\n* Evidence.\n"

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        if command == ["git", "show", f"{CANDIDATE_SHA}:CHANGELOG.md"]:
            return changelog
        if command == [
            "git",
            "show",
            f"{CANDIDATE_SHA}:docs/releases/v0.7.0.md",
        ]:
            return note
        if command[:2] == ["git", "diff"]:
            return ""
        raise AssertionError(command)

    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
    )

    with pytest.raises(ValueError, match="release candidate review diff is empty"):
        runtime._review_material("0.7.0", CANDIDATE_SHA, SOURCE_SHA)


def test_prepare_recognizes_exact_prepared_unpublished_main_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_resumable_release_documents(tmp_path)
    commands: list[list[str]] = []
    gateway = StubGateway()

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        commands.append(command)
        if command == ["git", "show", f"{SOURCE_SHA}:CHANGELOG.md"]:
            return (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
        if command == [
            "git",
            "show",
            f"{SOURCE_SHA}:docs/releases/v0.7.0.md",
        ]:
            return (
                tmp_path / "docs" / "releases" / "v0.7.0.md"
            ).read_text(encoding="utf-8")
        return ""

    runtime = ReleaseRuntime(
        tmp_path,
        gateway=gateway,
        command_output=command_output,
        authority_consult=lambda _arguments: {
            "consulted_at": 1.0,
            "ttl_seconds": 300,
        },
        authority_gate_check=lambda arguments: {
            "verdict": "allow",
            "session_id": arguments["session_id"],
            "workspace": "data-olympus",
        },
        clock=lambda: 1.0,
    )
    monkeypatch.setattr(runtime, "source_revision", lambda: SOURCE_SHA)
    monkeypatch.setattr(runtime, "remote_main_revision", lambda: SOURCE_SHA)
    monkeypatch.setattr(runtime, "_tree_revision", lambda _revision: CANDIDATE_TREE)
    monkeypatch.setattr(
        runtime,
        "_final_local_gates",
        lambda source, version: {
            "ci": {"all_success": True, "missing_required": []},
            "security": {"exit_code": 0, "report_hash": "5" * 64},
            "tests": {
                "source_revision": source,
                "passed": True,
                "evidence_hash": "6" * 64,
            },
            "version_free": {"version": version, "free": True},
        },
    )
    monkeypatch.setattr(
        "scripts.operations.release_runtime.deployment_state",
        lambda _statefulset: {
            "digest": DIGEST,
            "keel_policy": "never",
            "rollout_complete": True,
        },
    )
    monkeypatch.setattr(runtime, "live_statefulset", lambda: {})

    result = runtime.prepare(
        {
            "extra_context": "No extra context for this run",
            "run_id": "11111111-2222-4333-8444-555555555555",
            "source_revision": SOURCE_SHA,
        },
        {
            "evidence": {
                "computed_release": {
                    "changes": {
                        "breaking": [],
                        "features": [
                            "feat(automation): make releases outcome based"
                        ],
                        "fixes": ["fix(mcp): preserve exact source evidence"],
                    }
                }
            },
            "outputs": {"candidate": {"version": "0.7.0"}},
        },
    )

    assert result["preparation_mode"] == "prepared_unpublished"
    assert result["candidate"] == {
        "source_revision": SOURCE_SHA,
        "version": "0.7.0",
    }
    assert result["prepared_main"] == {
        "changelog_hash": result["changelog"]["content_hash"],
        "release_date": "2026-08-01",
        "release_note_hash": result["prepared_main"]["release_note_hash"],
        "source_revision": SOURCE_SHA,
        "tree_revision": CANDIDATE_TREE,
        "version": "0.7.0",
    }
    assert "release_pr" not in result
    assert "release_controls" not in result
    material = result["review_material"]
    assert material["mode"] == "prepared_main_documents"
    assert material["source_revision"] == SOURCE_SHA
    assert material["candidate_diff"] == ""
    assert material["release_note_sha256"] == sha256(
        material["release_note"].encode()
    ).hexdigest()
    assert material["changelog_section_sha256"] == sha256(
        material["changelog_section"].encode()
    ).hexdigest()
    assert gateway.calls == []
    assert not any(
        command[:2] in {
            ("git", "switch"),
            ("git", "add"),
            ("git", "commit"),
            ("git", "push"),
        }
        for command in map(tuple, commands)
    )


def test_strict_version_free_requires_all_candidate_channels_absent(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def command_output(command: list[str], _cwd: Path, _timeout: int) -> str:
        commands.append(command)
        return json.dumps(
            {
                "free": True,
                "ghcr_taken": False,
                "github_release_taken": False,
                "pypi_taken": False,
                "unreachable": [],
            }
        )

    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway(),
        command_output=command_output,
    )

    result = runtime._strict_version_free("0.7.0-rc.1")

    assert result["free"] is True
    assert commands == [[
        "uv",
        "run",
        "--python",
        "3.13",
        "python",
        "scripts/version_free.py",
        "--version",
        "0.7.0-rc.1",
        "--json",
    ]]


def test_deliver_merges_only_after_review_and_proves_exact_tree_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = "sha256:" + "e" * 64
    order: list[str] = []
    controls = {
        "candidate_revision": CANDIDATE_SHA,
        "ruleset_fingerprint": "4" * 64,
    }
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway({"list_tags": []}),
        command_output=lambda _command, _cwd, _timeout: "",
    )
    runtime._prepared = {
        "raw": {
            "candidate": {
                "source_revision": CANDIDATE_SHA,
                "version": "0.7.0",
            },
            "release_pr": {
                "base_source_revision": SOURCE_SHA,
                "head_revision": CANDIDATE_SHA,
                "head_tree_revision": CANDIDATE_TREE,
                "number": 182,
            },
            "rollback_point": {"digest": previous},
        },
        "release_controls": controls,
    }
    monkeypatch.setattr(
        runtime,
        "_wait_pull_request",
        lambda _number, _head, _base: (
            order.append("controls")
            or {
                "checks": {},
                "controls": controls,
                "pull": {},
            }
        ),
    )
    remote_revisions = iter([SOURCE_SHA, DELIVERY_SHA])
    monkeypatch.setattr(
        runtime,
        "remote_main_revision",
        lambda: next(remote_revisions),
    )
    monkeypatch.setattr(runtime, "source_revision", lambda: CANDIDATE_SHA)
    monkeypatch.setattr(
        runtime,
        "_tree_revision",
        lambda revision: (
            order.append(f"tree:{revision}") or CANDIDATE_TREE
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_parent_revisions",
        lambda revision: (
            order.append(f"parents:{revision}") or [SOURCE_SHA]
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_merge_exact",
        lambda _number, _version, _head: (
            order.append("merge")
            or {"merged": True, "sha": DELIVERY_SHA}
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_final_local_gates",
        lambda revision, version: (
            order.append("final-gates")
            or {
                "ci": {"all_success": True, "missing_required": []},
                "security": {"exit_code": 0},
                "tests": {"source_revision": revision, "passed": True},
                "version_free": {"version": version, "free": True},
            }
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_workflow_run",
        lambda name, revision, _inputs, **_kwargs: (
            order.append(f"workflow:{name}")
            or {
                "conclusion": "success",
                "run_id": len(order),
                "source_revision": revision,
            }
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_strict_version_free",
        lambda version: (
            order.append(f"candidate-free:{version}")
            or {
                "free": True,
                "ghcr_taken": False,
                "github_release_taken": False,
                "pypi_taken": False,
                "unreachable": [],
            }
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_candidate_publication",
        lambda revision, _version, tag: {
            "candidate_tag": tag,
            "image_digest": DIGEST,
            "source_revision": revision,
        },
    )
    monkeypatch.setattr(
        runtime,
        "_stable_publication",
        lambda revision, version: {
            "pypi_version": version,
            "source_revision": revision,
        },
    )
    monkeypatch.setattr(runtime, "_registry_digest", lambda _tag: DIGEST)
    monkeypatch.setattr(
        runtime,
        "_apply_digest",
        lambda _digest, **_kwargs: {
            "keel_policy": "never",
            "rollout_complete": True,
        },
    )
    monkeypatch.setattr(
        runtime,
        "_service_acceptance",
        lambda: {
            "enforcement": True,
            "healthy": True,
            "mcp_search": True,
            "ready": True,
        },
    )

    result = runtime.deliver(
        {},
        {
            "evidence": {
                "computed_release": {
                    "changes": {
                        "breaking": [],
                        "features": [],
                        "fixes": ["fix(release): resume prepared release"],
                    }
                }
            }
        },
        {},
        {
            "status": "pass",
            "evidence": {
                "reviewer_family": "claude",
                "source_revision": CANDIDATE_SHA,
            },
        },
    )

    assert result["candidate"]["source_revision"] == DELIVERY_SHA
    assert result["delivery_proof"] == {
        "admitted_revision": SOURCE_SHA,
        "approved_candidate_revision": CANDIDATE_SHA,
        "delivery_revision": DELIVERY_SHA,
        "delivery_tree_revision": CANDIDATE_TREE,
        "merge_confirmed": True,
        "reviewed_tree_revision": CANDIDATE_TREE,
        "sole_parent_revision": SOURCE_SHA,
    }
    assert order.index("controls") < order.index("merge")
    assert order.index("merge") < order.index(f"tree:{DELIVERY_SHA}")
    assert order.index(f"tree:{DELIVERY_SHA}") < order.index("final-gates")
    assert order.index("final-gates") < order.index("workflow:rc-publish.yml")


def test_deliver_resumes_exact_prepared_main_and_never_attempts_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = "sha256:" + "e" * 64
    order: list[str] = []
    documents = {
        "changelog_hash": "1" * 64,
        "content_hash": "2" * 64,
        "document_mode": "prepared_unpublished",
        "release_date": "2026-08-02",
        "release_note_hash": "2" * 64,
    }
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway({"list_tags": []}),
        command_output=lambda _command, _cwd, _timeout: "",
    )
    runtime._prepared = {
        "raw": {
            "candidate": {"source_revision": SOURCE_SHA, "version": "0.7.0"},
            "preparation_mode": "prepared_unpublished",
            "prepared_main": {
                "source_revision": SOURCE_SHA,
                "tree_revision": CANDIDATE_TREE,
                "version": "0.7.0",
                "changelog_hash": "1" * 64,
                "release_note_hash": "2" * 64,
                "release_date": "2026-08-02",
            },
            "rollback_point": {"digest": previous},
        },
        "document_evidence": documents,
    }
    monkeypatch.setattr(runtime, "remote_main_revision", lambda: SOURCE_SHA)
    monkeypatch.setattr(runtime, "source_revision", lambda: SOURCE_SHA)
    monkeypatch.setattr(runtime, "_tree_revision", lambda _revision: CANDIDATE_TREE)
    monkeypatch.setattr(
        "scripts.operations.release_runtime.validate_prepared_release_documents",
        lambda *_arguments: documents,
    )
    monkeypatch.setattr(
        runtime,
        "_merge_and_prove_delivery",
        lambda **_arguments: (_ for _ in ()).throw(
            AssertionError("prepared unpublished delivery must not merge")
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_final_local_gates",
        lambda revision, version: (
            order.append("final-gates")
            or {
                "ci": {"all_success": True, "missing_required": []},
                "security": {"exit_code": 0},
                "tests": {"source_revision": revision, "passed": True},
                "version_free": {"version": version, "free": True},
            }
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_strict_version_free",
        lambda version: (
            order.append(f"candidate-free:{version}")
            or {
                "free": True,
                "ghcr_taken": False,
                "github_release_taken": False,
                "pypi_taken": False,
                "unreachable": [],
            }
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_workflow_run",
        lambda name, revision, _inputs, **_kwargs: (
            order.append(f"workflow:{name}")
            or {
                "conclusion": "success",
                "run_id": len(order),
                "source_revision": revision,
            }
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_candidate_publication",
        lambda revision, _version, tag: {
            "candidate_tag": tag,
            "image_digest": DIGEST,
            "source_revision": revision,
        },
    )
    monkeypatch.setattr(
        runtime,
        "_stable_publication",
        lambda revision, version: {
            "pypi_version": version,
            "source_revision": revision,
        },
    )
    monkeypatch.setattr(runtime, "_registry_digest", lambda _tag: DIGEST)
    monkeypatch.setattr(
        runtime,
        "_apply_digest",
        lambda _digest, **_kwargs: {
            "keel_policy": "never",
            "rollout_complete": True,
        },
    )
    monkeypatch.setattr(
        runtime,
        "_service_acceptance",
        lambda: {
            "enforcement": True,
            "healthy": True,
            "mcp_search": True,
            "ready": True,
        },
    )

    result = runtime.deliver(
        {},
        {
            "evidence": {
                "computed_release": {
                    "changes": {
                        "breaking": [],
                        "features": [],
                        "fixes": ["fix(release): resume prepared release"],
                    }
                }
            }
        },
        {},
        {
            "status": "pass",
            "evidence": {
                "reviewer_family": "claude",
                "source_revision": SOURCE_SHA,
            },
        },
    )

    assert result["candidate"]["source_revision"] == SOURCE_SHA
    assert result["delivery_proof"] == {
        "preparation_mode": "prepared_unpublished",
        "admitted_revision": SOURCE_SHA,
        "approved_candidate_revision": SOURCE_SHA,
        "delivery_revision": SOURCE_SHA,
        "delivery_tree_revision": CANDIDATE_TREE,
        "merge_confirmed": False,
        "merge_skipped": True,
        "reviewed_tree_revision": CANDIDATE_TREE,
    }
    assert order.index("final-gates") < order.index("candidate-free:0.7.0-rc.1")
    assert order.index("candidate-free:0.7.0-rc.1") < order.index(
        "workflow:rc-publish.yml"
    )


def test_delivery_blocks_ruleset_drift_without_attempting_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway({"list_tags": []}),
    )
    reviewed = _prime_approved_delivery(
        runtime,
        monkeypatch,
        "sha256:" + "e" * 64,
    )
    changed_controls = {"ruleset_fingerprint": "9" * 64}
    monkeypatch.setattr(
        runtime,
        "_wait_pull_request",
        lambda *_arguments: {
            "checks": {},
            "controls": changed_controls,
            "pull": {},
        },
    )
    merge_attempted = False

    def merge(**_arguments):
        nonlocal merge_attempted
        merge_attempted = True
        return DELIVERY_SHA, {}

    monkeypatch.setattr(runtime, "_merge_and_prove_delivery", merge)

    with pytest.raises(ValueError, match="controls changed"):
        runtime.deliver({}, {}, {}, reviewed)

    assert merge_attempted is False


def test_merge_proof_rejects_a_different_delivery_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=StubGateway(),
        command_output=lambda _command, _cwd, _timeout: "",
    )
    monkeypatch.setattr(
        runtime,
        "_merge_exact",
        lambda *_arguments: {"merged": True, "sha": DELIVERY_SHA},
    )
    monkeypatch.setattr(runtime, "remote_main_revision", lambda: DELIVERY_SHA)
    monkeypatch.setattr(
        runtime,
        "_parent_revisions",
        lambda _revision: [SOURCE_SHA],
    )
    monkeypatch.setattr(runtime, "_tree_revision", lambda _revision: "8" * 40)

    with pytest.raises(ReleaseDeliveryError, match="tree does not match") as raised:
        runtime._merge_and_prove_delivery(
            number=182,
            version="0.7.0",
            head_revision=CANDIDATE_SHA,
            head_tree_revision=CANDIDATE_TREE,
            base_revision=SOURCE_SHA,
        )

    assert raised.value.evidence["merge_confirmed"] is True
    assert raised.value.evidence["merged_revision"] == DELIVERY_SHA


def test_ambiguous_merge_response_reconciles_only_the_same_confirmed_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = StubGateway(
        {
            "pull_request_read": {
                "base": {"sha": SOURCE_SHA},
                "head": {"sha": CANDIDATE_SHA},
                "merge_commit_sha": DELIVERY_SHA,
                "merged": True,
            }
        }
    )
    runtime = ReleaseRuntime(
        tmp_path,
        gateway=gateway,
        command_output=lambda _command, _cwd, _timeout: "",
    )
    monkeypatch.setattr(
        runtime,
        "_merge_exact",
        lambda *_arguments: (_ for _ in ()).throw(ValueError("response lost")),
    )
    monkeypatch.setattr(runtime, "remote_main_revision", lambda: DELIVERY_SHA)
    monkeypatch.setattr(
        runtime,
        "_parent_revisions",
        lambda _revision: [SOURCE_SHA],
    )
    monkeypatch.setattr(runtime, "_tree_revision", lambda _revision: CANDIDATE_TREE)

    revision, proof = runtime._merge_and_prove_delivery(
        number=182,
        version="0.7.0",
        head_revision=CANDIDATE_SHA,
        head_tree_revision=CANDIDATE_TREE,
        base_revision=SOURCE_SHA,
    )

    assert revision == DELIVERY_SHA
    assert proof["delivery_revision"] == DELIVERY_SHA


def _prime_approved_delivery(
    runtime: ReleaseRuntime,
    monkeypatch: pytest.MonkeyPatch,
    previous: str,
) -> dict[str, object]:
    controls = {"ruleset_fingerprint": "4" * 64}
    runtime._prepared = {
        "raw": {
            "candidate": {
                "version": "0.7.0",
                "source_revision": CANDIDATE_SHA,
            },
            "release_pr": {
                "base_source_revision": SOURCE_SHA,
                "head_revision": CANDIDATE_SHA,
                "head_tree_revision": CANDIDATE_TREE,
                "number": 182,
            },
            "rollback_point": {"digest": previous},
        },
        "release_controls": controls,
    }
    monkeypatch.setattr(
        runtime,
        "_wait_pull_request",
        lambda *_arguments: {"checks": {}, "controls": controls, "pull": {}},
    )
    monkeypatch.setattr(runtime, "remote_main_revision", lambda: SOURCE_SHA)
    monkeypatch.setattr(runtime, "source_revision", lambda: CANDIDATE_SHA)
    monkeypatch.setattr(runtime, "command", lambda *_arguments, **_kwargs: "")
    monkeypatch.setattr(runtime, "_tree_revision", lambda _revision: CANDIDATE_TREE)
    proof = {
        "admitted_revision": SOURCE_SHA,
        "approved_candidate_revision": CANDIDATE_SHA,
        "delivery_revision": DELIVERY_SHA,
        "delivery_tree_revision": CANDIDATE_TREE,
        "merge_confirmed": True,
        "reviewed_tree_revision": CANDIDATE_TREE,
        "sole_parent_revision": SOURCE_SHA,
    }
    monkeypatch.setattr(
        runtime,
        "_merge_and_prove_delivery",
        lambda **_arguments: (DELIVERY_SHA, proof),
    )
    monkeypatch.setattr(
        runtime,
        "_final_local_gates",
        lambda _revision, _version: {},
    )
    monkeypatch.setattr(
        runtime,
        "_strict_version_free",
        lambda _version: {
            "free": True,
            "ghcr_taken": False,
            "github_release_taken": False,
            "pypi_taken": False,
            "unreachable": [],
        },
    )
    return {
        "status": "pass",
        "evidence": {
            "reviewer_family": "claude",
            "source_revision": CANDIDATE_SHA,
        },
    }


def test_postmerge_tag_inventory_failure_preserves_delivery_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = "sha256:" + "e" * 64
    runtime = ReleaseRuntime(tmp_path, gateway=StubGateway())
    reviewed = _prime_approved_delivery(runtime, monkeypatch, previous)

    def fail_tags() -> list[object]:
        raise ValueError("FastMCP text content omitted gateway result")

    monkeypatch.setattr(runtime, "_list_tags", fail_tags)

    with pytest.raises(ReleaseDeliveryError, match="omitted gateway result") as raised:
        runtime.deliver({}, {}, {}, reviewed)

    assert raised.value.evidence == {
        "admitted_revision": SOURCE_SHA,
        "approved_candidate_revision": CANDIDATE_SHA,
        "delivery_revision": DELIVERY_SHA,
        "delivery_tree_revision": CANDIDATE_TREE,
        "external_state_changed": True,
        "merge_confirmed": True,
        "release_pr_number": 182,
        "reviewed_tree_revision": CANDIDATE_TREE,
        "rollback_completed": False,
        "sole_parent_revision": SOURCE_SHA,
    }


def test_postmerge_candidate_collision_failure_preserves_delivery_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ReleaseRuntime(tmp_path, gateway=StubGateway({"list_tags": []}))
    reviewed = _prime_approved_delivery(
        runtime,
        monkeypatch,
        "sha256:" + "e" * 64,
    )
    monkeypatch.setattr(
        runtime,
        "_strict_version_free",
        lambda _version: (_ for _ in ()).throw(
            ValueError("candidate version is taken")
        ),
    )

    with pytest.raises(ReleaseDeliveryError, match="candidate version is taken") as raised:
        runtime.deliver({}, {}, {}, reviewed)

    assert raised.value.evidence["delivery_revision"] == DELIVERY_SHA
    assert raised.value.evidence["merge_confirmed"] is True
    assert raised.value.evidence["release_pr_number"] == 182
    assert raised.value.evidence["rollback_completed"] is False


@pytest.mark.parametrize("dispatch_started", [False, True])
def test_delivery_failure_classification_tracks_the_workflow_dispatch_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dispatch_started: bool,
) -> None:
    previous = "sha256:" + "e" * 64
    runtime = ReleaseRuntime(tmp_path, gateway=StubGateway({"list_tags": []}))
    reviewed = _prime_approved_delivery(runtime, monkeypatch, previous)

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
        runtime.deliver({}, {}, {}, reviewed)

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
    reviewed = _prime_approved_delivery(runtime, monkeypatch, previous)
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
    def apply_digest(
        digest: str,
        *,
        on_apply: object = None,
    ) -> dict[str, object]:
        if on_apply is not None:
            assert callable(on_apply)
            on_apply()
        applied.append(digest)
        return {"digest": digest, "keel_policy": "never", "rollout_complete": True}

    monkeypatch.setattr(runtime, "_apply_digest", apply_digest)
    monkeypatch.setattr(runtime, "_service_acceptance", lambda: acceptance)
    monkeypatch.setattr(
        runtime,
        "_stable_publication",
        lambda _source, _version: (_ for _ in ()).throw(ValueError("stable failed")),
    )

    with pytest.raises(ReleaseDeliveryError, match="stable failed") as raised:
        runtime.deliver({}, {}, {}, reviewed)

    assert applied == [DIGEST, previous]
    assert raised.value.external_state_changed is True
    assert raised.value.evidence == {
        "deployment_started": True,
        "external_state_changed": True,
        "rollback_completed": True,
    }


@pytest.mark.parametrize("failure_point", ["preflight", "apply", "acceptance"])
def test_rollback_failure_evidence_tracks_the_digest_apply_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    rollback_digest = "sha256:" + "e" * 64
    runtime = ReleaseRuntime(tmp_path, gateway=StubGateway())

    def apply_digest(
        digest: str,
        *,
        on_apply: object = None,
    ) -> dict[str, object]:
        assert digest == rollback_digest
        if failure_point != "preflight":
            assert callable(on_apply)
            on_apply()
        if failure_point in {"preflight", "apply"}:
            raise ValueError(f"{failure_point} failed")
        return {"digest": digest, "keel_policy": "never", "rollout_complete": True}

    monkeypatch.setattr(runtime, "_apply_digest", apply_digest)
    monkeypatch.setattr(
        runtime,
        "_service_acceptance",
        lambda: (_ for _ in ()).throw(ValueError("acceptance failed")),
    )
    recovery = {
        "source_revision": SOURCE_SHA,
        "outcome_evidence": {
            "digest": DIGEST,
            "rollback_digest": rollback_digest,
        },
    }

    with pytest.raises(ValueError) as raised:
        runtime.rollback(recovery)

    if failure_point == "preflight":
        assert type(raised.value) is ValueError
        assert getattr(raised.value, "external_state_changed", False) is False
        return

    assert isinstance(raised.value, ReleaseDeliveryError)
    assert raised.value.evidence["external_state_changed"] is True
    assert raised.value.evidence["digest_apply_confirmed"] is (
        failure_point == "acceptance"
    )
    assert raised.value.evidence["rollback_completed"] is False
    assert raised.value.evidence["rollback_digest"] == rollback_digest
