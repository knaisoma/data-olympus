"""Commit-pinned snapshot reader used by the write gate (#259)."""
from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

import data_olympus.write_gate as write_gate
from data_olympus.write_gate import CommitSnapshot, SnapshotUnavailable, validate_postimage

if TYPE_CHECKING:
    from pathlib import Path

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True,
                   capture_output=True, env=_ENV)
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_ENV)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True,
                   capture_output=True, env=_ENV)
    return repo


def _doc(doc_id: str, extra: str = "") -> str:
    return (f"---\nid: {doc_id}\ntype: standard\nstatus: active\ntier: T1\n{extra}---\n"
            f"# {doc_id}\n")


def test_snapshot_reads_ids_and_frontmatter_from_head(tmp_path) -> None:
    repo = _repo(tmp_path, {
        "universal/a.md": _doc("A"),
        "universal/weird name 'q' é.md": _doc("WEIRD"),
    })
    snap = CommitSnapshot(str(repo))

    ids = snap.path_to_effective_id()

    assert ids["universal/a.md"] == "A"
    assert ids["universal/weird name 'q' é.md"] == "WEIRD"
    assert snap.frontmatter("universal/a.md")["id"] == "A"
    assert snap.frontmatter("universal/missing.md") is None


def test_snapshot_handles_a_newline_in_a_committed_filename(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A")})
    odd = repo / "universal" / "line\nbreak.md"
    odd.write_text(_doc("NEWLINE"), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_ENV)
    subprocess.run(["git", "commit", "-m", "odd"], cwd=repo, check=True,
                   capture_output=True, env=_ENV)

    ids = CommitSnapshot(str(repo)).path_to_effective_id()

    assert ids["universal/line\nbreak.md"] == "NEWLINE"


def test_snapshot_ignores_dirty_worktree_content(tmp_path) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A")})
    (repo / "universal" / "a.md").write_text(_doc("CHANGED"), encoding="utf-8")

    assert CommitSnapshot(str(repo)).path_to_effective_id()["universal/a.md"] == "A"


def test_snapshot_uses_at_most_three_git_subprocesses(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path, {f"universal/d{i}.md": _doc(f"D{i}") for i in range(40)})
    calls: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(args, *a, **kw):  # noqa: ANN001, ANN002, ANN003, ANN202
        if args and args[0] == "git":
            calls.append(list(args))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(write_gate.subprocess, "run", counting_run)
    snap = CommitSnapshot(str(repo))
    snap.path_to_effective_id()
    snap.frontmatter("universal/d3.md")
    snap.paths()

    assert len(calls) <= 3, calls


def test_snapshot_failure_is_raised_and_cached(tmp_path, monkeypatch) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    snap = CommitSnapshot(str(not_a_repo))
    with pytest.raises(SnapshotUnavailable):
        snap.paths()
    calls: list[object] = []

    def must_not_run(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append((args, kwargs))
        raise AssertionError("git must not run again after a cached failure")

    monkeypatch.setattr(write_gate.subprocess, "run", must_not_run)
    with pytest.raises(SnapshotUnavailable):
        snap.path_to_effective_id()
    assert calls == []


def test_snapshot_missing_object_is_unavailable(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A")})
    real_run = subprocess.run

    def broken_cat_file(args, *a, **kw):  # noqa: ANN001, ANN002, ANN003, ANN202
        if args[:3] == ["git", "-C", str(repo)] and "cat-file" in args:
            return subprocess.CompletedProcess(args, 0, stdout=b"deadbeef missing\n", stderr=b"")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(write_gate.subprocess, "run", broken_cat_file)
    with pytest.raises(SnapshotUnavailable):
        CommitSnapshot(str(repo)).path_to_effective_id()


def test_duplicate_id_check_stays_fail_open_on_snapshot_failure(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path, {"universal/a.md": _doc("A")})

    def unavailable(_self):  # noqa: ANN001, ANN202
        raise SnapshotUnavailable("boom")

    monkeypatch.setattr(CommitSnapshot, "path_to_effective_id", unavailable)
    result = validate_postimage(
        target_path="universal/b.md", postimage=_doc("B"), idx=None,
        worktree_path=str(repo))

    assert result.ok, result.errors
