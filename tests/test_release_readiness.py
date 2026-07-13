from __future__ import annotations

import copy
import json
import subprocess
import sys
from typing import Any

import pytest

from scripts.release_readiness import GateResult, evaluate, main

SHA_INTEGRATION = "a" * 40
SHA_REVIEW_67 = "b" * 40
SHA_REVIEW_68 = "c" * 40
DIGEST = "sha256:" + "d" * 64


def _valid_bundle() -> dict[str, Any]:
    return {
        "manifest": {
            "version": "0.5.0",
            "integration_sha": SHA_INTEGRATION,
            "feature_ids": ["KNA-67", "KNA-68"],
            "contract_blob_sha": "deadbeef",
            "security_classification": {"KNA-67": "standard", "KNA-68": "security"},
        },
        "tickets": [
            {
                "id": "KNA-67",
                "status": "done",
                "review_evidence": {
                    "companion_review_present": True,
                    "verdict": "approved",
                    "reviewed_sha": SHA_REVIEW_67,
                    "acceptance_verified": True,
                },
            },
            {
                "id": "KNA-68",
                "status": "done",
                "review_evidence": {
                    "companion_review_present": True,
                    "verdict": "approved",
                    "reviewed_sha": SHA_REVIEW_68,
                    "acceptance_verified": True,
                    "security_verdict": "approved",
                },
            },
        ],
        "ci": {
            "sha": SHA_INTEGRATION,
            "all_success": True,
            "found_any": True,
            "missing_required": [],
        },
        "verify": {
            "exit_code": 0,
            "digest_served": DIGEST,
            "target": "https://example.invalid/verify",
        },
        "version_free": {"free": True},
        "deployed_digest": {"digest": DIGEST, "source": "kndev"},
        "expected_rc_digest": DIGEST,
        "integration_review": {
            "approved": True,
            "reviewer": "Reviewer High-Risk",
            "locked_revision_id": "rev-123",
            "reviewed_sha": SHA_INTEGRATION,
        },
        "security": {"exit_code": 0},
        "open_blockers": [],
    }


ALL_CONDITIONS = [
    "manifest_complete",
    "every_feature_done",
    "every_review_validated",
    "integration_review_approved",
    "ci_green_for_exact_sha",
    "rc_digest_deployed",
    "verify_passed",
    "version_unpublished",
    "security_clear",
    "no_open_blockers",
]


def test_all_pass_is_ready() -> None:
    result = evaluate(_valid_bundle())
    assert isinstance(result, GateResult)
    assert result.ready is True
    assert all(result.conditions.values())
    assert result.conditions.keys() == set(ALL_CONDITIONS)
    assert result.blockers == []
    assert "READY" in result.report_md
    assert "NOT_READY" not in result.report_md


def test_empty_bundle_is_not_ready_and_does_not_raise() -> None:
    result = evaluate({})
    assert result.ready is False
    assert result.conditions["manifest_complete"] is False
    assert len(result.blockers) > 0


def test_none_like_bundle_types_do_not_raise() -> None:
    # Malformed bundle: wrong types everywhere. Must fail closed, never raise.
    bundle = {
        "manifest": None,
        "tickets": "not-a-list",
        "ci": 5,
        "verify": [],
        "version_free": None,
        "deployed_digest": "oops",
        "expected_rc_digest": None,
        "integration_review": None,
        "security": None,
        "open_blockers": "nope",
    }
    result = evaluate(bundle)
    assert result.ready is False
    # manifest.feature_ids is empty because manifest itself is malformed, so
    # the per-feature loops (every_feature_done / every_review_validated) are
    # vacuously True over zero features; manifest_complete still correctly
    # fails and gates overall readiness, which is what matters for fail-closed.
    assert result.conditions["manifest_complete"] is False
    non_vacuous = {
        k: v
        for k, v in result.conditions.items()
        if k not in {"every_feature_done", "every_review_validated"}
    }
    assert all(v is False for v in non_vacuous.values()), result.conditions


def test_caller_supplied_ready_true_is_ignored_when_ci_fails() -> None:
    bundle = _valid_bundle()
    bundle["ready"] = True
    bundle["review_validated"] = True
    bundle["ci"]["all_success"] = False
    result = evaluate(bundle)
    assert result.ready is False
    assert result.conditions["ci_green_for_exact_sha"] is False
    assert any("ci_green_for_exact_sha" in b for b in result.blockers)


def test_caller_supplied_ready_true_alone_does_not_make_it_ready() -> None:
    bundle = {"ready": True, "review_validated": True}
    result = evaluate(bundle)
    assert result.ready is False


@pytest.mark.parametrize(
    ("mutate", "failed_condition"),
    [
        (lambda b: b["manifest"].pop("version"), "manifest_complete"),
        (lambda b: b["manifest"].__setitem__("integration_sha", "not-a-sha"), "manifest_complete"),
        (lambda b: b["manifest"].__setitem__("feature_ids", []), "manifest_complete"),
        (lambda b: b["manifest"].pop("contract_blob_sha"), "manifest_complete"),
        (
            lambda b: b["tickets"].append(copy.deepcopy(b["tickets"][0])),
            "manifest_complete",
        ),
        (lambda b: b["tickets"][0].__setitem__("status", "in_progress"), "every_feature_done"),
        (lambda b: b["tickets"].pop(0), "every_feature_done"),
        (
            lambda b: b["tickets"][0]["review_evidence"].__setitem__(
                "companion_review_present", False
            ),
            "every_review_validated",
        ),
        (
            lambda b: b["tickets"][0]["review_evidence"].__setitem__(
                "verdict", "changes_requested"
            ),
            "every_review_validated",
        ),
        (
            lambda b: b["tickets"][0]["review_evidence"].__setitem__("reviewed_sha", "short"),
            "every_review_validated",
        ),
        (
            lambda b: b["tickets"][0]["review_evidence"].__setitem__("acceptance_verified", False),
            "every_review_validated",
        ),
        (
            lambda b: b["tickets"][1]["review_evidence"].pop("security_verdict"),
            "every_review_validated",
        ),
        (
            lambda b: b["tickets"][1]["review_evidence"].__setitem__(
                "security_verdict", "pending"
            ),
            "every_review_validated",
        ),
        (
            lambda b: b["integration_review"].__setitem__("approved", False),
            "integration_review_approved",
        ),
        (
            lambda b: b["integration_review"].__setitem__("reviewer", ""),
            "integration_review_approved",
        ),
        (
            lambda b: b["integration_review"].__setitem__("locked_revision_id", ""),
            "integration_review_approved",
        ),
        (
            lambda b: b["integration_review"].__setitem__("reviewed_sha", "e" * 40),
            "integration_review_approved",
        ),
        (lambda b: b["ci"].__setitem__("found_any", False), "ci_green_for_exact_sha"),
        (lambda b: b["ci"].__setitem__("all_success", False), "ci_green_for_exact_sha"),
        (lambda b: b["ci"].__setitem__("sha", "f" * 40), "ci_green_for_exact_sha"),
        (lambda b: b["ci"].__setitem__("missing_required", ["build"]), "ci_green_for_exact_sha"),
        (
            lambda b: b["deployed_digest"].__setitem__("digest", "sha256:" + "0" * 64),
            "rc_digest_deployed",
        ),
        (lambda b: b.__setitem__("expected_rc_digest", ""), "rc_digest_deployed"),
        (lambda b: b["verify"].__setitem__("exit_code", 1), "verify_passed"),
        (lambda b: b["version_free"].__setitem__("free", False), "version_unpublished"),
        (lambda b: b["security"].__setitem__("exit_code", 1), "security_clear"),
        (
            lambda b: b.__setitem__("open_blockers", ["KNA-99 needs a follow-up"]),
            "no_open_blockers",
        ),
    ],
)
def test_single_condition_failure(mutate, failed_condition: str) -> None:
    bundle = _valid_bundle()
    mutate(bundle)
    result = evaluate(bundle)
    assert result.conditions[failed_condition] is False, result.conditions
    assert result.ready is False
    assert any(b.startswith(failed_condition) for b in result.blockers), result.blockers
    assert "NOT_READY" in result.report_md


def test_extra_unknown_keys_are_tolerated() -> None:
    bundle = _valid_bundle()
    bundle["unexpected_top_level_key"] = {"whatever": "value"}
    bundle["manifest"]["unexpected_manifest_key"] = "value"
    bundle["tickets"][0]["unexpected_ticket_key"] = "value"
    result = evaluate(bundle)
    assert result.ready is True


def test_missing_verify_digest_served_does_not_fail_verify_passed() -> None:
    bundle = _valid_bundle()
    del bundle["verify"]["digest_served"]
    result = evaluate(bundle)
    assert result.conditions["verify_passed"] is True
    assert result.ready is True


def test_report_md_lists_version_sha_and_all_condition_lines() -> None:
    result = evaluate(_valid_bundle())
    for name in ALL_CONDITIONS:
        assert name in result.report_md
    assert "0.5.0" in result.report_md
    assert SHA_INTEGRATION in result.report_md


def test_duplicate_ticket_for_same_feature_fails_manifest_complete() -> None:
    bundle = _valid_bundle()
    dup = copy.deepcopy(bundle["tickets"][0])
    bundle["tickets"].append(dup)
    result = evaluate(bundle)
    assert result.conditions["manifest_complete"] is False
    assert result.ready is False


def test_evaluate_never_raises_on_wildly_malformed_bundle() -> None:
    result = evaluate({"manifest": {"feature_ids": [1, 2, None]}, "tickets": [1, "x", None, {}]})
    assert result.ready is False


def test_evaluate_never_raises_on_non_dict_bundle() -> None:
    result = evaluate([])  # type: ignore[arg-type]
    assert result.ready is False


def test_main_exit_code_0_when_ready(tmp_path) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_valid_bundle()))
    exit_code = main(["--evidence", str(evidence_path)])
    assert exit_code == 0


def test_main_exit_code_1_when_not_ready(tmp_path) -> None:
    bundle = _valid_bundle()
    bundle["security"]["exit_code"] = 1
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(bundle))
    exit_code = main(["--evidence", str(evidence_path)])
    assert exit_code == 1


def test_main_json_flag_emits_gate_result_json(tmp_path, capsys) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_valid_bundle()))
    exit_code = main(["--evidence", str(evidence_path), "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert "conditions" in payload and "blockers" in payload and "report_md" in payload


def test_main_reads_stdin_when_no_evidence_flag() -> None:
    bundle_json = json.dumps(_valid_bundle())
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.release_readiness"],
        input=bundle_json,
        capture_output=True,
        text=True,
        cwd=__file__.rsplit("/tests/", 1)[0],
    )
    assert proc.returncode == 0
    assert "READY" in proc.stdout


def test_main_not_valid_json_is_not_ready_not_a_crash(tmp_path) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text("{not valid json")
    exit_code = main(["--evidence", str(evidence_path)])
    assert exit_code == 1
