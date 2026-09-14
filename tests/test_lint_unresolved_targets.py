"""Unresolved supersession targets in lint (#259)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.cli.main import main
from data_olympus.format import collect_ids, lint_files

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, doc_id: str, extra: str = "", status: str = "active") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {doc_id}\ntype: standard\nstatus: {status}\ntier: T1\n{extra}---\n# {doc_id}\n",
        encoding="utf-8")
    return path


def _severities(results, path, field):  # noqa: ANN001, ANN202
    return [f.severity for f in results.get(path, []) if f.field == field]


def test_unresolved_superseded_by_is_error(tmp_path) -> None:
    a = _write(tmp_path / "a.md", "A", "superseded_by: GHOST\n", status="superseded")
    assert "error" in _severities(lint_files([a]), a, "superseded_by")


def test_draft_target_resolves(tmp_path) -> None:
    a = _write(tmp_path / "a.md", "A", status="draft")
    b = _write(tmp_path / "b.md", "B", "supersedes: A\n")
    assert "error" not in _severities(lint_files([a, b]), b, "supersedes")


def test_resolving_path_shaped_target_stays_warning(tmp_path) -> None:
    a = _write(tmp_path / "x" / "a.md", "x/a.md")
    b = _write(tmp_path / "b.md", "B", "supersedes: x/a.md\n")
    results = lint_files([a, b])
    supersedes = [f for f in results.get(b, []) if f.field == "supersedes"]
    assert "error" not in [f.severity for f in supersedes]
    assert any("looks like a file path" in f.message for f in supersedes)


def test_warn_downgrades_only_unresolved_targets(tmp_path) -> None:
    a = _write(tmp_path / "a.md", "A", "supersedes: [GHOST, A]\n")
    results = lint_files([a], unresolved_severity="warn")
    severities = _severities(results, a, "supersedes")
    assert "warning" in severities  # GHOST
    assert "error" in severities  # self-supersession stays an error


def test_resolve_ids_supply_existence_only(tmp_path) -> None:
    b = _write(tmp_path / "sub" / "b.md", "B", "supersedes: OUTSIDE\n")
    results = lint_files([b], resolve_ids={"OUTSIDE"})
    assert _severities(results, b, "supersedes") == []


def test_collect_ids_reads_authored_ids(tmp_path) -> None:
    _write(tmp_path / "a.md", "A")
    _write(tmp_path / "deep" / "b.md", "B")
    assert {"A", "B"} <= collect_ids(tmp_path)


def test_cli_subtree_exit_codes(tmp_path, capsys) -> None:
    _write(tmp_path / "outside" / "a.md", "OUTSIDE")
    _write(tmp_path / "sub" / "b.md", "B", "supersedes: OUTSIDE\n")
    sub = str(tmp_path / "sub")

    assert main(["lint", sub]) == 1
    assert main(["lint", sub, "--resolve-root", str(tmp_path)]) == 0
    assert main(["lint", sub, "--unresolved-targets", "warn"]) == 0
    out = capsys.readouterr().out
    assert "warning: supersedes" in out


def test_cli_invalid_resolve_root_exits_1(tmp_path, capsys) -> None:
    _write(tmp_path / "sub" / "b.md", "B")
    regular = tmp_path / "file.txt"
    regular.write_text("x")
    assert main(["lint", str(tmp_path / "sub"), "--resolve-root", str(tmp_path / "missing")]) == 1
    assert main(["lint", str(tmp_path / "sub"), "--resolve-root", str(regular)]) == 1
    assert "resolve-root" in capsys.readouterr().err


def test_boundary_relationships_unchanged_by_resolve_root(tmp_path) -> None:
    outside = _write(tmp_path / "out" / "o.md", "O", "supersedes: B\n")
    b = _write(tmp_path / "sub" / "b.md", "B", "superseded_by: O\n", status="superseded")
    plain = lint_files([b], resolve_ids={"O"})
    assert _severities(plain, b, "superseded_by") == _severities(
        lint_files([b], resolve_ids=collect_ids(outside.parent)), b, "superseded_by")
