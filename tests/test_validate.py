from pathlib import Path

from data_olympus.format.document import Document
from data_olympus.format.validate import (
    is_expired,
    is_in_force,
    is_upcoming,
    normalize_validity_date,
    validate_document,
)


def _doc(tmp_path: Path, name: str, text: str) -> Document:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return Document.load(p)


def test_conformant_document_has_no_errors(tmp_path: Path):
    doc = _doc(
        tmp_path,
        "STD-U-002.md",
        "---\nid: STD-U-002\ntype: standard\nstatus: active\ntier: T1\n"
        "title: Writing Style\ndescription: how to write\ntags: [foundation]\n"
        "timestamp: 2026-06-01\n---\nbody\n",
    )
    errors = [f for f in validate_document(doc) if f.severity == "error"]
    assert errors == []


def test_missing_required_fields_are_errors(tmp_path: Path):
    doc = _doc(tmp_path, "x.md", "---\ntitle: only a title\n---\nbody\n")
    fields = {f.field for f in validate_document(doc) if f.severity == "error"}
    assert {"id", "type", "status", "tier"} <= fields


def test_unknown_enum_values_are_errors(tmp_path: Path):
    doc = _doc(
        tmp_path,
        "x.md",
        "---\nid: X-1\ntype: bogus\nstatus: weird\ntier: T9\n---\nbody\n",
    )
    errs = {f.field for f in validate_document(doc) if f.severity == "error"}
    assert {"type", "status", "tier"} <= errs


def test_missing_recommended_fields_are_warnings(tmp_path: Path):
    doc = _doc(
        tmp_path,
        "x.md",
        "---\nid: X-1\ntype: standard\nstatus: active\ntier: T1\n---\nbody\n",
    )
    warns = {f.field for f in validate_document(doc) if f.severity == "warning"}
    assert {"title", "description", "tags", "timestamp"} <= warns


def test_reserved_files_are_exempt(tmp_path: Path):
    doc = _doc(tmp_path, "index.md", "# Index\n\n* [a](a.md)\n")
    assert validate_document(doc) == []


def test_zero_valued_required_field_is_not_missing(tmp_path):
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: 0\ntype: standard\nstatus: active\ntier: T1\n---\nbody\n",
    )
    missing = [
        f for f in validate_document(doc)
        if f.message.startswith("missing required field 'id'")
    ]
    assert missing == []


def test_adr_accepted_status_is_valid(tmp_path):
    doc = _doc(
        tmp_path, "DEC-021.md",
        "---\nid: DEC-021\ntype: decision\nstatus: accepted\ntier: meta\n---\nbody\n",
    )
    status_errs = [
        f for f in validate_document(doc)
        if f.field == "status" and f.severity == "error"
    ]
    assert status_errs == []


def test_tags_as_string_is_a_warning(tmp_path):
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\ntags: foundation\n---\nbody\n",
    )
    tag_warns = [
        f for f in validate_document(doc)
        if f.field == "tags" and f.severity == "warning"
    ]
    assert tag_warns and "should be a list" in tag_warns[0].message


# ---------------------------------------------------------------------------
# Validity / freshness predicates (issue #107)
# ---------------------------------------------------------------------------


def test_is_expired_true_when_valid_until_in_past():
    assert is_expired("2026-01-01", today="2026-01-02") is True


def test_is_expired_false_on_boundary_day():
    # valid_until == today is still in force (inclusive boundary).
    assert is_expired("2026-01-02", today="2026-01-02") is False


def test_is_expired_false_when_absent():
    assert is_expired(None, today="2026-01-02") is False
    assert is_expired("", today="2026-01-02") is False


def test_is_upcoming_true_when_valid_from_in_future():
    assert is_upcoming("2026-02-01", today="2026-01-02") is True


def test_is_upcoming_false_on_boundary_day():
    assert is_upcoming("2026-01-02", today="2026-01-02") is False


def test_is_upcoming_false_when_absent():
    assert is_upcoming(None, today="2026-01-02") is False


def test_is_in_force_requires_status_class_and_window():
    # active + no window -> in force.
    assert is_in_force("active", None, None, today="2026-01-02") is True
    # active + expired window -> not in force.
    assert is_in_force("active", None, "2026-01-01", today="2026-01-02") is False
    # active + upcoming window -> not in force.
    assert is_in_force("active", "2026-02-01", None, today="2026-01-02") is False
    # superseded (not in the status class) -> not in force even with a clean window.
    assert is_in_force("superseded", None, None, today="2026-01-02") is False


def test_is_in_force_boundary_days_are_inclusive():
    assert is_in_force("active", "2026-01-02", "2026-01-02", today="2026-01-02") is True


def test_normalize_validity_date_accepts_date_string():
    normalized, malformed = normalize_validity_date("2026-06-01")
    assert normalized == "2026-06-01"
    assert malformed is False


def test_normalize_validity_date_accepts_datetime_string_with_tz():
    normalized, malformed = normalize_validity_date("2026-06-01T12:00:00+02:00")
    assert normalized == "2026-06-01"
    assert malformed is False


def test_normalize_validity_date_accepts_zulu_suffix():
    normalized, malformed = normalize_validity_date("2026-06-01T00:00:00Z")
    assert normalized == "2026-06-01"
    assert malformed is False


def test_normalize_validity_date_accepts_python_date_object():
    import datetime
    normalized, malformed = normalize_validity_date(datetime.date(2026, 6, 1))
    assert normalized == "2026-06-01"
    assert malformed is False


def test_normalize_validity_date_accepts_python_datetime_object():
    import datetime
    normalized, malformed = normalize_validity_date(datetime.datetime(2026, 6, 1, 8, 30))
    assert normalized == "2026-06-01"
    assert malformed is False


def test_normalize_validity_date_none_is_absent_not_malformed():
    normalized, malformed = normalize_validity_date(None)
    assert normalized == ""
    assert malformed is False


def test_normalize_validity_date_garbage_is_malformed():
    normalized, malformed = normalize_validity_date("not-a-date")
    assert normalized == ""
    assert malformed is True


def test_normalize_validity_date_wrong_type_is_malformed():
    normalized, malformed = normalize_validity_date(12345)
    assert normalized == ""
    assert malformed is True


# ---------------------------------------------------------------------------
# kb lint validity warnings (issue #107): always warnings, never errors.
# ---------------------------------------------------------------------------


def test_lint_warns_on_recheck_by_in_the_past(tmp_path: Path):
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        "validity:\n  recheck_by: 2026-01-01\n---\nbody\n",
    )
    findings = validate_document(doc, today="2026-06-01")
    warns = [f for f in findings if f.severity == "warning" and f.field == "validity"]
    assert any("recheck_by" in f.message for f in warns)
    assert all(f.severity == "warning" for f in findings if f.field == "validity")


def test_lint_warns_on_expired_but_active_status(tmp_path: Path):
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        "validity:\n  valid_until: 2026-01-01\n---\nbody\n",
    )
    findings = validate_document(doc, today="2026-06-01")
    warns = [f for f in findings if f.severity == "warning" and f.field == "validity"]
    assert any("valid_until" in f.message for f in warns)
    assert all(f.severity == "warning" for f in findings if f.field == "validity")


def test_lint_no_warning_when_expired_and_status_not_in_force(tmp_path: Path):
    # Only "expired but ACTIVE" is a warning; a properly superseded doc with an
    # old valid_until is not flagged (its expiry is not surprising).
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: superseded\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        "validity:\n  valid_until: 2026-01-01\n---\nbody\n",
    )
    findings = validate_document(doc, today="2026-06-01")
    warns = [f for f in findings if f.field == "validity" and "valid_until" in f.message]
    assert warns == []


def test_lint_warns_on_malformed_validity_value(tmp_path: Path):
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        "validity:\n  valid_until: not-a-date\n---\nbody\n",
    )
    findings = validate_document(doc, today="2026-06-01")
    warns = [f for f in findings if f.severity == "warning" and f.field == "validity"]
    assert any("malformed" in f.message for f in warns)
    assert all(f.severity != "error" for f in findings)


def test_lint_no_validity_findings_when_validity_absent(tmp_path: Path):
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n---\nbody\n",
    )
    findings = validate_document(doc, today="2026-06-01")
    assert [f for f in findings if f.field == "validity"] == []


def test_lint_validity_findings_are_never_errors(tmp_path: Path):
    """Wall-clock-based checks must never block CI (they would flake with time)."""
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        "validity:\n  valid_until: 2000-01-01\n  recheck_by: bogus\n---\nbody\n",
    )
    findings = validate_document(doc, today="2026-06-01")
    assert all(f.severity != "error" for f in findings)
