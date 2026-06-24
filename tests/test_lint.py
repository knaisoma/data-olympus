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


# ---------------------------------------------------------------------------
# Repo-meta directory and root-level file skipping
# ---------------------------------------------------------------------------

_BAD_MD = "# no frontmatter at all\n"
_GOOD_FM = (
    "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
    "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n---\nok\n"
)


def test_lint_bundle_skips_archive_dir(tmp_path: Path):
    """Files under 'archive/' are not concept docs and must produce zero findings."""
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "old.md").write_text(_BAD_MD, encoding="utf-8")
    results = lint_bundle(tmp_path)
    assert (archive / "old.md") not in results


def test_lint_bundle_skips_dot_github_dir(tmp_path: Path):
    """Files under '.github/' are repo-meta and must produce zero findings."""
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "CODEOWNERS.md").write_text(_BAD_MD, encoding="utf-8")
    results = lint_bundle(tmp_path)
    assert (gh / "CODEOWNERS.md") not in results


def test_lint_bundle_skips_root_meta_files(tmp_path: Path):
    """Well-known root-level repo-meta files must be skipped entirely."""
    root_meta_names = [
        "README.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "NOTICE.md",
        "LICENSE.md",
        "AGENTS.md",
        "CLAUDE.md",
        "GEMINI.md",
    ]
    for name in root_meta_names:
        (tmp_path / name).write_text(_BAD_MD, encoding="utf-8")

    results = lint_bundle(tmp_path)

    for name in root_meta_names:
        assert (tmp_path / name) not in results, f"Expected {name} to be skipped at root"


def test_lint_bundle_root_readme_skipped_but_nested_readme_still_flagged(tmp_path: Path):
    """Root README.md is skipped; a nested projects/p/README.md without frontmatter is flagged."""
    # Root-level README: no frontmatter -> should be SKIPPED
    (tmp_path / "README.md").write_text(_BAD_MD, encoding="utf-8")

    # Nested README in a project dir: no frontmatter -> must still be FLAGGED
    nested = tmp_path / "projects" / "p"
    nested.mkdir(parents=True)
    (nested / "README.md").write_text(_BAD_MD, encoding="utf-8")

    results = lint_bundle(tmp_path)

    assert (tmp_path / "README.md") not in results, "Root README.md must be skipped"
    assert (nested / "README.md") in results, "Nested projects/p/README.md must still be flagged"


def test_lint_bundle_normal_nonconformant_concept_still_flagged(tmp_path: Path):
    """A non-conformant concept doc outside any skip path is still reported as an error."""
    concept_dir = tmp_path / "universal" / "foundation"
    concept_dir.mkdir(parents=True)
    (concept_dir / "STD-MISSING.md").write_text("# no frontmatter\n", encoding="utf-8")

    results = lint_bundle(tmp_path)

    assert (concept_dir / "STD-MISSING.md") in results
    errors = [f for f in results[concept_dir / "STD-MISSING.md"] if f.severity == "error"]
    assert errors, "Expected at least one error finding for missing required fields"


def test_lint_bundle_archive_and_github_zero_findings_combined(tmp_path: Path):
    """archive/, _archive/, and .github/ together contribute zero findings."""
    for dir_name in ("archive", "_archive", ".github"):
        d = tmp_path / dir_name
        d.mkdir()
        (d / "x.md").write_text(_BAD_MD, encoding="utf-8")

    results = lint_bundle(tmp_path)

    skipped_dirs = {"archive", "_archive", ".github"}
    for path in results:
        for part in path.parts:
            assert part not in skipped_dirs, f"Unexpected finding under skipped dir: {path}"
