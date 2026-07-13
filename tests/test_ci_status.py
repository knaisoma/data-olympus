from __future__ import annotations

from scripts.ci_status import evaluate


def _check(name: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


def test_all_required_present_and_success() -> None:
    checks = [_check("lint"), _check("test"), _check("build")]
    result = evaluate(checks, ["lint", "test"])
    assert result["all_success"] is True
    assert result["found_any"] is True
    assert result["missing_required"] == []


def test_required_check_missing() -> None:
    checks = [_check("lint")]
    result = evaluate(checks, ["lint", "test"])
    assert result["all_success"] is False
    assert result["missing_required"] == ["test"]


def test_required_check_present_but_failed() -> None:
    checks = [_check("lint"), _check("test", conclusion="failure")]
    result = evaluate(checks, ["lint", "test"])
    assert result["all_success"] is False
    assert "test" in result["missing_required"]


def test_empty_check_runs_fails_closed() -> None:
    result = evaluate([], ["lint"])
    assert result["found_any"] is False
    assert result["all_success"] is False


def test_no_required_all_present_success() -> None:
    checks = [_check("lint"), _check("test")]
    result = evaluate(checks, [])
    assert result["all_success"] is True
    assert result["missing_required"] == []


def test_no_required_one_failure() -> None:
    checks = [_check("lint"), _check("test", conclusion="failure")]
    result = evaluate(checks, [])
    assert result["all_success"] is False


def test_in_progress_check_not_success() -> None:
    checks = [_check("lint", status="in_progress", conclusion=None)]
    result = evaluate(checks, [])
    assert result["all_success"] is False


def test_neutral_conclusion_not_success() -> None:
    checks = [_check("lint", conclusion="neutral")]
    result = evaluate(checks, [])
    assert result["all_success"] is False


def test_cancelled_conclusion_not_success() -> None:
    checks = [_check("lint", conclusion="cancelled")]
    result = evaluate(checks, [])
    assert result["all_success"] is False


def test_empty_check_runs_with_no_required_fails_closed() -> None:
    result = evaluate([], [])
    assert result["found_any"] is False
    assert result["all_success"] is False


def test_checks_field_shape() -> None:
    checks = [_check("lint")]
    result = evaluate(checks, [])
    assert result["checks"] == [{"name": "lint", "status": "completed", "conclusion": "success"}]
    assert result["required"] == []
