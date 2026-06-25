from pathlib import Path

from data_olympus.format import discover_bundle_files, lint_bundle
from data_olympus.format.validate import RESERVED


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


# ---------------------------------------------------------------------------
# File discovery (the count that proves the gate actually linted something)
# ---------------------------------------------------------------------------


def _repo_example_bundle() -> Path:
    """The shipped example-bundle that the CI dogfood step lints."""
    return Path(__file__).resolve().parents[1] / "example-bundle"


def test_discover_bundle_files_returns_every_concept_file(tmp_path: Path):
    (tmp_path / "a.md").write_text(_GOOD_FM, encoding="utf-8")
    nested = tmp_path / "universal" / "foundation"
    nested.mkdir(parents=True)
    (nested / "b.md").write_text(_GOOD_FM, encoding="utf-8")

    files = discover_bundle_files(tmp_path)

    assert set(files) == {tmp_path / "a.md", nested / "b.md"}


def test_discover_bundle_files_applies_skip_logic(tmp_path: Path):
    (tmp_path / "keep.md").write_text(_GOOD_FM, encoding="utf-8")
    (tmp_path / "README.md").write_text(_GOOD_FM, encoding="utf-8")  # root meta -> skipped
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "old.md").write_text(_GOOD_FM, encoding="utf-8")  # skip dir

    assert discover_bundle_files(tmp_path) == [tmp_path / "keep.md"]


def test_discover_bundle_files_empty_when_nothing_to_lint(tmp_path: Path):
    # Files exist on disk but every one is skipped: discovery must report zero
    # so the CLI can fail instead of false-greening.
    (tmp_path / "README.md").write_text(_GOOD_FM, encoding="utf-8")
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "CODEOWNERS.md").write_text(_GOOD_FM, encoding="utf-8")

    assert discover_bundle_files(tmp_path) == []


def test_discover_bundle_files_excludes_reserved_files(tmp_path: Path):
    # Reserved files are exempt from the concept schema and can never produce a
    # finding, so they must not be counted as lintable; a concept doc beside
    # them must still be discovered.
    (tmp_path / "index.md").write_text("# generated index\n", encoding="utf-8")
    (tmp_path / "log.md").write_text("# log\n", encoding="utf-8")
    (tmp_path / "STD-A.md").write_text(_GOOD_FM, encoding="utf-8")

    assert discover_bundle_files(tmp_path) == [tmp_path / "STD-A.md"]


def test_example_bundle_discovery_matches_concept_files():
    """Regression for the false-green CI gate: linting the shipped example-bundle
    must discover every concept file and nothing reserved. The bundle has no
    skip-dirs and no root-meta files, so discovery == all '*.md' minus reserved
    names. If the skip/path logic ever silently swallows the subtree, the count
    drops to zero and this fails loudly instead of passing as '0 errors across
    0 files'.
    """
    bundle = _repo_example_bundle()
    files = discover_bundle_files(bundle)
    expected = [p for p in sorted(bundle.rglob("*.md")) if p.name not in RESERVED]

    assert files == expected
    assert len(files) > 0, "example-bundle discovery matched zero concept files"
    assert all(p.name not in RESERVED for p in files)


def test_example_bundle_is_conformant():
    """The shipped example-bundle must stay schema-conformant (zero error-severity
    findings) so the dogfood gate is green for the right reason, not because the
    walk found nothing.
    """
    bundle = _repo_example_bundle()
    results = lint_bundle(bundle)
    errors = [f for findings in results.values() for f in findings if f.severity == "error"]
    assert errors == [], f"example-bundle is not conformant: {errors}"
