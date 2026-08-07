from __future__ import annotations

import json
import subprocess
from hashlib import sha256

import pytest

from scripts.operations import release as release_module
from scripts.operations.release import (
    DEFAULT_EXTRA_CONTEXT,
    evaluate_stage,
    execute_release_run,
    main,
    parse_release_run_input,
)

SOURCE_SHA = "a" * 40
CANDIDATE_SHA = "f" * 40
DELIVERY_SHA = "7" * 40
BRANCH_SHA = "9" * 40
AUTHORITY_SHA = "b" * 40
CONTRACT_SHA = "c" * 64
IMAGE_DIGEST = "sha256:" + "d" * 64
PREVIOUS_DIGEST = "sha256:" + "e" * 64
RELEASE_NOTE = "# data-olympus 0.6.1\n\n## Fixed\n\n* Exact release evidence.\n"
CHANGELOG_SECTION = (
    "## [0.6.1] - 2026-08-02\n\n### Fixed\n\n"
    "* Exact release evidence.\n"
)
CANDIDATE_DIFF = (
    "diff --git a/CHANGELOG.md b/CHANGELOG.md\n"
    "+## [0.6.1] - 2026-08-02\n"
)


def _review_material(
    *,
    mode: str = "pull_request_diff",
    source_revision: str = CANDIDATE_SHA,
) -> dict[str, object]:
    candidate_diff = CANDIDATE_DIFF if mode == "pull_request_diff" else ""
    return {
        "mode": mode,
        "source_revision": source_revision,
        "candidate_diff": candidate_diff,
        "candidate_diff_sha256": sha256(candidate_diff.encode()).hexdigest(),
        "changelog_sha256": "1" * 64,
        "changelog_section": CHANGELOG_SECTION,
        "changelog_section_sha256": sha256(
            CHANGELOG_SECTION.encode()
        ).hexdigest(),
        "release_note": RELEASE_NOTE,
        "release_note_sha256": sha256(RELEASE_NOTE.encode()).hexdigest(),
    }


def _candidate() -> dict[str, object]:
    return {
        "version": "0.6.1",
        "candidate_tag": "0.6.1-rc.1",
        "source_revision": SOURCE_SHA,
        "image_digest": IMAGE_DIGEST,
    }


def _authority_input() -> dict[str, object]:
    return {
        "authority_revision": AUTHORITY_SHA,
        "contract_revision": CONTRACT_SHA,
        "source_revision": SOURCE_SHA,
    }


def _admission_input(*, releasable: bool = True) -> dict[str, object]:
    return {
        "branch": "main",
        "source_revision": SOURCE_SHA,
        "remote_main_revision": SOURCE_SHA,
        "computed_release": {
            "releasable": releasable,
            "bump": "patch" if releasable else "none",
            "current_version": "0.6.0",
            "next_version": "0.6.1" if releasable else "0.6.0",
            "functional_changed": False,
            "changes": {
                "features": [],
                "fixes": (
                    ["fix(ci): allow immutable history reconciliation"] if releasable else []
                ),
                "breaking": [],
            },
        },
    }


def _release_controls(candidate_revision: str) -> dict[str, object]:
    return {
        "candidate_revision": candidate_revision,
        "check_evidence": {"all_success": True, "missing_required": []},
        "codeql_analysis_hash": "4" * 64,
        "codeql_languages": ["actions", "javascript-typescript", "python"],
        "open_codeql_alerts": 0,
        "review_decision": "REVIEW_REQUIRED",
        "ruleset_fingerprint": "5" * 64,
        "ruleset_ids": [18080131],
        "unresolved_review_threads": 0,
    }


def test_authority_binds_governance_contract_and_source_revisions() -> None:
    result = evaluate_stage("authority", _authority_input())

    assert result["status"] == "pass"
    assert result["evidence"] == _authority_input()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authority_revision", "not-a-sha"),
        ("contract_revision", ""),
        ("source_revision", "short"),
    ],
)
def test_authority_fails_closed_on_invalid_revisions(
    field: str,
    value: str,
) -> None:
    input_document = _authority_input()
    input_document[field] = value

    result = evaluate_stage("authority", input_document)

    assert result["status"] == "blocked"
    assert field in result["reason"]


def test_admission_returns_truthful_no_action_only_for_no_release() -> None:
    result = evaluate_stage(
        "admission",
        _admission_input(releasable=False),
    )

    assert result["status"] == "no_action"
    assert result["evidence"]["source_revision"] == SOURCE_SHA
    assert result["evidence"]["remote_main_revision"] == SOURCE_SHA
    assert result["outputs"] == {}


def test_admission_emits_exact_forward_candidate() -> None:
    result = evaluate_stage("admission", _admission_input())

    assert result["status"] == "pass"
    assert result["outputs"]["candidate"] == {
        "version": "0.6.1",
        "source_revision": SOURCE_SHA,
        "bump": "patch",
    }


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda value: value.__setitem__("branch", "feature/not-main"),
            "main",
        ),
        (
            lambda value: value.__setitem__(
                "remote_main_revision",
                "f" * 40,
            ),
            "remote main",
        ),
        (
            lambda value: value["computed_release"].__setitem__(
                "next_version",
                "0.6.0",
            ),
            "v0.6.0",
        ),
        (
            lambda value: value["computed_release"].__setitem__(
                "next_version",
                "0.9.0",
            ),
            "does not match",
        ),
    ],
)
def test_admission_blocks_unmerged_changed_or_v060_candidate(
    mutate,
    reason: str,
) -> None:
    input_document = _admission_input()
    mutate(input_document)

    result = evaluate_stage("admission", input_document)

    assert result["status"] == "blocked"
    assert reason in result["reason"]


def test_prepare_confirms_one_unmerged_release_candidate_and_hashes_extra_context() -> None:
    candidate = {**_candidate(), "source_revision": CANDIDATE_SHA}
    result = evaluate_stage(
        "prepare",
        {
            "candidate": candidate,
            "release_controls": _release_controls(CANDIDATE_SHA),
            "extra_context": DEFAULT_EXTRA_CONTEXT,
            "changelog": {
                "source_revision": CANDIDATE_SHA,
                "content_hash": "1" * 64,
            },
            "security": {"exit_code": 0, "report_hash": "2" * 64},
            "tests": {
                "source_revision": CANDIDATE_SHA,
                "passed": True,
                "evidence_hash": "3" * 64,
            },
            "rollback_point": {
                "image": f"ghcr.io/knaisoma/data-olympus@{PREVIOUS_DIGEST}",
                "digest": PREVIOUS_DIGEST,
                "keel_policy": "never",
            },
            "release_pr": {
                "number": 178,
                "url": "https://github.com/knaisoma/data-olympus/pull/178",
                "merged": False,
                "base_source_revision": SOURCE_SHA,
                "head_revision": CANDIDATE_SHA,
                "head_tree_revision": BRANCH_SHA,
                "source_revision": CANDIDATE_SHA,
                "candidate_version": "0.6.1",
            },
        },
    )

    assert result["status"] == "pass"
    context = result["evidence"]["extra_context"]
    assert context["default"] is True
    assert context["length"] == len(DEFAULT_EXTRA_CONTEXT)
    assert len(context["sha256"]) == 64
    assert DEFAULT_EXTRA_CONTEXT not in json.dumps(result)
    assert result["outputs"]["release_pr_number"] == 178
    assert result["evidence"]["release_pr_head_tree_revision"] == BRANCH_SHA
    assert result["evidence"]["ruleset_fingerprint"] == "5" * 64


def test_prepare_blocks_when_the_release_pr_was_merged_before_review() -> None:
    candidate = {**_candidate(), "source_revision": CANDIDATE_SHA}
    input_document = {
        "candidate": candidate,
        "release_controls": _release_controls(CANDIDATE_SHA),
        "extra_context": DEFAULT_EXTRA_CONTEXT,
        "changelog": {
            "source_revision": CANDIDATE_SHA,
            "content_hash": "1" * 64,
        },
        "security": {"exit_code": 0, "report_hash": "2" * 64},
        "tests": {
            "source_revision": CANDIDATE_SHA,
            "passed": True,
            "evidence_hash": "3" * 64,
        },
        "rollback_point": {
            "image": f"ghcr.io/knaisoma/data-olympus@{PREVIOUS_DIGEST}",
            "digest": PREVIOUS_DIGEST,
            "keel_policy": "never",
        },
        "release_pr": {
            "number": 178,
            "url": "https://github.com/knaisoma/data-olympus/pull/178",
            "merged": True,
            "base_source_revision": SOURCE_SHA,
            "head_revision": CANDIDATE_SHA,
            "head_tree_revision": BRANCH_SHA,
            "source_revision": CANDIDATE_SHA,
            "candidate_version": "0.6.1",
        },
    }

    result = evaluate_stage("prepare", input_document)

    assert result["status"] == "blocked"
    assert "must remain unmerged" in result["reason"]


def _prepared_unpublished_input() -> dict[str, object]:
    return {
        "candidate": {"version": "0.6.1", "source_revision": SOURCE_SHA},
        "preparation_mode": "prepared_unpublished",
        "review_material": _review_material(
            mode="prepared_main_documents",
            source_revision=SOURCE_SHA,
        ),
        "extra_context": DEFAULT_EXTRA_CONTEXT,
        "prepared_main": {
            "source_revision": SOURCE_SHA,
            "tree_revision": BRANCH_SHA,
            "version": "0.6.1",
            "changelog_hash": "1" * 64,
            "release_note_hash": sha256(RELEASE_NOTE.encode()).hexdigest(),
            "release_date": "2026-08-02",
        },
        "changelog": {
            "source_revision": SOURCE_SHA,
            "content_hash": "1" * 64,
            "document_mode": "prepared_unpublished",
            "release_note_hash": sha256(RELEASE_NOTE.encode()).hexdigest(),
        },
        "security": {"exit_code": 0, "report_hash": "3" * 64},
        "tests": {
            "source_revision": SOURCE_SHA,
            "passed": True,
            "evidence_hash": "4" * 64,
        },
        "rollback_point": {
            "image": f"ghcr.io/knaisoma/data-olympus@{PREVIOUS_DIGEST}",
            "digest": PREVIOUS_DIGEST,
            "keel_policy": "never",
        },
    }


def test_prepare_accepts_only_exact_prepared_unpublished_main_without_a_pr() -> None:
    result = evaluate_stage("prepare", _prepared_unpublished_input())

    assert result["status"] == "pass"
    assert result["evidence"]["preparation_mode"] == "prepared_unpublished"
    assert result["evidence"]["prepared_tree_revision"] == BRANCH_SHA
    assert result["outputs"] == {
        "preparation_reference": f"origin/main@{SOURCE_SHA}"
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("release_pr", {"number": 178}),
        lambda value: value["prepared_main"].__setitem__(
            "source_revision", CANDIDATE_SHA
        ),
        lambda value: value["changelog"].__setitem__("document_mode", "normal"),
    ],
)
def test_prepare_rejects_ambiguous_prepared_unpublished_evidence(mutate) -> None:
    input_document = _prepared_unpublished_input()
    mutate(input_document)

    result = evaluate_stage("prepare", input_document)

    assert result["status"] == "blocked"


def test_validate_requires_unchanged_candidate_and_all_exact_gates() -> None:
    result = evaluate_stage(
        "validate",
        {
            "candidate": _candidate(),
            "current_source_revision": SOURCE_SHA,
            "ci": {
                "source_revision": SOURCE_SHA,
                "all_success": True,
                "missing_required": [],
            },
            "security": {"exit_code": 0},
            "version_free": {"version": "0.6.1", "free": True},
            "tests": {
                "source_revision": SOURCE_SHA,
                "passed": True,
            },
        },
    )

    assert result["status"] == "pass"
    assert result["evidence"]["source_revision"] == SOURCE_SHA


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__(
            "current_source_revision",
            "f" * 40,
        ),
        lambda value: value["ci"].__setitem__("all_success", False),
        lambda value: value["security"].__setitem__("exit_code", 5),
        lambda value: value["version_free"].__setitem__("free", False),
        lambda value: value["tests"].__setitem__("passed", False),
    ],
)
def test_validate_fails_closed_when_any_gate_changes(mutate) -> None:
    input_document = {
        "candidate": _candidate(),
        "current_source_revision": SOURCE_SHA,
        "ci": {
            "source_revision": SOURCE_SHA,
            "all_success": True,
            "missing_required": [],
        },
        "security": {"exit_code": 0},
        "version_free": {"version": "0.6.1", "free": True},
        "tests": {"source_revision": SOURCE_SHA, "passed": True},
    }
    mutate(input_document)

    result = evaluate_stage("validate", input_document)

    assert result["status"] == "blocked"


def test_review_requires_independent_claude_and_exact_ledger_approval() -> None:
    result = evaluate_stage(
        "review",
        {
            "candidate": _candidate(),
            "current_source_revision": SOURCE_SHA,
            "contract_revision": CONTRACT_SHA,
            "run_id": "11111111-2222-4333-8444-555555555555",
            "executor": {"family": "deterministic", "source_revision": SOURCE_SHA},
            "companion_review": {
                "family": "claude",
                "verdict": "APPROVE",
                "reviewed_source_revision": SOURCE_SHA,
                "evidence_hash": "5" * 64,
            },
            "candidate_approval": {
                "authority": "standing-delegation",
                "candidate_revision": SOURCE_SHA,
                "contract_digest": CONTRACT_SHA,
                "approval_id_hash": "6" * 64,
                "review_model_use_id": 71,
                "run_id": "11111111-2222-4333-8444-555555555555",
            },
            "review_model_use_id": 71,
        },
    )

    assert result["status"] == "pass"
    assert result["evidence"]["reviewer_family"] == "claude"
    assert result["evidence"]["executor_family"] == "deterministic"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["companion_review"].__setitem__("family", "local"),
        lambda value: value["companion_review"].__setitem__("family", "codex"),
        lambda value: value["companion_review"].__setitem__(
            "verdict",
            "BLOCK",
        ),
        lambda value: value["companion_review"].__setitem__(
            "reviewed_source_revision",
            "f" * 40,
        ),
        lambda value: value["candidate_approval"].__setitem__(
            "authority",
            "forged",
        ),
        lambda value: value["candidate_approval"].__setitem__(
            "candidate_revision",
            "f" * 40,
        ),
    ],
)
def test_review_blocks_self_review_changed_sha_or_missing_approval(
    mutate,
) -> None:
    input_document = {
        "candidate": _candidate(),
        "current_source_revision": SOURCE_SHA,
        "contract_revision": CONTRACT_SHA,
        "run_id": "11111111-2222-4333-8444-555555555555",
        "executor": {"family": "deterministic", "source_revision": SOURCE_SHA},
        "companion_review": {
            "family": "claude",
            "verdict": "APPROVE",
            "reviewed_source_revision": SOURCE_SHA,
            "evidence_hash": "5" * 64,
        },
        "candidate_approval": {
            "authority": "standing-delegation",
            "candidate_revision": SOURCE_SHA,
            "contract_digest": CONTRACT_SHA,
            "approval_id_hash": "6" * 64,
            "review_model_use_id": 71,
            "run_id": "11111111-2222-4333-8444-555555555555",
        },
        "review_model_use_id": 71,
    }
    mutate(input_document)

    result = evaluate_stage("review", input_document)

    assert result["status"] == "blocked"


def test_deliver_accepts_only_existing_workflows_bound_to_candidate() -> None:
    result = evaluate_stage(
        "deliver",
        {
            "candidate": _candidate(),
            "current_source_revision": SOURCE_SHA,
            "delivery_proof": {
                "admitted_revision": "8" * 40,
                "approved_candidate_revision": CANDIDATE_SHA,
                "delivery_revision": SOURCE_SHA,
                "delivery_tree_revision": BRANCH_SHA,
                "merge_confirmed": True,
                "reviewed_tree_revision": BRANCH_SHA,
                "sole_parent_revision": "8" * 40,
            },
            "workflows": [
                {
                    "name": "rc-publish.yml",
                    "conclusion": "success",
                    "source_revision": SOURCE_SHA,
                    "candidate_tag": "0.6.1-rc.1",
                },
                {
                    "name": "tag-release.yml",
                    "conclusion": "success",
                    "source_revision": SOURCE_SHA,
                    "candidate_tag": "0.6.1-rc.1",
                },
                {
                    "name": "set-channel.yml",
                    "conclusion": "success",
                    "source_revision": SOURCE_SHA,
                    "source_tag": "v0.6.1",
                },
            ],
            "canary": {
                "candidate_tag": "0.6.1-rc.1",
                "source_revision": SOURCE_SHA,
                "digest": IMAGE_DIGEST,
                "keel_policy": "never",
                "rollout_complete": True,
                "healthy": True,
                "ready": True,
                "mcp_search": True,
                "enforcement": True,
            },
            "deployment": {
                "keel_policy": "never",
                "image_digest": IMAGE_DIGEST,
                "source_revision": SOURCE_SHA,
                "rollout_complete": True,
            },
        },
    )

    assert result["status"] == "pass"
    assert result["outputs"]["published_version"] == "0.6.1"


def test_deliver_accepts_reviewed_prepared_unpublished_source_without_merge() -> None:
    input_document = {
        "candidate": _candidate(),
        "current_source_revision": SOURCE_SHA,
        "delivery_proof": {
            "preparation_mode": "prepared_unpublished",
            "admitted_revision": SOURCE_SHA,
            "approved_candidate_revision": SOURCE_SHA,
            "delivery_revision": SOURCE_SHA,
            "delivery_tree_revision": BRANCH_SHA,
            "merge_confirmed": False,
            "merge_skipped": True,
            "reviewed_tree_revision": BRANCH_SHA,
        },
        "workflows": [
            {
                "name": "rc-publish.yml",
                "conclusion": "success",
                "source_revision": SOURCE_SHA,
                "candidate_tag": "0.6.1-rc.1",
            },
            {
                "name": "tag-release.yml",
                "conclusion": "success",
                "source_revision": SOURCE_SHA,
                "candidate_tag": "0.6.1-rc.1",
            },
            {
                "name": "set-channel.yml",
                "conclusion": "success",
                "source_revision": SOURCE_SHA,
                "source_tag": "v0.6.1",
            },
        ],
        "canary": {
            "candidate_tag": "0.6.1-rc.1",
            "source_revision": SOURCE_SHA,
            "digest": IMAGE_DIGEST,
            "keel_policy": "never",
            "rollout_complete": True,
            "healthy": True,
            "ready": True,
            "mcp_search": True,
            "enforcement": True,
        },
        "deployment": {
            "keel_policy": "never",
            "image_digest": IMAGE_DIGEST,
            "source_revision": SOURCE_SHA,
            "rollout_complete": True,
        },
    }

    result = evaluate_stage("deliver", input_document)

    assert result["status"] == "pass"
    assert result["evidence"]["preparation_mode"] == "prepared_unpublished"

    input_document["delivery_proof"]["merge_confirmed"] = True
    assert evaluate_stage("deliver", input_document)["status"] == "failed"


def test_deliver_fails_stable_promotion_without_verified_canary() -> None:
    input_document = {
        "candidate": _candidate(),
        "current_source_revision": SOURCE_SHA,
        "delivery_proof": {
            "admitted_revision": "8" * 40,
            "approved_candidate_revision": CANDIDATE_SHA,
            "delivery_revision": SOURCE_SHA,
            "delivery_tree_revision": BRANCH_SHA,
            "merge_confirmed": True,
            "reviewed_tree_revision": BRANCH_SHA,
            "sole_parent_revision": "8" * 40,
        },
        "workflows": [
            {
                "name": "rc-publish.yml",
                "conclusion": "success",
                "source_revision": SOURCE_SHA,
                "candidate_tag": "0.6.1-rc.1",
            },
            {
                "name": "tag-release.yml",
                "conclusion": "success",
                "source_revision": SOURCE_SHA,
                "candidate_tag": "0.6.1-rc.1",
            },
            {
                "name": "set-channel.yml",
                "conclusion": "success",
                "source_revision": SOURCE_SHA,
                "source_tag": "v0.6.1",
            },
        ],
        "canary": {
            "candidate_tag": "0.6.1-rc.1",
            "source_revision": SOURCE_SHA,
            "digest": IMAGE_DIGEST,
            "keel_policy": "never",
            "rollout_complete": True,
            "healthy": False,
            "ready": True,
            "mcp_search": True,
            "enforcement": True,
        },
        "deployment": {
            "keel_policy": "never",
            "image_digest": IMAGE_DIGEST,
            "source_revision": SOURCE_SHA,
            "rollout_complete": True,
        },
    }

    result = evaluate_stage("deliver", input_document)

    assert result["status"] == "failed"
    assert "canary.healthy" in result["reason"]


def test_verify_requires_every_surface_and_live_service_to_match() -> None:
    result = evaluate_stage(
        "verify",
        {
            "candidate": _candidate(),
            "github_release": {
                "tag": "v0.6.1",
                "source_revision": SOURCE_SHA,
            },
            "pypi": {
                "version": "0.6.1",
                "provenance_source_revision": SOURCE_SHA,
            },
            "image": {"tag": "v0.6.1", "digest": IMAGE_DIGEST},
            "deployment": {
                "source_revision": SOURCE_SHA,
                "digest": IMAGE_DIGEST,
                "healthy": True,
                "ready": True,
                "mcp_search": True,
                "enforcement": True,
                "documentation": True,
            },
        },
    )

    assert result["status"] == "pass"
    assert result["evidence"]["digest"] == IMAGE_DIGEST


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["github_release"].__setitem__(
            "source_revision",
            "f" * 40,
        ),
        lambda value: value["pypi"].__setitem__("version", "0.6.2"),
        lambda value: value["image"].__setitem__(
            "digest",
            PREVIOUS_DIGEST,
        ),
        lambda value: value["deployment"].__setitem__("healthy", False),
        lambda value: value["deployment"].__setitem__("ready", False),
        lambda value: value["deployment"].__setitem__("mcp_search", False),
        lambda value: value["deployment"].__setitem__("enforcement", False),
        lambda value: value["deployment"].__setitem__(
            "documentation",
            False,
        ),
    ],
)
def test_verify_fails_closed_when_any_external_surface_differs(mutate) -> None:
    input_document = {
        "candidate": _candidate(),
        "github_release": {
            "tag": "v0.6.1",
            "source_revision": SOURCE_SHA,
        },
        "pypi": {
            "version": "0.6.1",
            "provenance_source_revision": SOURCE_SHA,
        },
        "image": {"tag": "v0.6.1", "digest": IMAGE_DIGEST},
        "deployment": {
            "source_revision": SOURCE_SHA,
            "digest": IMAGE_DIGEST,
            "healthy": True,
            "ready": True,
            "mcp_search": True,
            "enforcement": True,
            "documentation": True,
        },
    }
    mutate(input_document)

    result = evaluate_stage("verify", input_document)

    assert result["status"] == "failed"


def test_notify_requires_exact_independent_readback() -> None:
    result = evaluate_stage(
        "notify",
        {
            "destination": "data-olympus-operations",
            "readback_destination": "data-olympus-operations",
            "sent_message_id": "120",
            "readback_message_id": "120",
            "sent_content_hash": "6" * 64,
            "readback_content_hash": "6" * 64,
        },
    )

    assert result["status"] == "pass"
    assert result["outputs"]["message_id"] == "120"


def test_notify_fails_when_readback_content_differs() -> None:
    result = evaluate_stage(
        "notify",
        {
            "destination": "data-olympus-operations",
            "readback_destination": "data-olympus-operations",
            "sent_message_id": "120",
            "readback_message_id": "120",
            "sent_content_hash": "6" * 64,
            "readback_content_hash": "7" * 64,
        },
    )

    assert result["status"] == "failed"
    assert "content does not match" in result["reason"]


def test_rollback_requires_direct_digest_restore_while_keel_stays_paused() -> None:
    result = evaluate_stage(
        "rollback",
        {
            "failed_digest": IMAGE_DIGEST,
            "rollback_point": {
                "image": f"ghcr.io/knaisoma/data-olympus@{PREVIOUS_DIGEST}",
                "digest": PREVIOUS_DIGEST,
                "intended_keel_policy": "never",
            },
            "events": [
                {
                    "name": "keel-paused",
                    "policy": "never",
                },
                {
                    "name": "digest-restored",
                    "digest": PREVIOUS_DIGEST,
                    "containers": [
                        "data-olympus-mcp",
                        "prepare-git",
                    ],
                },
                {
                    "name": "deployment-verified",
                    "digest": PREVIOUS_DIGEST,
                    "healthy": True,
                    "ready": True,
                },
                {
                    "name": "keel-policy-restored",
                    "policy": "never",
                },
            ],
        },
    )

    assert result["status"] == "pass"
    assert result["evidence"]["restored_digest"] == PREVIOUS_DIGEST


def test_rollback_fails_when_events_are_out_of_order() -> None:
    result = evaluate_stage(
        "rollback",
        {
            "failed_digest": IMAGE_DIGEST,
            "rollback_point": {
                "image": f"ghcr.io/knaisoma/data-olympus@{PREVIOUS_DIGEST}",
                "digest": PREVIOUS_DIGEST,
                "intended_keel_policy": "never",
            },
            "events": [
                {
                    "name": "digest-restored",
                    "digest": PREVIOUS_DIGEST,
                    "containers": [
                        "data-olympus-mcp",
                        "prepare-git",
                    ],
                },
                {"name": "keel-paused", "policy": "never"},
                {
                    "name": "deployment-verified",
                    "digest": PREVIOUS_DIGEST,
                    "healthy": True,
                    "ready": True,
                },
                {
                    "name": "keel-policy-restored",
                    "policy": "never",
                },
            ],
        },
    )

    assert result["status"] == "failed"
    assert "out of order" in result["reason"]


def test_rollback_fails_when_rollback_digest_is_the_failed_digest() -> None:
    result = evaluate_stage(
        "rollback",
        {
            "failed_digest": IMAGE_DIGEST,
            "rollback_point": {
                "image": f"ghcr.io/knaisoma/data-olympus@{IMAGE_DIGEST}",
                "digest": IMAGE_DIGEST,
                "intended_keel_policy": "never",
            },
            "events": [],
        },
    )

    assert result["status"] == "failed"
    assert "must differ" in result["reason"]


def test_cli_emits_one_failed_json_object_when_input_is_missing(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("AI_OPERATIONS_RUN_INPUT", raising=False)

    exit_code = main([], dependencies={})
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert output == {
        "status": "failed",
        "reason": "AI_OPERATIONS_RUN_INPUT is required",
        "evidence": {},
        "outputs": {},
    }


def test_cli_rejects_retired_stage_invocation(capsys) -> None:
    exit_code = main(["authority"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert output["status"] == "failed"
    assert output["reason"] == "unsupported Data Olympus release operation"


def test_cli_rejects_malformed_json(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AI_OPERATIONS_RUN_INPUT", "{")

    exit_code = main([], dependencies={})
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["status"] == "failed"
    assert output["reason"] == "AI_OPERATIONS_RUN_INPUT must be valid JSON"


def test_cli_rejects_unknown_operation(capsys) -> None:
    exit_code = main(["not-a-stage"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert output["status"] == "failed"
    assert output["reason"] == "unsupported Data Olympus release operation"


RUN_INPUT = {
    "authority_revision": AUTHORITY_SHA,
    "contract_id": "data-olympus-release",
    "contract_revision": CONTRACT_SHA,
    "executor_capability": "deterministic-release-execution",
    "extra_context": DEFAULT_EXTRA_CONTEXT,
    "model_request": {
        "command": [
            "/usr/bin/python3",
            "-m",
            "ai_operations",
            "models",
            "request",
        ],
        "control_environment": "AI_OPERATIONS_RUN_CONTROL",
        "ticket_environment": "AI_OPERATIONS_MODEL_TICKET",
    },
    "notification": {
        "confirmation": "send_and_readback",
        "destination": "data-olympus-operations",
        "transport": "telegram",
    },
    "outcome": (
        "One immutable reviewed release is published and every channel and "
        "deployment matches its source revision."
    ),
    "project": "data-olympus",
    "reviewer_capability": "high-risk-review",
    "run_id": "11111111-2222-4333-8444-555555555555",
    "source_revision": SOURCE_SHA,
}


def _approved_review(request: dict[str, object]) -> dict[str, object]:
    assert request["purpose"] == "review"
    ticket = request["ticket"]
    assert isinstance(ticket, dict)
    return {
        "model_use_id": ticket["model_use_id"],
        "reason": "candidate approved",
        "route_id": ticket["route_id"],
        "status": "approved",
        "verdict": "APPROVE",
    }


def _release_dependencies(*, source_revision: str = SOURCE_SHA) -> dict[str, object]:
    state = {"head": source_revision}
    prepared_candidate = {
        "version": "0.6.1",
        "source_revision": CANDIDATE_SHA,
    }
    artifact_candidate = {
        "version": "0.6.1",
        "source_revision": DELIVERY_SHA,
        "candidate_tag": "0.6.1-rc.1",
        "image_digest": IMAGE_DIGEST,
    }

    def prepare(run, _admitted):
        state["head"] = CANDIDATE_SHA
        return {
            "candidate": prepared_candidate,
            "review_material": _review_material(),
            "release_controls": _release_controls(CANDIDATE_SHA),
            "extra_context": run["extra_context"],
            "changelog": {
                "source_revision": CANDIDATE_SHA,
                "content_hash": "1" * 64,
                "release_note_hash": sha256(RELEASE_NOTE.encode()).hexdigest(),
            },
            "security": {"exit_code": 0, "report_hash": "2" * 64},
            "tests": {
                "source_revision": CANDIDATE_SHA,
                "passed": True,
                "evidence_hash": "3" * 64,
            },
            "rollback_point": {
                "image": f"ghcr.io/knaisoma/data-olympus@{PREVIOUS_DIGEST}",
                "digest": PREVIOUS_DIGEST,
                "keel_policy": "never",
            },
            "release_pr": {
                "number": 178,
                "url": "https://github.com/knaisoma/data-olympus/pull/178",
                "merged": False,
                "base_source_revision": SOURCE_SHA,
                "head_revision": CANDIDATE_SHA,
                "head_tree_revision": BRANCH_SHA,
                "source_revision": CANDIDATE_SHA,
                "candidate_version": "0.6.1",
            },
        }

    return {
        "source_revision": lambda: source_revision,
        "candidate_revision": lambda: state["head"],
        "collect_admission": lambda _run: _admission_input(),
        "prepare": prepare,
        "validate": lambda _run, _admitted, _prepared: {
            "candidate": prepared_candidate,
            "current_source_revision": CANDIDATE_SHA,
            "ci": {
                "source_revision": CANDIDATE_SHA,
                "all_success": True,
                "missing_required": [],
            },
            "security": {"exit_code": 0},
            "version_free": {"version": "0.6.1", "free": True},
            "tests": {"source_revision": CANDIDATE_SHA, "passed": True},
        },
        "request_model_ticket": lambda request: {
            "model_use_id": 71,
            "purpose": request["purpose"],
            "route_id": "claude-code",
            "source_revision": request["source_revision"],
            "ticket": "opaque-run-scoped-ticket",
        },
        "invoke_model": _approved_review,
        "collect_candidate_approval": lambda run, _candidate, review: {
            "approval_id_hash": "6" * 64,
            "authority": "standing-delegation",
            "candidate_revision": CANDIDATE_SHA,
            "contract_digest": run["contract_revision"],
            "review_model_use_id": review["model_use_id"],
            "run_id": run["run_id"],
        },
        "deliver": lambda _run, _admitted, _prepared, _review: {
            "candidate": artifact_candidate,
            "current_source_revision": DELIVERY_SHA,
            "delivery_proof": {
                "admitted_revision": SOURCE_SHA,
                "approved_candidate_revision": CANDIDATE_SHA,
                "delivery_revision": DELIVERY_SHA,
                "delivery_tree_revision": BRANCH_SHA,
                "merge_confirmed": True,
                "reviewed_tree_revision": BRANCH_SHA,
                "sole_parent_revision": SOURCE_SHA,
            },
            "workflows": [
                {
                    "name": "rc-publish.yml",
                    "conclusion": "success",
                    "source_revision": DELIVERY_SHA,
                    "candidate_tag": "0.6.1-rc.1",
                },
                {
                    "name": "tag-release.yml",
                    "conclusion": "success",
                    "source_revision": DELIVERY_SHA,
                    "candidate_tag": "0.6.1-rc.1",
                },
                {
                    "name": "set-channel.yml",
                    "conclusion": "success",
                    "source_revision": DELIVERY_SHA,
                    "source_tag": "v0.6.1",
                },
            ],
            "canary": {
                "candidate_tag": "0.6.1-rc.1",
                "source_revision": DELIVERY_SHA,
                "digest": IMAGE_DIGEST,
                "keel_policy": "never",
                "rollout_complete": True,
                "healthy": True,
                "ready": True,
                "mcp_search": True,
                "enforcement": True,
            },
            "deployment": {
                "keel_policy": "never",
                "image_digest": IMAGE_DIGEST,
                "source_revision": DELIVERY_SHA,
                "rollout_complete": True,
            },
        },
        "verify": lambda _run, _admitted, _delivery: {
            "candidate": artifact_candidate,
            "github_release": {
                "tag": "v0.6.1",
                "source_revision": DELIVERY_SHA,
            },
            "pypi": {
                "version": "0.6.1",
                "provenance_source_revision": DELIVERY_SHA,
            },
            "image": {"tag": "v0.6.1", "digest": IMAGE_DIGEST},
            "deployment": {
                "source_revision": DELIVERY_SHA,
                "digest": IMAGE_DIGEST,
                "healthy": True,
                "ready": True,
                "mcp_search": True,
                "enforcement": True,
                "documentation": True,
            },
        },
    }


def test_run_input_matches_the_current_runner_boundary() -> None:
    parsed = parse_release_run_input(json.dumps(RUN_INPUT))

    assert parsed["model_request"]["ticket_environment"] == (
        "AI_OPERATIONS_MODEL_TICKET"
    )
    forged = dict(RUN_INPUT)
    forged["model_tickets"] = []

    with pytest.raises(ValueError, match="unknown run input field: model_tickets"):
        parse_release_run_input(json.dumps(forged))


def test_one_release_command_emits_the_exact_runner_protocol() -> None:
    events = execute_release_run(json.dumps(RUN_INPUT), _release_dependencies())

    assert [event["name"] for event in events[:-1]] == [
        "admission",
        "prepare",
        "validate",
        "domain_review",
        "deliver",
        "verify",
    ]
    assert events[-1] == {
        "type": "result",
        "sequence": 7,
        "status": "delivered",
        "reason": "release 0.6.1 and kn dev match the approved release content",
        "evidence": {
            "admitted_revision": SOURCE_SHA,
            "approved_candidate_revision": CANDIDATE_SHA,
            "delivery_revision": DELIVERY_SHA,
            "delivery_tree_revision": BRANCH_SHA,
            "published_version": "0.6.1",
            "source_revision": DELIVERY_SHA,
            "version": "0.6.1",
            "digest": IMAGE_DIGEST,
            "health": "verified",
                "mcp": "verified",
                "documentation": "verified",
                "rollback_digest": PREVIOUS_DIGEST,
                "release_pr_number": 178,
            },
        "source_revision": SOURCE_SHA,
        "candidate_revision": CANDIDATE_SHA,
        "review_model_use_id": 71,
    }


def test_release_command_reviews_and_delivers_prepared_unpublished_main() -> None:
    dependencies = _release_dependencies()
    captured: dict[str, object] = {}
    artifact_candidate = _candidate()
    prepared = _prepared_unpublished_input()

    dependencies.update(
        {
            "candidate_revision": lambda: SOURCE_SHA,
            "prepare": lambda _run, _admitted: prepared,
            "validate": lambda _run, _admitted, _prepared: {
                "candidate": prepared["candidate"],
                "current_source_revision": SOURCE_SHA,
                "ci": {
                    "source_revision": SOURCE_SHA,
                    "all_success": True,
                    "missing_required": [],
                },
                "security": {"exit_code": 0},
                "version_free": {"version": "0.6.1", "free": True},
                "tests": {"source_revision": SOURCE_SHA, "passed": True},
            },
            "invoke_model": lambda request: (
                captured.__setitem__("packet", request["packet"])
                or _approved_review(request)
            ),
            "collect_candidate_approval": lambda run, _candidate, review: {
                "approval_id_hash": "6" * 64,
                "authority": "standing-delegation",
                "candidate_revision": SOURCE_SHA,
                "contract_digest": run["contract_revision"],
                "review_model_use_id": review["model_use_id"],
                "run_id": run["run_id"],
            },
            "deliver": lambda _run, _admitted, _prepared, _review: {
                "candidate": artifact_candidate,
                "current_source_revision": SOURCE_SHA,
                "delivery_proof": {
                    "preparation_mode": "prepared_unpublished",
                    "admitted_revision": SOURCE_SHA,
                    "approved_candidate_revision": SOURCE_SHA,
                    "delivery_revision": SOURCE_SHA,
                    "delivery_tree_revision": BRANCH_SHA,
                    "merge_confirmed": False,
                    "merge_skipped": True,
                    "reviewed_tree_revision": BRANCH_SHA,
                },
                "workflows": [
                    {
                        "name": "rc-publish.yml",
                        "conclusion": "success",
                        "source_revision": SOURCE_SHA,
                        "candidate_tag": "0.6.1-rc.1",
                    },
                    {
                        "name": "tag-release.yml",
                        "conclusion": "success",
                        "source_revision": SOURCE_SHA,
                        "candidate_tag": "0.6.1-rc.1",
                    },
                    {
                        "name": "set-channel.yml",
                        "conclusion": "success",
                        "source_revision": SOURCE_SHA,
                        "source_tag": "v0.6.1",
                    },
                ],
                "canary": {
                    "candidate_tag": "0.6.1-rc.1",
                    "source_revision": SOURCE_SHA,
                    "digest": IMAGE_DIGEST,
                    "keel_policy": "never",
                    "rollout_complete": True,
                    "healthy": True,
                    "ready": True,
                    "mcp_search": True,
                    "enforcement": True,
                },
                "deployment": {
                    "keel_policy": "never",
                    "image_digest": IMAGE_DIGEST,
                    "source_revision": SOURCE_SHA,
                    "rollout_complete": True,
                },
            },
            "verify": lambda _run, _admitted, _delivery: {
                "candidate": artifact_candidate,
                "github_release": {
                    "tag": "v0.6.1",
                    "source_revision": SOURCE_SHA,
                },
                "pypi": {
                    "version": "0.6.1",
                    "provenance_source_revision": SOURCE_SHA,
                },
                "image": {"tag": "v0.6.1", "digest": IMAGE_DIGEST},
                "deployment": {
                    "source_revision": SOURCE_SHA,
                    "digest": IMAGE_DIGEST,
                    "healthy": True,
                    "ready": True,
                    "mcp_search": True,
                    "enforcement": True,
                    "documentation": True,
                },
            },
        }
    )

    events = execute_release_run(json.dumps(RUN_INPUT), dependencies)

    assert events[-1]["status"] == "delivered", events[-1]
    assert events[-1]["candidate_revision"] is None
    assert events[-1]["evidence"]["preparation_reference"] == (
        f"origin/main@{SOURCE_SHA}"
    )
    assert events[-1]["evidence"]["approved_candidate_revision"] == SOURCE_SHA
    assert events[-1]["evidence"]["delivery_revision"] == SOURCE_SHA
    assert "release_pr_number" not in events[-1]["evidence"]
    packet = captured["packet"]
    assert isinstance(packet, dict)
    assert packet["source_revision"] == SOURCE_SHA
    assert packet["candidate"]["source_revision"] == SOURCE_SHA
    assert packet["preparation_controls"] == prepared["prepared_main"]


def test_prepared_unpublished_review_failure_keeps_central_candidate_null() -> None:
    dependencies = _release_dependencies()
    captured: dict[str, object] = {}
    prepared = _prepared_unpublished_input()

    def fail_review(request: dict[str, object]) -> dict[str, object]:
        captured["packet"] = request["packet"]
        raise ValueError("review model invocation failed")

    dependencies.update(
        {
            "candidate_revision": lambda: SOURCE_SHA,
            "prepare": lambda _run, _admitted: prepared,
            "validate": lambda _run, _admitted, _prepared: {
                "candidate": prepared["candidate"],
                "current_source_revision": SOURCE_SHA,
                "ci": {
                    "source_revision": SOURCE_SHA,
                    "all_success": True,
                    "missing_required": [],
                },
                "security": {"exit_code": 0},
                "version_free": {"version": "0.6.1", "free": True},
                "tests": {"source_revision": SOURCE_SHA, "passed": True},
            },
            "invoke_model": fail_review,
        }
    )

    events = execute_release_run(json.dumps(RUN_INPUT), dependencies)

    assert [event.get("name") for event in events[:-1]] == [
        "admission",
        "prepare",
        "validate",
    ]
    assert events[-1]["status"] == "blocked"
    assert events[-1]["reason"] == "review model invocation failed"
    assert events[-1]["source_revision"] == SOURCE_SHA
    assert events[-1]["candidate_revision"] is None
    packet = captured["packet"]
    assert isinstance(packet, dict)
    assert packet["source_revision"] == SOURCE_SHA
    assert packet["candidate"]["source_revision"] == SOURCE_SHA


def test_review_evidence_hash_binds_the_packet_and_claude_response() -> None:
    dependencies = _release_dependencies()
    captured: dict[str, object] = {}

    def invoke_model(request: dict[str, object]) -> dict[str, object]:
        response = _approved_review(request)
        captured["packet"] = request["packet"]
        captured["response"] = response
        return response

    dependencies["invoke_model"] = invoke_model

    events = execute_release_run(json.dumps(RUN_INPUT), dependencies)

    review_event = next(event for event in events if event.get("name") == "domain_review")
    expected_hash = sha256(
        json.dumps(
            {"packet": captured["packet"], "response": captured["response"]},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    assert review_event["evidence"]["review_hash"] == expected_hash


def test_release_review_packet_omits_the_validated_prereview_state() -> None:
    dependencies = _release_dependencies()
    captured: dict[str, object] = {}

    def invoke_model(request: dict[str, object]) -> dict[str, object]:
        captured["packet"] = request["packet"]
        return _approved_review(request)

    dependencies["invoke_model"] = invoke_model

    events = execute_release_run(json.dumps(RUN_INPUT), dependencies)

    packet = captured["packet"]
    assert isinstance(packet, dict)
    assert packet["response_contract"] == {
        "actual_model": "exact invoked model identifier",
        "verdict": "pass or fail",
        "reason": "nonempty concise string",
        "evidence": "JSON object",
    }
    instruction = packet["instruction"]
    assert isinstance(instruction, str)
    assert "exactly one compact JSON object" in instruction
    release_controls = packet["release_controls"]
    assert isinstance(release_controls, dict)
    assert "review_decision" not in release_controls
    assert packet["candidate_material"] == _review_material()
    assert events[-1]["status"] == "delivered"


def test_release_blocks_before_model_when_review_material_hash_changes() -> None:
    dependencies = _release_dependencies()
    original_prepare = dependencies["prepare"]
    invoked = False

    def prepare(run, admitted):
        prepared = original_prepare(run, admitted)
        prepared["review_material"]["release_note_sha256"] = "0" * 64
        return prepared

    def invoke_model(_request):
        nonlocal invoked
        invoked = True
        raise AssertionError("invalid review material must not reach a model")

    dependencies["prepare"] = prepare
    dependencies["invoke_model"] = invoke_model

    events = execute_release_run(json.dumps(RUN_INPUT), dependencies)

    assert events[-1]["status"] == "blocked"
    assert "release_note hash does not match" in events[-1]["reason"]
    assert invoked is False


def test_pull_request_review_rejects_prepared_main_material_mode() -> None:
    dependencies = _release_dependencies()
    original_prepare = dependencies["prepare"]
    invoked = False

    def prepare(run, admitted):
        prepared = original_prepare(run, admitted)
        prepared["review_material"] = _review_material(
            mode="prepared_main_documents"
        )
        return prepared

    def invoke_model(_request):
        nonlocal invoked
        invoked = True
        raise AssertionError("mismatched review mode must not reach a model")

    dependencies["prepare"] = prepare
    dependencies["invoke_model"] = invoke_model

    events = execute_release_run(json.dumps(RUN_INPUT), dependencies)

    assert events[-1]["status"] == "blocked"
    assert "mode does not match the release preparation" in events[-1]["reason"]
    assert invoked is False


def test_pull_request_review_rejects_an_empty_candidate_diff() -> None:
    dependencies = _release_dependencies()
    original_prepare = dependencies["prepare"]
    invoked = False

    def prepare(run, admitted):
        prepared = original_prepare(run, admitted)
        prepared["review_material"]["candidate_diff"] = ""
        prepared["review_material"]["candidate_diff_sha256"] = sha256(b"").hexdigest()
        return prepared

    def invoke_model(_request):
        nonlocal invoked
        invoked = True
        raise AssertionError("empty candidate diff must not reach a model")

    dependencies["prepare"] = prepare
    dependencies["invoke_model"] = invoke_model

    events = execute_release_run(json.dumps(RUN_INPUT), dependencies)

    assert events[-1]["status"] == "blocked"
    assert "candidate_diff must be a bounded string" in events[-1]["reason"]
    assert invoked is False


def test_failed_model_process_is_not_misreported_as_invalid_json(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        release_module.subprocess,
        "run",
        lambda *_arguments, **_keywords: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="error: qualified routes exhausted\n",
        ),
    )
    ticket = {
        "model_use_id": 71,
        "purpose": "review",
        "route_id": "claude-code",
        "source_revision": SOURCE_SHA,
        "ticket": "opaque-run-scoped-ticket",
    }

    with pytest.raises(
        ValueError,
        match="^review model invocation failed$",
    ):
        release_module._invoke_ticketed_model_from_runner(
            RUN_INPUT,
            ticket,
            {"instruction": "review"},
            tmp_path,
        )


def test_nonzero_model_block_remains_an_independent_review_block(
    monkeypatch,
    tmp_path,
) -> None:
    response = {
        "model_use_id": 71,
        "reason": "release note content was absent from the review packet",
        "route_id": "claude-code",
        "status": "blocked",
        "verdict": "BLOCK",
    }
    monkeypatch.setattr(
        release_module.subprocess,
        "run",
        lambda *_arguments, **_keywords: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(response),
            stderr="",
        ),
    )
    ticket = {
        "model_use_id": 71,
        "purpose": "review",
        "route_id": "claude-code",
        "source_revision": SOURCE_SHA,
        "ticket": "opaque-run-scoped-ticket",
    }

    with pytest.raises(
        ValueError,
        match=(
            "^independent review blocked the release: release note content "
            "was absent from the review packet$"
        ),
    ):
        release_module._invoke_ticketed_model_from_runner(
            RUN_INPUT,
            ticket,
            {"instruction": "review"},
            tmp_path,
        )


def test_nonzero_model_approval_is_still_an_invocation_failure(
    monkeypatch,
    tmp_path,
) -> None:
    response = {
        "model_use_id": 71,
        "reason": "candidate approved",
        "route_id": "claude-code",
        "status": "approved",
        "verdict": "APPROVE",
    }
    monkeypatch.setattr(
        release_module.subprocess,
        "run",
        lambda *_arguments, **_keywords: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(response),
            stderr="",
        ),
    )
    ticket = {
        "model_use_id": 71,
        "purpose": "review",
        "route_id": "claude-code",
        "source_revision": SOURCE_SHA,
        "ticket": "opaque-run-scoped-ticket",
    }

    with pytest.raises(
        ValueError,
        match="^review model invocation failed$",
    ):
        release_module._invoke_ticketed_model_from_runner(
            RUN_INPUT,
            ticket,
            {"instruction": "review"},
            tmp_path,
        )


def test_runtime_heartbeats_keep_one_contiguous_protocol_sequence() -> None:
    dependencies = _release_dependencies()
    original_prepare = dependencies["prepare"]
    heartbeat = None

    def set_heartbeat(callback):
        nonlocal heartbeat
        heartbeat = callback

    def prepare(run, admitted):
        assert heartbeat is not None
        heartbeat()
        heartbeat()
        return original_prepare(run, admitted)

    dependencies["set_heartbeat"] = set_heartbeat
    dependencies["prepare"] = prepare
    emitted: list[dict[str, object]] = []

    events = execute_release_run(
        json.dumps(RUN_INPUT),
        dependencies,
        emit=emitted.append,
    )

    assert events == emitted
    assert [event["sequence"] for event in events] == list(
        range(1, len(events) + 1)
    )
    assert [event["type"] for event in events[:4]] == [
        "milestone",
        "heartbeat",
        "heartbeat",
        "milestone",
    ]


def test_no_action_is_deterministic_and_consumes_no_model_ticket() -> None:
    # The runner fails a no_action result that carries a review_model_use_id
    # or that produced any model use at all: a deterministic no_action is
    # reviewed once when the lane revision is promoted, not per run. This
    # lane requested and invoked a review on every no_action, so every run
    # died with "no_action cannot issue or consume a model ticket".
    requested: list[dict[str, object]] = []
    dependencies = _release_dependencies()
    dependencies["collect_admission"] = lambda _run: _admission_input(
        releasable=False
    )
    dependencies["request_model_ticket"] = lambda request: requested.append(
        request
    ) or _fail("no_action must not request a model ticket")

    events = execute_release_run(json.dumps(RUN_INPUT), dependencies)

    assert [event["type"] for event in events] == ["milestone", "result"]
    assert events[-1]["status"] == "no_action"
    assert events[-1]["candidate_revision"] is None
    assert events[-1]["review_model_use_id"] is None
    assert requested == []


def _fail(message: str):
    raise AssertionError(message)


def test_release_blocks_when_ledger_approval_is_not_bound_to_candidate() -> None:
    dependencies = _release_dependencies()
    dependencies["collect_candidate_approval"] = lambda run, _candidate, review: {
        "approval_id_hash": "6" * 64,
        "authority": "standing-delegation",
        "candidate_revision": SOURCE_SHA,
        "contract_digest": run["contract_revision"],
        "review_model_use_id": review["model_use_id"],
        "run_id": run["run_id"],
    }

    events = execute_release_run(json.dumps(RUN_INPUT), dependencies)

    assert events[-1]["status"] == "blocked"
    assert events[-1]["candidate_revision"] == CANDIDATE_SHA
    assert "candidate approval revision does not match" in events[-1]["reason"]


def test_release_blocks_when_review_ticket_is_not_the_qualified_claude_route() -> None:
    dependencies = _release_dependencies()
    dependencies["request_model_ticket"] = lambda request: {
        "model_use_id": 71,
        "purpose": request["purpose"],
        "route_id": "codex",
        "source_revision": request["source_revision"],
        "ticket": "opaque-run-scoped-ticket",
    }

    events = execute_release_run(json.dumps(RUN_INPUT), dependencies)

    assert events[-1]["status"] == "blocked"
    assert "qualified Claude route" in events[-1]["reason"]


@pytest.mark.parametrize("stage", ["prepare", "deliver"])
def test_release_marks_external_release_failure_as_failed_with_recovery_evidence(
    stage: str,
) -> None:
    dependencies = _release_dependencies()

    class ExternalFailure(ValueError):
        external_state_changed = True
        evidence = {
            "external_state_changed": True,
            "rollback_completed": True,
        }

    dependencies[stage] = lambda *_arguments: (_ for _ in ()).throw(
        ExternalFailure("stable publication failed")
    )

    events = execute_release_run(json.dumps(RUN_INPUT), dependencies)

    assert events[-1]["status"] == "failed"
    assert events[-1]["evidence"] == ExternalFailure.evidence


def test_release_cli_default_invocation_reads_complete_run_input(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("AI_OPERATIONS_RUN_INPUT", json.dumps(RUN_INPUT))

    exit_code = main([], dependencies=_release_dependencies())
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert exit_code == 0
    assert lines[-1]["status"] == "delivered"
    assert "candidate_revision" in lines[-1]


def test_release_cli_rollback_preflight_failure_is_one_blocked_recovery_result(
    monkeypatch,
    capsys,
) -> None:
    recovery_input = {
        "authority_revision": AUTHORITY_SHA,
        "candidate_revision": CANDIDATE_SHA,
        "contract_id": "data-olympus-release",
        "contract_revision": CONTRACT_SHA,
        "failure_reason": "notification failed",
        "outcome_evidence": {
            "digest": IMAGE_DIGEST,
            "rollback_digest": PREVIOUS_DIGEST,
        },
        "run_id": RUN_INPUT["run_id"],
        "source_revision": SOURCE_SHA,
    }
    monkeypatch.setenv(
        "AI_OPERATIONS_RECOVERY_INPUT",
        json.dumps(recovery_input),
    )
    dependencies = _release_dependencies()
    dependencies["rollback"] = lambda _recovery: (_ for _ in ()).throw(
        ValueError("gateway unavailable")
    )

    exit_code = main(["rollback"], dependencies=dependencies)
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output == {
        "status": "blocked",
        "reason": "gateway unavailable",
        "evidence": {},
        "source_revision": SOURCE_SHA,
    }


def test_release_cli_preserves_rollback_failure_evidence(monkeypatch, capsys) -> None:
    recovery_input = {
        "outcome_evidence": {
            "digest": IMAGE_DIGEST,
            "rollback_digest": PREVIOUS_DIGEST,
        },
        "source_revision": SOURCE_SHA,
    }
    monkeypatch.setenv(
        "AI_OPERATIONS_RECOVERY_INPUT",
        json.dumps(recovery_input),
    )

    class RecoveryFailure(ValueError):
        external_state_changed = True
        evidence = {
            "digest_apply_confirmed": True,
            "external_state_changed": True,
            "rollback_completed": False,
        }

    dependencies = _release_dependencies()
    dependencies["rollback"] = lambda _recovery: (_ for _ in ()).throw(
        RecoveryFailure("acceptance failed")
    )

    exit_code = main(["rollback"], dependencies=dependencies)
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["status"] == "failed"
    assert output["evidence"] == RecoveryFailure.evidence
