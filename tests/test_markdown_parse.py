"""Tests for markdown front-matter parsing."""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from data_olympus.markdown_parse import ParsedDoc, parse_file

if TYPE_CHECKING:
    from pathlib import Path


def test_parse_file_with_front_matter(tmp_kb: Path) -> None:
    path = tmp_kb / "universal" / "foundation" / "STD-U-001-test-policy.md"
    doc = parse_file(path)
    assert isinstance(doc, ParsedDoc)
    assert doc.id == "STD-U-001"
    assert doc.tier == "T1"
    assert doc.category == "foundation"
    assert doc.tags == ["policy", "test"]
    assert doc.title == "Test Policy"
    assert "worktree" in doc.body
    assert doc.path == path


def test_parse_file_without_front_matter(tmp_path: Path) -> None:
    """A markdown file lacking front matter still parses; metadata fields are empty."""
    p = tmp_path / "no_fm.md"
    p.write_text("# Heading\n\nBody only.\n")
    doc = parse_file(p)
    assert doc.id == ""
    assert doc.title == ""
    assert doc.tags == []
    assert "Body only" in doc.body


def test_parse_file_malformed_yaml_is_lenient(tmp_path: Path) -> None:
    """Malformed front matter is treated as no-front-matter (lenient)."""
    p = tmp_path / "bad.md"
    p.write_text("---\nthis is: not: valid: yaml: at: all\n---\n# H\n")
    doc = parse_file(p)
    # Should not raise; should return a ParsedDoc with empty metadata.
    assert doc.id == ""
    assert doc.body  # body is still extracted (may or may not include the fm block)


def test_parse_file_nonexistent_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_file(tmp_path / "nope.md")


def test_parse_git_remote_url_present(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_text(
        "---\n"
        "id: PROJECT-example-project\n"
        "tier: T3\n"
        "git_remote_url: git@github.com:example-org/example-project.git\n"
        "---\n"
        "# Body\n"
    )
    doc = parse_file(p)
    assert doc.git_remote_url == "git@github.com:example-org/example-project.git"


def test_parse_git_remote_url_absent_defaults_to_none(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_text("---\nid: X\n---\n# Body\n")
    doc = parse_file(p)
    assert doc.git_remote_url is None


def test_parse_file_extracts_status_and_type(tmp_path: Path) -> None:
    from data_olympus.markdown_parse import parse_file
    p = tmp_path / "x.md"
    p.write_text(
        "---\nid: STD-1\ntier: T1\ntype: standard\nstatus: active\n---\n# Body\n"
    )
    doc = parse_file(p)
    assert doc.status == "active"
    assert doc.doc_type == "standard"


def test_parse_file_status_and_type_default_empty(tmp_path: Path) -> None:
    from data_olympus.markdown_parse import parse_file
    p = tmp_path / "y.md"
    p.write_text("# No front matter\n")
    doc = parse_file(p)
    assert doc.status == ""
    assert doc.doc_type == ""


def test_parse_file_extracts_applies_when_list(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_text(
        "---\nid: STD-1\ntier: T1\napplies_when:\n  - openpyxl\n  - insert_cols\n"
        "description: Use xlsxwriter for new Excel files.\n---\n# Body\n"
    )
    doc = parse_file(p)
    assert doc.applies_when == ["openpyxl", "insert_cols"]
    assert doc.description == "Use xlsxwriter for new Excel files."


def test_parse_file_applies_when_inline_list(tmp_path: Path) -> None:
    p = tmp_path / "y.md"
    p.write_text("---\nid: STD-2\ntier: T1\napplies_when: [excel, xlsx]\n---\n# B\n")
    doc = parse_file(p)
    assert doc.applies_when == ["excel", "xlsx"]


def test_parse_file_applies_when_and_description_default_empty(tmp_path: Path) -> None:
    p = tmp_path / "z.md"
    p.write_text("---\nid: STD-3\ntier: T1\n---\n# B\n")
    doc = parse_file(p)
    assert doc.applies_when == []
    assert doc.description == ""


def test_parse_file_multiline_description(tmp_path: Path) -> None:
    p = tmp_path / "m.md"
    p.write_text(
        "---\nid: STD-4\ntier: T1\ndescription: >\n  First line\n  second line.\n---\n# B\n"
    )
    doc = parse_file(p)
    assert "First line second line." in doc.description


# ---------------------------------------------------------------------------
# validity frontmatter (issue #107)
# ---------------------------------------------------------------------------


def test_parse_file_extracts_validity_fields(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_text(
        "---\nid: STD-5\ntier: T1\n"
        "validity:\n"
        "  valid_from: 2026-01-01\n"
        "  valid_until: 2026-12-31\n"
        "  last_verified: 2026-06-01\n"
        "  recheck_by: 2026-09-01\n"
        "  verification_source: manual review\n"
        "---\n# B\n"
    )
    doc = parse_file(p)
    assert doc.valid_from == "2026-01-01"
    assert doc.valid_until == "2026-12-31"
    assert doc.last_verified == "2026-06-01"
    assert doc.recheck_by == "2026-09-01"
    assert doc.verification_source == "manual review"
    assert doc.validity_malformed is False


def test_parse_file_validity_absent_defaults_empty(tmp_path: Path) -> None:
    p = tmp_path / "y.md"
    p.write_text("---\nid: STD-6\ntier: T1\n---\n# B\n")
    doc = parse_file(p)
    assert doc.valid_from == ""
    assert doc.valid_until == ""
    assert doc.last_verified == ""
    assert doc.recheck_by == ""
    assert doc.verification_source == ""
    assert doc.validity_malformed is False


def test_parse_file_validity_datetime_normalizes_to_date(tmp_path: Path) -> None:
    p = tmp_path / "z.md"
    p.write_text(
        "---\nid: STD-7\ntier: T1\n"
        "validity:\n"
        "  valid_until: 2026-06-01T23:00:00+02:00\n"
        "---\n# B\n"
    )
    doc = parse_file(p)
    assert doc.valid_until == "2026-06-01"


def test_parse_file_validity_zulu_datetime_normalizes_to_date(tmp_path: Path) -> None:
    p = tmp_path / "z2.md"
    p.write_text(
        "---\nid: STD-8\ntier: T1\n"
        "validity:\n"
        "  valid_until: '2026-06-01T00:00:00Z'\n"
        "---\n# B\n"
    )
    doc = parse_file(p)
    assert doc.valid_until == "2026-06-01"


def test_parse_file_malformed_validity_treated_as_absent(tmp_path: Path) -> None:
    """A malformed date anywhere in ``validity`` fails the WHOLE block open (the
    doc is treated as having no validity at all), and is flagged for a caller
    to warn/count, rather than silently indexing a partially-parsed block."""
    p = tmp_path / "bad.md"
    p.write_text(
        "---\nid: STD-9\ntier: T1\n"
        "validity:\n"
        "  valid_from: 2026-01-01\n"
        "  valid_until: not-a-real-date\n"
        "---\n# B\n"
    )
    doc = parse_file(p)
    assert doc.valid_from == ""
    assert doc.valid_until == ""
    assert doc.validity_malformed is True


def test_parse_file_validity_not_a_mapping_is_malformed(tmp_path: Path) -> None:
    p = tmp_path / "bad2.md"
    p.write_text("---\nid: STD-10\ntier: T1\nvalidity: not-a-mapping\n---\n# B\n")
    doc = parse_file(p)
    assert doc.validity_malformed is True
    assert doc.valid_from == ""
    assert doc.valid_until == ""
