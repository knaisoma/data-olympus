from pathlib import Path

from data_olympus.format.document import Document
from data_olympus.format.validate import validate_document


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
