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


def test_lint_reports_number_of_files_linted(tmp_path, capsys):
    # The summary must state how many files were actually linted so that
    # "0 errors across 0 files" can never be confused with "nothing linted".
    _write(
        tmp_path / "universal/foundation/STD-U-001.md",
        "---\nid: STD-U-001\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n---\nok\n",
    )
    code = main(["lint", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "1 linted" in out


def test_lint_fails_when_no_files_to_lint(tmp_path, capsys):
    # An empty bundle must NOT false-green: the gate exits non-zero so a broken
    # walk surfaces as red instead of "0 errors across 0 files".
    code = main(["lint", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 1
    assert "no" in err.lower()


def test_lint_fails_when_every_file_is_skipped(tmp_path, capsys):
    # Files exist on disk but all are skipped (root meta + repo-meta dir). The
    # CLI must still fail rather than report a misleading clean pass.
    _write(tmp_path / "README.md", "# root meta, skipped\n")
    _write(tmp_path / ".github/CODEOWNERS.md", "# repo meta, skipped\n")
    code = main(["lint", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 1
    assert "no" in err.lower()


def test_lint_fails_when_only_reserved_files(tmp_path, capsys):
    # A bundle with only reserved files (index.md/log.md) validates zero
    # concepts; counting them as "linted" would let it false-green, so the gate
    # must fail instead.
    _write(tmp_path / "index.md", "# generated index\n")
    _write(tmp_path / "decisions/index.md", "# generated index\n")
    _write(tmp_path / "log.md", "# log\n")
    code = main(["lint", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 1
    assert "no" in err.lower()
