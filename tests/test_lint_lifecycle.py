"""Cross-file lint tests for typed lifecycle relationships (issue #110, slice 1):
supersedes / superseded_by / contradicts.

These findings only appear when linting a bundle (a discovered file list),
built from an in-memory id map over that list -- no database involved. Per-file
schema validation (`validate_document`) is unaffected and covered by
test_lint.py.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.format import lint_files

if TYPE_CHECKING:
    from pathlib import Path


def _write(p: Path, fm_extra: str, *, doc_id: str, status: str = "active") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nid: {doc_id}\ntype: standard\nstatus: {status}\ntier: T1\n"
        f"title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        f"{fm_extra}---\nbody\n",
        encoding="utf-8",
    )


def _findings(results, path, severity=None):
    findings = results.get(path, [])
    if severity is None:
        return findings
    return [f for f in findings if f.severity == severity]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_self_supersession_via_supersedes_is_error(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    _write(a, "supersedes: A\n", doc_id="A")
    results = lint_files([a])
    errors = _findings(results, a, "error")
    assert any("supersedes" in f.field or "cannot supersede itself" in f.message for f in errors)


def test_self_supersession_via_superseded_by_is_error(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    _write(a, "superseded_by: A\n", doc_id="A", status="superseded")
    results = lint_files([a])
    errors = _findings(results, a, "error")
    assert errors, "expected a self-supersession error"


def test_two_doc_supersession_cycle_is_error(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    _write(a, "supersedes: B\n", doc_id="A")
    _write(b, "supersedes: A\n", doc_id="B")
    results = lint_files([a, b])
    assert _findings(results, a, "error") or _findings(results, b, "error")
    # Both nodes in the cycle should be flagged.
    assert _findings(results, a, "error")
    assert _findings(results, b, "error")


def test_three_doc_supersession_cycle_is_error(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    c = tmp_path / "c.md"
    _write(a, "supersedes: B\n", doc_id="A")
    _write(b, "supersedes: C\n", doc_id="B")
    _write(c, "supersedes: A\n", doc_id="C")
    results = lint_files([a, b, c])
    assert _findings(results, a, "error")
    assert _findings(results, b, "error")
    assert _findings(results, c, "error")


def test_non_string_entry_in_supersedes_is_error(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text(
        "---\nid: A\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        "supersedes: [B, 123]\n---\nbody\n",
        encoding="utf-8",
    )
    b = tmp_path / "b.md"
    _write(b, "", doc_id="B")
    results = lint_files([a, b])
    errors = _findings(results, a, "error")
    assert errors, "a non-string entry in supersedes must be an error"


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def test_dangling_target_is_warning(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    _write(a, "supersedes: GHOST\n", doc_id="A")
    results = lint_files([a])
    warnings = _findings(results, a, "warning")
    assert any("GHOST" in f.message for f in warnings)


def test_asymmetric_pair_missing_superseded_by(tmp_path: Path) -> None:
    """A supersedes B but B lacks superseded_by: A."""
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    _write(a, "supersedes: B\n", doc_id="A")
    _write(b, "", doc_id="B", status="superseded")
    results = lint_files([a, b])
    warnings = _findings(results, a, "warning")
    assert any("B" in f.message for f in warnings)


def test_asymmetric_pair_missing_supersedes(tmp_path: Path) -> None:
    """B superseded_by A but A lacks supersedes: B."""
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    _write(a, "", doc_id="A")
    _write(b, "superseded_by: A\n", doc_id="B", status="superseded")
    results = lint_files([a, b])
    warnings = _findings(results, b, "warning")
    assert any("A" in f.message for f in warnings)


def test_superseded_by_present_but_status_in_force(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    _write(a, "superseded_by: B\n", doc_id="A", status="active")
    _write(b, "", doc_id="B")
    results = lint_files([a, b])
    warnings = _findings(results, a, "warning")
    assert any("in-force" in f.message or "active" in f.message for f in warnings)


def test_status_superseded_without_superseded_by(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    _write(a, "", doc_id="A", status="superseded")
    results = lint_files([a])
    warnings = _findings(results, a, "warning")
    assert any("superseded_by" in f.message for f in warnings)


def test_path_shaped_target_is_warning(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    _write(a, "supersedes: universal/foundation/STD-1.md\n", doc_id="A")
    results = lint_files([a])
    warnings = _findings(results, a, "warning")
    assert any("path" in f.message.lower() for f in warnings)


def test_inforce_contradiction_pair_is_warning(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    _write(a, "contradicts: B\n", doc_id="A", status="active")
    _write(b, "", doc_id="B", status="active")
    results = lint_files([a, b])
    assert _findings(results, a, "warning")
    assert _findings(results, b, "warning")


def test_inforce_contradiction_pair_only_one_side_listed(tmp_path: Path) -> None:
    """Even when only A lists B in contradicts, both in-force docs get warned."""
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    _write(a, "contradicts: B\n", doc_id="A", status="accepted")
    _write(b, "", doc_id="B", status="approved")
    results = lint_files([a, b])
    assert any("B" in f.message for f in _findings(results, a, "warning"))
    assert any("A" in f.message for f in _findings(results, b, "warning"))


# ---------------------------------------------------------------------------
# Regression: ADR-importer scalar-shape output and the example-bundle must
# stay lint-clean.
# ---------------------------------------------------------------------------


def test_adr_importer_scalar_supersedes_shape_lints_clean(tmp_path: Path) -> None:
    """The ADR importer emits `supersedes` as a bare scalar when there's only
    one entry (importer/run.py); that shape must not itself produce any
    supersedes-shape finding."""
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    _write(a, "supersedes: B\n", doc_id="A")
    _write(b, "superseded_by: A\n", doc_id="B", status="superseded")
    results = lint_files([a, b])
    shape_findings = [
        f
        for path in (a, b)
        for f in results.get(path, [])
        if f.field in ("supersedes", "superseded_by") and f.severity == "error"
    ]
    assert shape_findings == []


def test_example_bundle_lints_zero_errors_and_warnings() -> None:
    from pathlib import Path as _Path

    from data_olympus.format import discover_bundle_files

    bundle = _Path(__file__).resolve().parents[1] / "example-bundle"
    files = discover_bundle_files(bundle)
    results = lint_files(files)
    all_findings = [f for findings in results.values() for f in findings]
    assert all_findings == [], f"example-bundle must lint clean: {all_findings}"
