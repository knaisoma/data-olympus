from pathlib import Path

from data_olympus.format import lint_bundle


def test_lint_bundle_reports_only_nonconformant_files(tmp_path: Path):
    (tmp_path / "good.md").write_text(
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n---\nok\n",
        encoding="utf-8",
    )
    (tmp_path / "bad.md").write_text("---\ntitle: no required fields\n---\nbad\n", encoding="utf-8")
    (tmp_path / "index.md").write_text("# Index\n", encoding="utf-8")

    results = lint_bundle(tmp_path)

    assert (tmp_path / "bad.md") in results
    assert (tmp_path / "good.md") not in results
    assert (tmp_path / "index.md") not in results


def test_lint_bundle_includes_files_with_only_warnings(tmp_path):
    # All required fields present (no errors) but recommended fields missing (warnings).
    (tmp_path / "warnonly.md").write_text(
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n---\nbody\n",
        encoding="utf-8",
    )
    results = lint_bundle(tmp_path)
    assert (tmp_path / "warnonly.md") in results
    assert all(f.severity == "warning" for f in results[tmp_path / "warnonly.md"])


def test_lint_bundle_skips_dot_and_vendor_dirs(tmp_path: Path):
    hidden = tmp_path / ".git"
    hidden.mkdir()
    (hidden / "COMMIT_EDITMSG.md").write_text("---\n- bad\n---\n", encoding="utf-8")
    results = lint_bundle(tmp_path)
    assert all(".git" not in p.parts for p in results)
