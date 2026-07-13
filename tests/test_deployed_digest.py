from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.deployed_digest import evaluate, main

if TYPE_CHECKING:
    import pytest


def _version(name: str, tags: list[str]) -> dict:
    return {"name": name, "metadata": {"container": {"tags": tags}}}


def test_evaluate_single_match_resolves_digest() -> None:
    versions = [
        _version("sha256:aaaa", ["edge"]),
        _version("sha256:bbbb", ["stable", "v0.5.0"]),
    ]
    result = evaluate(versions, "stable")
    assert result["digest"] == "sha256:bbbb"
    assert result["source"] == "ghcr:stable"
    assert result["matched_versions"] == 1


def test_evaluate_no_match_fails_closed() -> None:
    versions = [_version("sha256:aaaa", ["edge"])]
    result = evaluate(versions, "stable")
    assert result["digest"] is None
    assert result["source"] is None
    assert result["matched_versions"] == 0


def test_evaluate_ambiguous_match_fails_closed() -> None:
    versions = [
        _version("sha256:aaaa", ["stable"]),
        _version("sha256:bbbb", ["stable"]),
    ]
    result = evaluate(versions, "stable")
    assert result["digest"] is None
    assert result["matched_versions"] == 2


def test_evaluate_empty_digest_name_fails_closed() -> None:
    versions = [_version("", ["stable"])]
    result = evaluate(versions, "stable")
    assert result["digest"] is None


def test_evaluate_malformed_metadata_ignored() -> None:
    versions = [{"name": "sha256:aaaa", "metadata": "not-a-dict"}]
    result = evaluate(versions, "stable")
    assert result["digest"] is None
    assert result["matched_versions"] == 0


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, *, versions: object) -> None:
    import scripts.deployed_digest as mod

    def _raise_or_return(_package: str, _org: str) -> list[dict]:
        if isinstance(versions, Exception):
            raise versions
        return versions  # type: ignore[return-value]

    monkeypatch.setattr(mod, "_fetch_versions", _raise_or_return)


def test_main_success_path_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _patch_fetch(monkeypatch, versions=[_version("sha256:cccc", ["stable"])])
    code = main(["--target", "stable", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    assert "sha256:cccc" in out
    assert '"digest": null' not in out


def test_main_fail_closed_on_lookup_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _patch_fetch(monkeypatch, versions=RuntimeError("gh api unreachable"))
    code = main(["--target", "stable", "--json"])
    assert code != 0
    out = capsys.readouterr().out
    assert '"digest": null' in out


def test_main_fail_closed_on_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch(monkeypatch, versions=[_version("sha256:aaaa", ["edge"])])
    code = main(["--target", "stable", "--json"])
    assert code != 0
