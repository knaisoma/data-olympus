"""Regression tests for companion-review findings on the readiness evaluator.

Covers the four fail-open / crash gaps found in adversarial review:
1. security-classification bypass (only exact "security" matched),
2. exit_code truthiness trap (False / 0.0 accepted as success),
3. digest fail-open (missing digest_served skipped the check),
4. CLI file-read crash instead of fail-closed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.release_readiness import evaluate, main  # noqa: E402

_SHA = "a" * 40


def _ready_bundle() -> dict:
    return {
        "manifest": {
            "version": "0.5.0",
            "integration_sha": _SHA,
            "feature_ids": ["KNA-67"],
            "contract_blob_sha": "deadbeef",
            "security_classification": {"KNA-67": "standard"},
        },
        "tickets": [
            {
                "id": "KNA-67",
                "status": "done",
                "review_evidence": {
                    "companion_review_present": True,
                    "verdict": "approved",
                    "reviewed_sha": _SHA,
                    "acceptance_verified": True,
                },
            }
        ],
        "ci": {"sha": _SHA, "found_any": True, "all_success": True, "missing_required": []},
        "verify": {"exit_code": 0},
        "version_free": {"free": True},
        "deployed_digest": {"digest": "sha256:abc"},
        "expected_rc_digest": "sha256:abc",
        "integration_review": {
            "approved": True,
            "reviewer": "Reviewer High-Risk",
            "locked_revision_id": "rev1",
            "reviewed_sha": _SHA,
        },
        "security": {"exit_code": 0},
        "open_blockers": [],
    }


def test_baseline_is_ready() -> None:
    assert evaluate(_ready_bundle()).ready is True


def test_security_classification_non_standard_requires_verdict() -> None:
    # Any classification that is not exactly "standard" must require an approved
    # security_verdict; "high-security", True, and 1 must NOT bypass it.
    for label in ("high-security", "security", True, 1):
        b = _ready_bundle()
        b["manifest"]["security_classification"]["KNA-67"] = label
        # no security_verdict present -> must be NOT ready
        assert evaluate(b).ready is False, f"classification {label!r} bypassed security review"
        # with an approved security_verdict -> ready again
        b["tickets"][0]["review_evidence"]["security_verdict"] = "approved"
        assert evaluate(b).ready is True


def test_missing_classification_defaults_to_security_required() -> None:
    b = _ready_bundle()
    b["manifest"]["security_classification"] = {}  # no entry for KNA-67
    assert evaluate(b).ready is False
    b["tickets"][0]["review_evidence"]["security_verdict"] = "approved"
    assert evaluate(b).ready is True


def test_exit_code_false_is_not_success() -> None:
    b = _ready_bundle()
    b["verify"]["exit_code"] = False
    assert evaluate(b).conditions["verify_passed"] is False
    b = _ready_bundle()
    b["security"]["exit_code"] = False
    assert evaluate(b).conditions["security_clear"] is False


def test_exit_code_float_is_not_success() -> None:
    b = _ready_bundle()
    b["verify"]["exit_code"] = 0.0
    assert evaluate(b).conditions["verify_passed"] is False


def test_rc_digest_mismatch_fails_closed() -> None:
    b = _ready_bundle()
    b["deployed_digest"]["digest"] = "sha256:different"
    assert evaluate(b).conditions["rc_digest_deployed"] is False
    b = _ready_bundle()
    b["deployed_digest"] = {}  # missing digest
    assert evaluate(b).conditions["rc_digest_deployed"] is False


def test_cli_missing_file_fails_closed_no_crash() -> None:
    rc = main(["--evidence", "/nonexistent/path/to/evidence.json"])
    assert rc == 1  # fail-closed, no traceback
