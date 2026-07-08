"""Tests for `data-olympus validity-report` (issue #107): lists expired docs
and docs expiring within N days, scanning a bundle directory directly (no
index/server required, mirroring `lint`'s bundle-walk model)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.cli.main import main

if TYPE_CHECKING:
    from pathlib import Path


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _doc(id_: str, *, status: str = "active", validity: str = "") -> str:
    return (
        f"---\nid: {id_}\ntype: standard\nstatus: {status}\ntier: T1\n"
        f"title: {id_}\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        f"{validity}---\nbody\n"
    )


def test_validity_report_lists_expired_docs(tmp_path, capsys):
    _write(
        tmp_path / "expired.md",
        _doc("DOC-EXPIRED", validity="validity:\n  valid_until: 2020-01-01\n"),
    )
    _write(tmp_path / "fresh.md", _doc("DOC-FRESH"))

    code = main(["validity-report", str(tmp_path), "--today", "2026-06-01"])

    out = capsys.readouterr().out
    assert code == 0
    assert "DOC-EXPIRED" in out
    assert "DOC-FRESH" not in out


def test_validity_report_lists_expiring_within_days(tmp_path, capsys):
    _write(
        tmp_path / "soon.md",
        _doc("DOC-SOON", validity="validity:\n  valid_until: 2026-06-10\n"),
    )
    _write(
        tmp_path / "far.md",
        _doc("DOC-FAR", validity="validity:\n  valid_until: 2027-01-01\n"),
    )

    code = main([
        "validity-report", str(tmp_path),
        "--today", "2026-06-01", "--expiring-within", "30",
    ])

    out = capsys.readouterr().out
    assert code == 0
    assert "DOC-SOON" in out
    assert "DOC-FAR" not in out


def test_validity_report_no_findings_is_clean(tmp_path, capsys):
    _write(tmp_path / "fresh.md", _doc("DOC-FRESH"))

    code = main(["validity-report", str(tmp_path), "--today", "2026-06-01"])

    out = capsys.readouterr().out
    assert code == 0
    assert "0 expired" in out


def test_validity_report_defaults_to_current_directory(tmp_path, capsys, monkeypatch):
    _write(
        tmp_path / "expired.md",
        _doc("DOC-EXPIRED", validity="validity:\n  valid_until: 2020-01-01\n"),
    )
    monkeypatch.chdir(tmp_path)

    code = main(["validity-report", "--today", "2026-06-01"])

    out = capsys.readouterr().out
    assert code == 0
    assert "DOC-EXPIRED" in out
