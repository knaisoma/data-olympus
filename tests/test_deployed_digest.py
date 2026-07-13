from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scripts.deployed_digest import evaluate, main

if TYPE_CHECKING:
    import pytest


def _version(name: str, tags: list[str]) -> dict:
    return {"name": name, "metadata": {"container": {"tags": tags}}}


# Valid-shape sha256 digests ("sha256:" + 64 hex chars) for tests that expect
# a successful resolution; format validation now rejects anything shorter.
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64


def test_evaluate_single_match_resolves_digest() -> None:
    versions = [
        _version("sha256:aaaa", ["edge"]),
        _version(_DIGEST_B, ["stable", "v0.5.0"]),
    ]
    result = evaluate(versions, "stable")
    assert result["digest"] == _DIGEST_B
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
    _patch_fetch(monkeypatch, versions=[_version(_DIGEST_C, ["stable"])])
    code = main(["--target", "stable", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    assert _DIGEST_C in out
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


def test_evaluate_non_sha256_digest_fails_closed() -> None:
    versions = [_version("not-a-digest", ["stable"])]
    result = evaluate(versions, "stable")
    assert result["digest"] is None
    assert result["source"] is None
    assert result["matched_versions"] == 1


def test_main_fail_closed_on_non_sha256_digest(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _patch_fetch(monkeypatch, versions=[_version("not-a-digest", ["stable"])])
    code = main(["--target", "stable", "--json"])
    assert code != 0
    out = capsys.readouterr().out
    assert '"digest": null' in out


def test_main_fail_closed_on_malformed_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import json as json_mod

    _patch_fetch(
        monkeypatch, versions=json_mod.JSONDecodeError("bad json", "{not valid", 0)
    )
    code = main(["--target", "stable", "--json"])
    assert code != 0
    out = capsys.readouterr().out
    assert '"digest": null' in out
    assert '"source": null' in out


def test_main_fail_closed_on_non_dict_payload_shape(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # _fetch_versions returning something that isn't a list of version dicts
    # (e.g. a single dict instead of a list) must not crash main(); it must
    # fail closed the same as any other lookup failure.
    _patch_fetch(monkeypatch, versions={"unexpected": "shape"})
    code = main(["--target", "stable", "--json"])
    assert code != 0
    out = capsys.readouterr().out
    assert '"digest": null' in out


def test_evaluate_tolerates_non_dict_version_entries() -> None:
    # Individual entries in the versions list that are not dicts at all
    # (e.g. a bare string or None) must be ignored, not raise.
    versions: list[Any] = [1, "x", None, {}]
    result = evaluate(versions, "stable")
    assert result["digest"] is None
    assert result["matched_versions"] == 0


def test_main_fail_closed_on_missing_gh_binary(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import scripts.deployed_digest as mod

    def _missing_gh(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("gh: command not found")

    monkeypatch.setattr(mod.subprocess, "run", _missing_gh)
    code = main(["--target", "stable", "--json"])
    assert code != 0
    out = capsys.readouterr().out
    assert '"digest": null' in out
