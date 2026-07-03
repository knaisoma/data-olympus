"""Tests for the doc-consistency CI guard (scripts/check_doc_consistency.py).

Covers both the pure extraction/diff logic (no filesystem) and an
end-to-end pass/fail run against a scratch root with real SPEC.md /
docs/adoption.md text, so a regression in the sentence-boundary regex or the
diffing logic is caught even if the two never disagree in practice.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_doc_consistency import (
    ParseError,
    _diff_message,
    _extract_enum_occurrences,
    _extract_reserved,
    check_doc_consistency,
)

# --- _extract_enum_occurrences -----------------------------------------------


def test_extract_enum_single_line() -> None:
    text = "- `type`: one of `standard`, `decision`, `workflow`.\nMore prose."
    occurrences = _extract_enum_occurrences(text, field="type")
    assert len(occurrences) == 1
    line_no, values = occurrences[0]
    assert line_no == 1
    assert values == {"standard", "decision", "workflow"}


def test_extract_enum_wraps_across_lines() -> None:
    text = (
        "- `type`: one of `standard`, `decision`, `workflow`, `project`,\n"
        "  `memory`, `reference`.\n"
    )
    occurrences = _extract_enum_occurrences(text, field="type")
    assert len(occurrences) == 1
    _, values = occurrences[0]
    assert values == {"standard", "decision", "workflow", "project", "memory", "reference"}


def test_extract_enum_tolerates_oxford_or() -> None:
    text = "`status`: one of `draft`, `active`, or `deprecated`.\n"
    _, values = _extract_enum_occurrences(text, field="status")[0]
    assert values == {"draft", "active", "deprecated"}


def test_extract_enum_does_not_stop_at_period_inside_backticks() -> None:
    # A period embedded in a backtick-quoted value (e.g. a filename) must not
    # be mistaken for the sentence-ending period.
    text = "`type`: one of `index.md`, `standard`. Trailing prose.\n"
    _, values = _extract_enum_occurrences(text, field="type")[0]
    assert values == {"index.md", "standard"}


def test_extract_enum_finds_multiple_occurrences() -> None:
    text = (
        "- `type`: controlled vocabulary: `standard`, `decision`.\n"
        "...\n"
        "   - `type`: one of `standard`, `decision`\n"
    )
    # Second occurrence has no terminating period at all before EOF; give it
    # one so both occurrences parse (mirrors the real SPEC.md fix).
    text = text.rstrip("\n") + ".\n"
    occurrences = _extract_enum_occurrences(text, field="type")
    assert len(occurrences) == 2
    assert occurrences[0][1] == {"standard", "decision"}
    assert occurrences[1][1] == {"standard", "decision"}


def test_extract_enum_raises_when_marker_absent() -> None:
    with pytest.raises(ParseError, match="no '`type`:' marker found"):
        _extract_enum_occurrences("nothing relevant here", field="type")


def test_extract_enum_raises_when_no_values_follow() -> None:
    with pytest.raises(ParseError, match="no backtick-quoted values followed"):
        _extract_enum_occurrences("`type`: nothing quoted here.\n", field="type")


def test_extract_enum_raises_when_no_sentence_end() -> None:
    with pytest.raises(ParseError, match="no sentence-ending"):
        _extract_enum_occurrences("`type`: one of `standard`, `decision`", field="type")


# --- _extract_reserved --------------------------------------------------------


def test_extract_reserved() -> None:
    text = (
        "**Reserved filenames.** The filenames `index.md`, `log.md`, and "
        "`template.md` are reserved in every directory. More prose.\n"
    )
    line_no, values = _extract_reserved(text)
    assert line_no == 1
    assert values == {"index.md", "log.md", "template.md"}


def test_extract_reserved_raises_when_sentence_absent() -> None:
    with pytest.raises(ParseError, match="no 'Reserved filenames.' sentence found"):
        _extract_reserved("no mention of reserved anything here.\n")


# --- _diff_message -------------------------------------------------------------


def test_diff_message_none_when_in_sync() -> None:
    msg = _diff_message(
        source="SPEC.md", field="type", line_no=1,
        extracted={"a", "b"}, canonical={"a", "b"},
    )
    assert msg is None


def test_diff_message_reports_missing_and_extra() -> None:
    msg = _diff_message(
        source="SPEC.md", field="type", line_no=42,
        extracted={"a", "c"}, canonical={"a", "b"},
    )
    assert msg is not None
    assert "SPEC.md:42" in msg
    assert "'b'" in msg  # missing from doc
    assert "'c'" in msg  # stale in doc


# --- check_doc_consistency (end-to-end against a scratch root) --------------


def _write_bundle(tmp_path: Path, *, spec_type_line: str, adoption_type_line: str) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "SPEC.md").write_text(
        "## 4.2 Governance extensions\n\n"
        f"- {spec_type_line}\n"
        "- `status`: lifecycle state: `draft`, `active`, `deprecated`, `superseded`, "
        "`proposed`, `accepted`, `rejected`. More prose.\n\n"
        "**Reserved filenames.** The filenames `index.md`, `log.md`, and "
        "`template.md` are reserved in every directory. More prose.\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "adoption.md").write_text(
        f"- {adoption_type_line}\n"
        "- `status`: one of `draft`, `active`, `deprecated`, `superseded`,\n"
        "  `proposed`, `accepted`, `rejected`.\n",
        encoding="utf-8",
    )
    return tmp_path


_IN_SYNC_TYPE_LINE = (
    "`type`: controlled vocabulary: `standard`, `decision`, `workflow`, "
    "`project`, `memory`, `reference`. Unknown values are an error."
)


def test_check_doc_consistency_passes_when_in_sync(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        spec_type_line=_IN_SYNC_TYPE_LINE,
        adoption_type_line=(
            "`type`: one of `standard`, `decision`, `workflow`, `project`, "
            "`memory`, `reference`."
        ),
    )
    assert check_doc_consistency(tmp_path) == []


def test_check_doc_consistency_detects_drift_in_spec(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        # Drops 'reference', adds a stale 'obsolete-type'.
        spec_type_line=(
            "`type`: controlled vocabulary: `standard`, `decision`, `workflow`, "
            "`project`, `memory`, `obsolete-type`. Unknown values are an error."
        ),
        adoption_type_line=(
            "`type`: one of `standard`, `decision`, `workflow`, `project`, "
            "`memory`, `reference`."
        ),
    )
    errors = check_doc_consistency(tmp_path)
    assert len(errors) == 1
    assert "SPEC.md" in errors[0]
    assert "'reference'" in errors[0]
    assert "'obsolete-type'" in errors[0]


def test_check_doc_consistency_detects_drift_in_adoption_doc(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        spec_type_line=_IN_SYNC_TYPE_LINE,
        # adoption.md forgot to add 'memory' when it was introduced.
        adoption_type_line=(
            "`type`: one of `standard`, `decision`, `workflow`, `project`, "
            "`reference`."
        ),
    )
    errors = check_doc_consistency(tmp_path)
    assert len(errors) == 1
    assert "docs/adoption.md" in errors[0]
    assert "'memory'" in errors[0]


def test_check_doc_consistency_detects_reserved_drift(tmp_path: Path) -> None:
    _write_bundle(tmp_path, spec_type_line=_IN_SYNC_TYPE_LINE, adoption_type_line=(
        "`type`: one of `standard`, `decision`, `workflow`, `project`, "
        "`memory`, `reference`."
    ))
    spec_path = tmp_path / "SPEC.md"
    text = spec_path.read_text(encoding="utf-8")
    # Drop template.md from the reserved-filename sentence.
    text = text.replace(
        "The filenames `index.md`, `log.md`, and `template.md` are reserved",
        "The filenames `index.md`, `log.md` are reserved",
    )
    spec_path.write_text(text, encoding="utf-8")

    errors = check_doc_consistency(tmp_path)
    assert len(errors) == 1
    assert "RESERVED" in errors[0]
    assert "'template.md'" in errors[0]


def test_check_doc_consistency_reports_parse_error_without_crashing(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "SPEC.md").write_text("nothing relevant in this file.\n", encoding="utf-8")
    (tmp_path / "docs" / "adoption.md").write_text("also nothing relevant.\n", encoding="utf-8")

    errors = check_doc_consistency(tmp_path)
    # Every check should fail to parse (both files, both fields, plus
    # reserved), but the function must return messages, not raise.
    assert len(errors) >= 4
    assert all("no '" in e or "marker found" in e or "sentence found" in e for e in errors)


def test_check_doc_consistency_real_repo_docs_are_in_sync() -> None:
    """The actual SPEC.md / docs/adoption.md in this repo must pass today.

    This is the guard's own dogfood check: if this test fails, either the
    real docs have drifted (fix the docs) or the parser broke on real
    formatting (fix the parser) — either way CI should have caught it.
    """
    repo_root = Path(__file__).resolve().parent.parent
    assert check_doc_consistency(repo_root) == []
