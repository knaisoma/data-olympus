from __future__ import annotations

from scripts.version_free import evaluate


def test_evaluate_all_absent_is_free() -> None:
    result = evaluate(False, False, False)
    assert result == {
        "pypi_taken": False,
        "ghcr_taken": False,
        "github_release_taken": False,
        "unreachable": [],
        "free": True,
    }


def test_evaluate_pypi_present_not_free() -> None:
    result = evaluate(True, False, False)
    assert result["pypi_taken"] is True
    assert result["free"] is False
    assert result["unreachable"] == []


def test_evaluate_any_none_not_free_and_unreachable() -> None:
    result = evaluate(None, False, False)
    assert result["free"] is False
    assert "pypi" in result["unreachable"]


def test_evaluate_mixed_unreachable_ghcr_only() -> None:
    result = evaluate(False, None, False)
    assert result["free"] is False
    assert result["unreachable"] == ["ghcr"]
    assert result["pypi_taken"] is False
    assert result["ghcr_taken"] is None
    assert result["github_release_taken"] is False


def test_evaluate_all_none_all_unreachable() -> None:
    result = evaluate(None, None, None)
    assert result["free"] is False
    assert result["unreachable"] == ["pypi", "ghcr", "github_release"]
    assert result["pypi_taken"] is None
    assert result["ghcr_taken"] is None
    assert result["github_release_taken"] is None


def test_evaluate_ghcr_present_not_free() -> None:
    result = evaluate(False, True, False)
    assert result["free"] is False
    assert result["ghcr_taken"] is True


def test_evaluate_gh_release_present_not_free() -> None:
    result = evaluate(False, False, True)
    assert result["free"] is False
    assert result["github_release_taken"] is True
