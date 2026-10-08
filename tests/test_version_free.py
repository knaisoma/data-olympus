from __future__ import annotations

import pytest

from scripts.version_free import _ghcr_present, evaluate, main, registry_versions


def test_evaluate_all_absent_is_free() -> None:
    result = evaluate(False, False, False, False)
    assert result == {
        "pypi_taken": False,
        "ghcr_taken": False,
        "github_release_taken": False,
        "github_tag_taken": False,
        "unreachable": [],
        "free": True,
    }


def test_evaluate_pypi_present_not_free() -> None:
    result = evaluate(True, False, False, False)
    assert result["pypi_taken"] is True
    assert result["free"] is False
    assert result["unreachable"] == []


def test_evaluate_any_none_not_free_and_unreachable() -> None:
    result = evaluate(None, False, False, False)
    assert result["free"] is False
    assert "pypi" in result["unreachable"]


def test_evaluate_mixed_unreachable_ghcr_only() -> None:
    result = evaluate(False, None, False, False)
    assert result["free"] is False
    assert result["unreachable"] == ["ghcr"]
    assert result["pypi_taken"] is False
    assert result["ghcr_taken"] is None
    assert result["github_release_taken"] is False


def test_evaluate_all_none_all_unreachable() -> None:
    result = evaluate(None, None, None, None)
    assert result["free"] is False
    assert result["unreachable"] == ["pypi", "ghcr", "github_release", "github_tag"]
    assert result["pypi_taken"] is None
    assert result["ghcr_taken"] is None
    assert result["github_release_taken"] is None


def test_evaluate_ghcr_present_not_free() -> None:
    result = evaluate(False, True, False, False)
    assert result["free"] is False
    assert result["ghcr_taken"] is True


def test_evaluate_gh_release_present_not_free() -> None:
    result = evaluate(False, False, True, False)
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
    monkeypatch.setattr(module, "_gh_tag_present", lambda *_args: False)
    assert main(["--version", "0.6.0-rc.3"]) == 0
    assert seen == {
        "pypi": "0.6.0rc3",
        "ghcr": "0.6.0-rc.3",
        "github": "0.6.0-rc.3",
    }


def test_ghcr_present_uses_public_manifest_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.version_free as module

    seen: list[list[str]] = []

    def run(command: list[str], **_kwargs) -> object:
        seen.append(command)
        return module.subprocess.CompletedProcess(command, 0, "manifest", "")

    monkeypatch.setattr(module.subprocess, "run", run)

    assert _ghcr_present("0.7.0-rc.1") is True
    assert seen == [[
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        "ghcr.io/knaisoma/data-olympus:0.7.0-rc.1",
    ]]


def test_ghcr_present_accepts_only_explicit_missing_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.version_free as module

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: module.subprocess.CompletedProcess(
            [], 1, "", "manifest unknown"
        ),
    )

    assert _ghcr_present("0.7.0-rc.1") is False


@pytest.mark.parametrize(
    "failure",
    [
        "unauthorized",
        "connection refused",
    ],
)
def test_ghcr_present_fails_closed_on_client_or_registry_error(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import scripts.version_free as module

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: module.subprocess.CompletedProcess(
            [], 1, "", failure
        ),
    )

    assert _ghcr_present("0.7.0-rc.1") is None


@pytest.mark.parametrize("version,python", [
    ("1.2.3-hotfix.rc.4", "1.2.3.dev4"),
    ("1.2.3-hotfix.rc.0", "1.2.3.dev0"),
    ("1.2.3-rc.0", "1.2.3rc0"),
])
def test_new_candidate_mappings(version: str, python: str) -> None:
    mapped = registry_versions(version)
    assert (mapped.pypi, mapped.ghcr, mapped.github) == (python, version, version)


@pytest.mark.parametrize("present", [True, None])
def test_git_tag_collision_or_outage_blocks_release(present: bool | None) -> None:
    result = evaluate(False, False, False, present)
    assert result["free"] is False
    assert result["github_tag_taken"] is present


@pytest.mark.parametrize("present,exit_code", [(True, 1), (None, 1), (False, 0)])
def test_main_checks_hotfix_tag_independently(
    monkeypatch: pytest.MonkeyPatch, present: bool | None, exit_code: int,
) -> None:
    import scripts.version_free as module

    monkeypatch.setattr(module, "_pypi_present", lambda *_args: False)
    monkeypatch.setattr(module, "_ghcr_present", lambda *_args: False)
    monkeypatch.setattr(module, "_gh_release_present", lambda *_args: False)
    monkeypatch.setattr(module, "_gh_tag_present", lambda *_args: present, raising=False)
    assert main(["--version", "1.2.3-hotfix.rc.4"]) == exit_code


@pytest.mark.parametrize("returncode,stderr,expected", [
    (0, "", True), (1, "HTTP 404: Not Found", False), (1, "unauthorized", None),
])
def test_remote_git_tag_check(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: str, expected: bool | None,
) -> None:
    import scripts.version_free as module

    def run(command: list[str], **_kwargs: object) -> object:
        assert command == ["gh", "api", "repos/a/b/git/ref/tags/1.2.3-hotfix.rc.4"]
        return module.subprocess.CompletedProcess(command, returncode, "", stderr)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module._gh_tag_present("1.2.3-hotfix.rc.4", "a/b") is expected


@pytest.mark.parametrize("error", [FileNotFoundError("gh"), TimeoutError("timeout")])
def test_registry_transport_failure_is_unknown(
    monkeypatch: pytest.MonkeyPatch, error: OSError,
) -> None:
    import scripts.version_free as module

    def fail(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(module.subprocess, "run", fail)
    monkeypatch.setattr(module.urllib.request, "urlopen", fail)
    assert module._gh_tag_present("1.2.3-rc.4") is None
    assert module._pypi_present("1.2.3rc4") is None
