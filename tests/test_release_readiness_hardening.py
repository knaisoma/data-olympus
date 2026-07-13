"""Regression tests for companion-review findings on the readiness evaluator.

Covers the four fail-open / crash gaps found in adversarial review:
1. security-classification bypass (only exact "security" matched),
2. exit_code truthiness trap (False / 0.0 accepted as success),
3. digest fail-open (missing digest_served skipped the check),
4. CLI file-read crash instead of fail-closed.

Also covers a fifth gap found in a later hardening pass: a MISSING
top-level evidence key (e.g. "open_blockers" simply absent from the
bundle) must fail closed, the same as a present-but-invalid value.
Previously "open_blockers" defaulted to [] when absent, so an evidence
bundle that never populated that field was silently treated as clear.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.release_readiness import evaluate, main  # noqa: E402

_SHA = "a" * 40


def _ready_bundle() -> dict:
    return {
        "manifest": {
            "version": "0.5.0",
            "integration_sha": _SHA,
            "feature_ids": ["FEATURE-1"],
            "contract_blob_sha": "deadbeef",
            "security_classification": {"FEATURE-1": "standard"},
        },
        "tickets": [
            {
                "id": "FEATURE-1",
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
        b["manifest"]["security_classification"]["FEATURE-1"] = label
        # no security_verdict present -> must be NOT ready
        assert evaluate(b).ready is False, f"classification {label!r} bypassed security review"
        # with an approved security_verdict -> ready again
        b["tickets"][0]["review_evidence"]["security_verdict"] = "approved"
        assert evaluate(b).ready is True


def test_missing_classification_defaults_to_security_required() -> None:
    b = _ready_bundle()
    b["manifest"]["security_classification"] = {}  # no entry for FEATURE-1
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


def test_missing_open_blockers_key_is_not_ready() -> None:
    # A bundle that never populated open_blockers must NOT be treated as
    # equivalent to an explicit empty list. Missing evidence is not the
    # same claim as verified-clear evidence.
    b = _ready_bundle()
    del b["open_blockers"]
    result = evaluate(b)
    assert result.conditions["no_open_blockers"] is False
    assert result.ready is False
    assert any(bl.startswith("no_open_blockers") for bl in result.blockers), result.blockers


@pytest.mark.parametrize(
    ("top_level_key", "failed_condition"),
    [
        ("manifest", "manifest_complete"),
        ("tickets", "every_feature_done"),
        ("integration_review", "integration_review_approved"),
        ("ci", "ci_green_for_exact_sha"),
        ("expected_rc_digest", "rc_digest_deployed"),
        ("deployed_digest", "rc_digest_deployed"),
        ("verify", "verify_passed"),
        ("version_free", "version_unpublished"),
        ("security", "security_clear"),
        ("open_blockers", "no_open_blockers"),
    ],
)
def test_missing_required_top_level_field_is_not_ready(
    top_level_key: str, failed_condition: str
) -> None:
    # Omitting any of the top-level evidence keys the gate reads must make
    # its owning condition (and therefore the overall verdict) NOT ready.
    # This is the fail-closed principle applied to absence, not just to
    # present-but-malformed values (which the other tests in this module
    # and in test_release_readiness.py already cover).
    b = _ready_bundle()
    del b[top_level_key]
    result = evaluate(b)
    assert result.conditions[failed_condition] is False, result.conditions
    assert result.ready is False
