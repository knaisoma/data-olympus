"""Write-gate supersession rule (#259)."""
from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from data_olympus.write_gate import (
    SNAPSHOT_DEPENDENT_CODES,
    CommitSnapshot,
    SnapshotUnavailable,
    validate_postimage,
)

if TYPE_CHECKING:
    from pathlib import Path

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def _commit(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_ENV)
    subprocess.run(["git", "commit", "-m", "c", "--allow-empty"], cwd=repo, check=True,
                   capture_output=True, env=_ENV)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True,
                   capture_output=True, env=_ENV)
    _commit(repo, files)
    return repo


def _doc(doc_id: str, extra: str = "", status: str = "active") -> str:
    return (f"---\nid: {doc_id}\ntype: standard\nstatus: {status}\ntier: T1\n{extra}---\n"
            f"# {doc_id}\n")


def _codes(result) -> list[str]:  # noqa: ANN001
    return [e["code"] for e in result.errors]


def _validate(repo: Path, path: str, text: str, **kw):  # noqa: ANN003, ANN202
    return validate_postimage(target_path=path, postimage=text, idx=None,
                              worktree_path=str(repo), **kw)


@pytest.mark.parametrize(("field", "code"), [
    ("supersedes", "unresolved_supersedes_target"),
    ("superseded_by", "unresolved_superseded_by_target"),
])
def test_new_unresolved_target_is_rejected(tmp_path, field, code) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A")})

    result = _validate(repo, "universal/b.md", _doc("B", f"{field}: GHOST\n"))

    assert code in _codes(result)
    message = next(e["message"] for e in result.errors if e["code"] == code)
    assert message.startswith(f"{code}: ") and "GHOST" in message


def test_draft_target_resolves(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A", status="draft")})
    assert _validate(repo, "universal/b.md", _doc("B", "supersedes: A\n")).ok


def test_committed_but_unindexed_target_resolves(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A")})
    assert _validate(repo, "universal/b.md", _doc("B", "supersedes: A\n")).ok  # idx=None


def test_removed_and_renamed_ids_do_not_resolve(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("OLD"), "universal/c.md": _doc("C")})
    _commit(repo, {"universal/a.md": _doc("NEW")})  # id changed at an unchanged path
    (repo / "universal" / "c.md").unlink()
    subprocess.run(["git", "commit", "-am", "rm"], cwd=repo, check=True,
                   capture_output=True, env=_ENV)

    assert "unresolved_supersedes_target" in _codes(
        _validate(repo, "universal/b.md", _doc("B", "supersedes: OLD\n")))
    assert "unresolved_supersedes_target" in _codes(
        _validate(repo, "universal/b.md", _doc("B", "supersedes: C\n")))
    assert _validate(repo, "universal/b.md", _doc("B", "supersedes: NEW\n")).ok


def test_duplicate_owner_keeps_an_id_resolvable(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("OLD"), "universal/b.md": _doc("OLD")})
    snap = CommitSnapshot(str(repo))
    postimage = _doc("NEW", "supersedes: OLD\n")

    result = validate_postimage(target_path="universal/a.md", postimage=postimage,
                                idx=None, snapshot=snap)

    assert "unresolved_supersedes_target" not in _codes(result)


def test_id_removed_by_the_same_write_does_not_resolve(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("OLD")})

    result = _validate(repo, "universal/a.md", _doc("NEW", "supersedes: OLD\n"))

    assert "unresolved_supersedes_target" in _codes(result)


def test_transaction_files_resolve_and_renames_apply(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("OLD")})
    snap = CommitSnapshot(str(repo))
    transaction = {
        "universal/a.md": _doc("RENAMED"),
        "universal/x.md": _doc("X"),
        "universal/y.md": _doc("Y", "supersedes: X\n"),
        "universal/z.md": _doc("Z", "supersedes: OLD\n"),
    }

    def check(path: str) -> list[str]:
        return _codes(validate_postimage(target_path=path, postimage=transaction[path],
                                         idx=None, snapshot=snap, transaction=transaction))

    assert "unresolved_supersedes_target" not in check("universal/y.md")
    assert "unresolved_supersedes_target" in check("universal/z.md")


@pytest.mark.parametrize("value", ['" A "', '["A ", B]'])
def test_whitespace_is_part_of_the_reference(tmp_path, value) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A"), "universal/c.md": _doc("B")})
    assert "unresolved_supersedes_target" in _codes(
        _validate(repo, "universal/b2.md", _doc("B2", f"supersedes: {value}\n")))


def test_whitespace_is_part_of_the_superseded_by_reference(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A")})
    assert "unresolved_superseded_by_target" in _codes(
        _validate(repo, "universal/b.md", _doc("B", 'superseded_by: " A"\n')))


def test_legacy_dangling_edge_on_unrelated_edit_is_accepted(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A", "supersedes: [GHOST]\n")})
    edited = _doc("A", "supersedes: GHOST\n").replace("# A\n", "# A\nMore body.\n")
    assert _validate(repo, "universal/a.md", edited).ok


def test_adding_a_second_dangling_target_names_only_the_new_one(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A", "supersedes: [GHOST]\n")})
    result = _validate(repo, "universal/a.md", _doc("A", "supersedes: [GHOST, PHANTOM]\n"))
    messages = [e["message"] for e in result.errors]
    assert any("PHANTOM" in m for m in messages)
    assert not any("GHOST" in m for m in messages)


def test_moving_a_target_between_fields_is_checked(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A", "supersedes: GHOST\n")})
    assert "unresolved_superseded_by_target" in _codes(
        _validate(repo, "universal/a.md", _doc("A", "superseded_by: GHOST\n")))


@pytest.mark.parametrize(("line", "code"), [
    ("supersedes: 123\n", "malformed_supersedes"),
    ("supersedes: [123, A]\n", "malformed_supersedes"),
    ("supersedes: {a: b}\n", "malformed_supersedes"),
    ("superseded_by: [A]\n", "malformed_superseded_by"),
])
def test_new_malformed_values_are_rejected(tmp_path, line, code) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A")})
    assert code in _codes(_validate(repo, "universal/b.md", _doc("B", line)))


@pytest.mark.parametrize(("before", "after", "ok"), [
    ("supersedes: 123\n", "supersedes: 123\n", True),
    ("supersedes: {a: 1, b: 2}\n", "supersedes: {b: 2, a: 1}\n", True),
    ("supersedes: 123\n", "", True),
    ("supersedes: 123\n", "supersedes: null\n", True),
    ("supersedes: 1\n", "supersedes: true\n", False),
    ("supersedes: 1\n", "supersedes: 1.0\n", False),
    ("supersedes: [1, A]\n", "supersedes: [true, A]\n", False),
    ("supersedes: [[1]]\n", "supersedes: [[1.0]]\n", False),
])
def test_malformed_grandfathering_uses_type_exact_equality(tmp_path, before, after, ok) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A", before), "universal/t.md": _doc("T")})
    result = _validate(repo, "universal/a.md", _doc("A", after))
    assert ("malformed_supersedes" not in _codes(result)) is ok


def test_snapshot_failure_rejects_as_unverifiable(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A")})

    def unavailable(_self, _path):  # noqa: ANN001, ANN202
        raise SnapshotUnavailable("boom")

    monkeypatch.setattr(CommitSnapshot, "frontmatter", unavailable)
    assert "unresolved_target_unverifiable" in _codes(
        _validate(repo, "universal/b.md", _doc("B", "supersedes: A\n")))


def test_documents_without_relationships_never_open_the_snapshot(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A")})

    def boom(_self, _path):  # noqa: ANN001, ANN202
        raise AssertionError("snapshot must not be read for supersession")

    monkeypatch.setattr(CommitSnapshot, "frontmatter", boom)
    assert _validate(repo, "universal/b.md", _doc("B")).ok


def test_credential_shaped_target_is_redacted(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A")})
    fake = "ghp_" + "FAKE" * 9
    result = _validate(repo, "universal/b.md", _doc("B", f"supersedes: {fake}\n"))
    messages = " ".join(e["message"] for e in result.errors)
    assert "unresolved_supersedes_target" in messages
    assert fake not in messages and "redacted" in messages


def test_index_only_calls_skip_the_rule_and_codes_are_prediction_exempt() -> None:
    result = validate_postimage(target_path="universal/b.md",
                                postimage=_doc("B", "supersedes: GHOST\n"), idx=None)
    assert result.ok
    assert {"missing_status", "unresolved_supersedes_target",
            "unresolved_superseded_by_target", "malformed_supersedes",
            "malformed_superseded_by", "unresolved_target_unverifiable"} <= SNAPSHOT_DEPENDENT_CODES


@pytest.mark.parametrize(("before", "after"), [
    ("supersedes: {1: x}\n", "supersedes: {true: x}\n"),
    ("supersedes: {1: x}\n", "supersedes: {1.0: x}\n"),
])
def test_mapping_keys_are_compared_type_exactly(tmp_path, before, after) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A", before)})
    assert "malformed_supersedes" in _codes(_validate(repo, "universal/a.md", _doc("A", after)))


def test_unchanged_self_referencing_malformed_value_is_accepted(tmp_path) -> None:
    value = "supersedes: &x [*x]\n"
    repo = _repo(tmp_path, {"universal/a.md": _doc("A", value)})
    result = _validate(repo, "universal/a.md", _doc("A", value).replace("# A\n", "# A\nMore.\n"))
    assert "malformed_supersedes" not in _codes(result)


@pytest.mark.parametrize(("line", "code"), [
    ('supersedes: " "\n', "malformed_supersedes"),
    ('supersedes: ""\n', "malformed_supersedes"),
    ('supersedes: [" "]\n', "malformed_supersedes"),
    ('superseded_by: " "\n', "malformed_superseded_by"),
    ('superseded_by: ""\n', "malformed_superseded_by"),
])
def test_blank_targets_are_malformed_in_both_shapes(tmp_path, line, code) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A")})
    assert code in _codes(_validate(repo, "universal/b.md", _doc("B", line)))


@pytest.mark.parametrize(("before", "after"), [
    ("supersedes: !!set {1: null}\n", "supersedes: !!set {true: null}\n"),
    ("supersedes: !!set {1: null}\n", "supersedes: !!set {1.0: null}\n"),
    ("supersedes: !!pairs [{1: x}]\n", "supersedes: !!pairs [{true: x}]\n"),
    ("supersedes: !!omap [{1: x}]\n", "supersedes: !!omap [{1.0: x}]\n"),
])
def test_sets_pairs_and_omaps_are_compared_type_exactly(tmp_path, before, after) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A", before)})
    assert "malformed_supersedes" in _codes(_validate(repo, "universal/a.md", _doc("A", after)))


@pytest.mark.parametrize("value", [
    "supersedes: .nan\n",
    "supersedes: [.nan, A]\n",
    "supersedes: !!set {1: null}\n",
    "supersedes: !!omap [{1: x}]\n",
])
def test_unchanged_unusual_malformed_values_are_accepted(tmp_path, value) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A", value)})
    edited = _doc("A", value).replace("# A\n", "# A\nMore.\n")
    assert "malformed_supersedes" not in _codes(_validate(repo, "universal/a.md", edited))
