from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.version_free import evaluate, main, registry_versions

if TYPE_CHECKING:
    import pytest


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


def test_registry_versions_map_candidate_channels() -> None:
    versions = registry_versions("0.6.0-rc.3")
    assert versions.pypi == "0.6.0rc3"
    assert versions.ghcr == "0.6.0-rc.3"
    assert versions.github == "0.6.0-rc.3"


def test_registry_versions_keep_stable_channels() -> None:
    versions = registry_versions("0.6.0")
    assert versions.pypi == "0.6.0"
    assert versions.ghcr == "v0.6.0"
    assert versions.github == "v0.6.0"


def test_main_queries_candidate_registry_spellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.version_free as module

    seen: dict[str, str] = {}

    def pypi(version: str, _package: str) -> bool:
        seen["pypi"] = version
        return False

    def ghcr(version: str, _package: str) -> bool:
        seen["ghcr"] = version
        return False

    def github(version: str, _repo: str) -> bool:
        seen["github"] = version
        return False

    monkeypatch.setattr(module, "_pypi_present", pypi)
    monkeypatch.setattr(module, "_ghcr_present", ghcr)
    monkeypatch.setattr(module, "_gh_release_present", github)
    assert main(["--version", "0.6.0-rc.3"]) == 0
    assert seen == {
        "pypi": "0.6.0rc3",
        "ghcr": "0.6.0-rc.3",
        "github": "0.6.0-rc.3",
    }
