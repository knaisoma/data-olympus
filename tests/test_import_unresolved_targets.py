"""Import exit codes for unresolved supersession targets (#259)."""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from data_olympus.cli.main import main

if TYPE_CHECKING:
    from pathlib import Path


def _adr(path: Path, number: int, title: str, status_line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {number}. {title}\n\n## Status\n\n{status_line}\n\n## Context\n\nctx\n\n"
        f"## Decision\n\ndecision\n\n## Consequences\n\ncons\n",
        encoding="utf-8")


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _adr_set(tmp_path: Path) -> Path:
    src = tmp_path / "adrs"
    _adr(src / "0004-use-postgres.md", 4, "Use Postgres", "Accepted\n\nSupersedes ADR-0003")
    return src


def test_import_with_absent_target_exits_1_and_reports_it(tmp_path, capsys) -> None:
    src = _adr_set(tmp_path)
    out = tmp_path / "out"
    code = main(["import", str(src), "--kind", "adr", "--tier", "T2", "-o", str(out), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["lint_clean"] is False
    assert any("ADR-0003" in f["message"] for f in report["lint"])
    drafts = list(out.rglob("*.md"))
    assert drafts, "drafts are still written"
    assert any("supersedes: ADR-0003" in p.read_text() for p in drafts), "fixture precondition"


def test_import_resolves_against_resolve_root(tmp_path) -> None:
    src = _adr_set(tmp_path)
    corpus = tmp_path / "corpus"
    (corpus / "decisions").mkdir(parents=True)
    (corpus / "decisions" / "adr-0003.md").write_text(
        "---\nid: ADR-0003\ntype: decision\nstatus: superseded\ntier: T2\n---\n# ADR-0003\n")
    code = main(["import", str(src), "--kind", "adr", "--tier", "T2",
                 "-o", str(tmp_path / "out"), "--resolve-root", str(corpus)])
    assert code == 0


def test_import_warn_exits_0(tmp_path) -> None:
    src = _adr_set(tmp_path)
    assert main(["import", str(src), "--kind", "adr", "--tier", "T2",
                 "-o", str(tmp_path / "out"), "--unresolved-targets", "warn"]) == 0


def test_invalid_resolve_root_changes_nothing(tmp_path, capsys) -> None:
    src = _adr_set(tmp_path)
    out = tmp_path / "out"
    assert main(["import", str(src), "--kind", "adr", "--tier", "T2", "-o", str(out),
                 "--unresolved-targets", "warn"]) == 0
    before = _tree_digest(out)
    regular = tmp_path / "file.txt"
    regular.write_text("x")
    for bad in (str(tmp_path / "missing"), str(regular)):
        for extra in ([], ["--force"]):
            code = main(["import", str(src), "--kind", "adr", "--tier", "T2", "-o", str(out),
                         "--resolve-root", bad, *extra])
            assert code == 1
            assert _tree_digest(out) == before
    assert "resolve-root" in capsys.readouterr().err


def test_import_with_malformed_file_under_resolve_root_still_reports(tmp_path, capsys) -> None:
    src = _adr_set(tmp_path)
    corpus = tmp_path / "corpus"
    (corpus / "decisions").mkdir(parents=True)
    (corpus / "decisions" / "adr-0003.md").write_text(
        "---\nid: ADR-0003\ntype: decision\nstatus: superseded\ntier: T2\n---\n# ADR-0003\n")
    (corpus / "decisions" / "broken.md").write_text("---\nid: [bad\n---\n")
    code = main(["import", str(src), "--kind", "adr", "--tier", "T2",
                 "-o", str(tmp_path / "out"), "--resolve-root", str(corpus), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["lint_clean"] is True
