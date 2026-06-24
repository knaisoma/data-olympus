from pathlib import Path

from data_olympus.cli.main import main


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_lint_returns_zero_on_conformant_bundle(tmp_path, capsys):
    _write(
        tmp_path / "universal/foundation/STD-U-001.md",
        "---\nid: STD-U-001\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n---\nok\n",
    )
    code = main(["lint", str(tmp_path)])
    assert code == 0
    assert "0 errors" in capsys.readouterr().out


def test_lint_returns_one_and_reports_errors(tmp_path, capsys):
    _write(tmp_path / "bad.md", "---\ntitle: missing required\n---\nbad\n")
    code = main(["lint", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "bad.md" in out
    assert "missing required field 'id'" in out
