"""`derived_from` (issue #300, first slice): surfaced, never acted on.

A document may declare that its guidance was drawn from other documents. When
such a source leaves force, `kb lint` and `kb_get` (on both ends) name the
in-force dependents so a person decides. Nothing is invalidated, demoted or
filtered. All date logic uses an injected ``today``.
"""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import httpx
import pytest
from fastmcp import Client

from data_olympus.format import lint_files
from data_olympus.format.validate import RETIRED_STATUSES, is_retired
from data_olympus.index import Index
from data_olympus.server import build_app
from data_olympus.tools_read import kb_get_fn, shape_response

if TYPE_CHECKING:
    from pathlib import Path

TODAY = "2026-07-08"


def _doc(doc_id: str, extra: str = "", status: str = "active") -> str:
    return (
        f"---\nid: {doc_id}\ntype: standard\nstatus: {status}\ntier: T1\n"
        f"category: foundation\ntitle: {doc_id}\ndescription: d\ntags: [x]\n"
        f"timestamp: 2026-01-01\n{extra}---\n# {doc_id}\n\nwidget body {doc_id}\n"
    )


def _write(root: Path, rel: str, doc_id: str, extra: str = "", status: str = "active") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_doc(doc_id, extra, status), encoding="utf-8")
    return p


def _derived(results, path, severity=None):  # noqa: ANN001, ANN202
    return [
        f for f in results.get(path, [])
        if f.field == "derived_from" and (severity is None or f.severity == severity)
    ]


# ---------------------------------------------------------------------------
# D4: the single retirement definition
# ---------------------------------------------------------------------------


def test_retired_status_class_includes_rejected_deliberately() -> None:
    assert frozenset({"deprecated", "superseded", "rejected"}) == RETIRED_STATUSES
    assert is_retired("rejected", "", TODAY)
    assert is_retired("active", "2026-07-01", TODAY)
    assert is_retired("active", "", TODAY, graph_excluded=True)
    for status in ("draft", "proposed", "active", "accepted"):
        assert not is_retired(status, "", TODAY)
    # The boundary day is not expired.
    assert not is_retired("active", TODAY, TODAY)


# ---------------------------------------------------------------------------
# Check 3: path-shaped targets
# ---------------------------------------------------------------------------


def test_path_shaped_resolving_target_warns(tmp_path: Path) -> None:
    a = _write(tmp_path, "x/a.md", "x/a.md")
    b = _write(tmp_path, "b.md", "B", "derived_from: x/a.md\n")
    findings = _derived(lint_files([a, b], today=TODAY), b)
    assert [f.severity for f in findings] == ["warning"]
    assert "looks like a file path" in findings[0].message


def test_path_shaped_unresolved_target_errors(tmp_path: Path) -> None:
    b = _write(tmp_path, "b.md", "B", "derived_from: x/ghost.md\n")
    findings = _derived(lint_files([b], today=TODAY), b, "error")
    assert findings
    assert "looks like a file path" in findings[0].message


# ---------------------------------------------------------------------------
# Check 4: self-reference and cycles
# ---------------------------------------------------------------------------


def test_self_reference_errors(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.md", "A", "derived_from: A\n")
    errors = _derived(lint_files([a], today=TODAY), a, "error")
    assert any("cannot derive from itself" in f.message for f in errors)


def test_two_node_derivation_cycle_errors_on_every_member(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.md", "A", "derived_from: B\n")
    b = _write(tmp_path, "b.md", "B", "derived_from: A\n")
    results = lint_files([a, b], today=TODAY)
    for path in (a, b):
        assert any("derivation cycle" in f.message for f in _derived(results, path, "error"))


def test_three_node_derivation_cycle_errors_on_every_member(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.md", "A", "derived_from: B\n")
    b = _write(tmp_path, "b.md", "B", "derived_from: [C]\n")
    c = _write(tmp_path, "c.md", "C", "derived_from: A\n")
    results = lint_files([a, b, c], today=TODAY)
    for path in (a, b, c):
        assert any("derivation cycle" in f.message for f in _derived(results, path, "error"))


def test_derivation_graph_is_not_merged_with_supersession(tmp_path: Path) -> None:
    # A derives from B and B supersedes A: different relations, no cycle.
    a = _write(tmp_path, "a.md", "A", "derived_from: B\nsuperseded_by: B\n", status="superseded")
    b = _write(tmp_path, "b.md", "B", "supersedes: A\n")
    results = lint_files([a, b], today=TODAY)
    assert not any("cycle" in f.message for p in (a, b) for f in results.get(p, []))


# ---------------------------------------------------------------------------
# Check 5: derived_from mixed with this document's own supersession
# ---------------------------------------------------------------------------


def test_derives_from_and_supersedes_same_target_errors(tmp_path: Path) -> None:
    old = _write(tmp_path, "old.md", "OLD", "superseded_by: NEW\n", status="superseded")
    new = _write(tmp_path, "new.md", "NEW", "supersedes: OLD\nderived_from: OLD\n")
    results = lint_files([old, new], today=TODAY)
    errors = _derived(results, new, "error")
    assert any("supersedes" in f.message and "'OLD'" in f.message for f in errors)
    # The mixed pair is reported once, as the error, not also as a review warning.
    assert not _derived(results, new, "warning")
    assert not _derived(results, old, "warning")


def test_derives_from_target_whose_superseded_by_names_it_errors(tmp_path: Path) -> None:
    old = _write(tmp_path, "old.md", "OLD", "superseded_by: NEW\n", status="superseded")
    new = _write(tmp_path, "new.md", "NEW", "derived_from: OLD\n")
    errors = _derived(lint_files([old, new], today=TODAY), new, "error")
    assert any("'OLD'" in f.message for f in errors)


def test_contradicts_and_derived_from_same_target_is_allowed(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.md", "A")
    b = _write(tmp_path, "b.md", "B", "derived_from: A\ncontradicts: A\n")
    assert not _derived(lint_files([a, b], today=TODAY), b)


# ---------------------------------------------------------------------------
# Checks 6 and 7: retirement warnings, both ends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("status", "validity", "reason"), [
    ("superseded", "superseded_by: NEW\n", "status 'superseded'"),
    ("deprecated", "", "status 'deprecated'"),
    ("rejected", "", "status 'rejected'"),
    ("active", "validity:\n  valid_until: 2026-07-01\n",
     f"expired: valid_until 2026-07-01 is before {TODAY}"),
])
def test_retired_source_produces_exactly_two_warnings(
    tmp_path: Path, status: str, validity: str, reason: str,
) -> None:
    files = [
        _write(tmp_path, "src.md", "SRC", validity, status=status),
        _write(tmp_path, "dep.md", "DEP", "derived_from: SRC\n"),
    ]
    if "superseded_by" in validity:
        files.append(_write(tmp_path, "new.md", "NEW", "supersedes: SRC\n"))
    src, dep = files[0], files[1]
    results = lint_files(files, today=TODAY)
    dep_findings = _derived(results, dep)
    src_findings = _derived(results, src)
    assert [f.severity for f in dep_findings] == ["warning"]
    assert [f.severity for f in src_findings] == ["warning"]
    assert dep_findings[0].message == (
        f"'DEP' is in force but derives from 'SRC', which is no longer in force "
        f"({reason}); decide whether 'DEP' still holds, needs rewording, or should be retired"
    )
    assert src_findings[0].message == (
        f"'SRC' is no longer in force ({reason}) but in-force documents derive "
        "from it: 'DEP'; review them"
    )


def test_forgotten_status_flip_reason_names_the_in_force_superseder(tmp_path: Path) -> None:
    src = _write(tmp_path, "src.md", "SRC")
    new = _write(tmp_path, "new.md", "NEW", "supersedes: SRC\n")
    dep = _write(tmp_path, "dep.md", "DEP", "derived_from: SRC\n")
    results = lint_files([src, new, dep], today=TODAY)
    assert "superseded by in-force 'NEW'" in _derived(results, dep, "warning")[0].message
    assert "superseded by in-force 'NEW'" in _derived(results, src, "warning")[0].message


def test_source_finding_lists_every_in_force_dependent_sorted(tmp_path: Path) -> None:
    src = _write(tmp_path, "src.md", "SRC", status="deprecated")
    d2 = _write(tmp_path, "d2.md", "DEP-2", "derived_from: SRC\n")
    d1 = _write(tmp_path, "d1.md", "DEP-1", "derived_from: [SRC]\n")
    gone = _write(tmp_path, "d3.md", "DEP-3", "derived_from: SRC\n", status="deprecated")
    results = lint_files([src, d2, d1, gone], today=TODAY)
    msgs = [f.message for f in _derived(results, src)]
    assert msgs == [
        "'SRC' is no longer in force (status 'deprecated') but in-force documents "
        "derive from it: 'DEP-1', 'DEP-2'; review them"
    ]
    # A retired dependent needs no review and is never named.
    assert not _derived(results, gone)


@pytest.mark.parametrize(("status", "validity"), [
    ("draft", ""),
    ("proposed", ""),
    ("active", "validity:\n  valid_from: 2026-08-01\n"),
])
def test_never_governing_sources_produce_no_warning(
    tmp_path: Path, status: str, validity: str,
) -> None:
    src = _write(tmp_path, "src.md", "SRC", validity, status=status)
    dep = _write(tmp_path, "dep.md", "DEP", "derived_from: SRC\n")
    results = lint_files([src, dep], today=TODAY)
    assert not _derived(results, src)
    assert not _derived(results, dep)


def test_expired_dependent_is_not_named(tmp_path: Path) -> None:
    src = _write(tmp_path, "src.md", "SRC", status="deprecated")
    dep = _write(tmp_path, "dep.md", "DEP",
                 "derived_from: SRC\nvalidity:\n  valid_until: 2026-01-01\n")
    results = lint_files([src, dep], today=TODAY)
    assert not _derived(results, src)
    assert not _derived(results, dep)


def test_inbox_dependent_is_not_named_when_root_is_known(tmp_path: Path) -> None:
    src = _write(tmp_path, "universal/src.md", "SRC", status="deprecated")
    dep = _write(tmp_path, "memory/inbox/dep.md", "DEP", "derived_from: SRC\n")
    results = lint_files([src, dep], today=TODAY, root=tmp_path)
    assert not _derived(results, src)
    assert not _derived(results, dep)


def test_id_known_only_through_resolve_root_produces_no_finding(tmp_path: Path) -> None:
    dep = _write(tmp_path, "dep.md", "DEP", "derived_from: OUTSIDE\n")
    assert _derived(lint_files([dep], resolve_ids={"OUTSIDE"}, today=TODAY), dep) == []


def test_supersession_by_a_draft_does_not_retire_the_source(tmp_path: Path) -> None:
    src = _write(tmp_path, "src.md", "SRC")
    new = _write(tmp_path, "new.md", "NEW", "supersedes: SRC\n", status="draft")
    dep = _write(tmp_path, "dep.md", "DEP", "derived_from: SRC\n")
    results = lint_files([src, new, dep], today=TODAY)
    assert not _derived(results, src)
    assert not _derived(results, dep)


@pytest.mark.parametrize("today", ["2026-07-02", "2027-01-01", "2099-12-31"])
def test_expiry_is_a_warning_never_an_error_at_any_today(tmp_path: Path, today: str) -> None:
    src = _write(tmp_path, "src.md", "SRC", "validity:\n  valid_until: 2026-07-01\n")
    dep = _write(tmp_path, "dep.md", "DEP", "derived_from: SRC\n")
    results = lint_files([src, dep], today=today)
    findings = _derived(results, src) + _derived(results, dep)
    assert len(findings) == 2
    assert {f.severity for f in findings} == {"warning"}


def test_before_expiry_there_is_no_finding(tmp_path: Path) -> None:
    src = _write(tmp_path, "src.md", "SRC", "validity:\n  valid_until: 2026-07-01\n")
    dep = _write(tmp_path, "dep.md", "DEP", "derived_from: SRC\n")
    results = lint_files([src, dep], today="2026-07-01")
    assert not _derived(results, src) and not _derived(results, dep)


# ---------------------------------------------------------------------------
# Check 9: kb_get on both ends
# ---------------------------------------------------------------------------


def _kb_get_corpus(kb: Path) -> None:
    _write(kb, "universal/foundation/src.md", "SRC", "superseded_by: NEW\n", status="superseded")
    _write(kb, "universal/foundation/new.md", "NEW", "supersedes: SRC\n")
    _write(kb, "universal/foundation/dep-b.md", "DEP-B", "derived_from: SRC\n")
    _write(kb, "universal/foundation/dep-a.md", "DEP-A", "derived_from: [SRC, NEW]\n")
    _write(kb, "universal/foundation/dep-old.md", "DEP-OLD", "derived_from: SRC\n",
           status="deprecated")
    _write(kb, "universal/foundation/dep-exp.md", "DEP-EXP",
           "derived_from: SRC\nvalidity:\n  valid_until: 2026-01-01\n")
    _write(kb, "memory/inbox/dep-mem.md", "DEP-MEM", "derived_from: SRC\n")
    _write(kb, "universal/foundation/dangling.md", "DANGLING", "derived_from: GHOST\n")


def test_kb_get_retired_source_names_in_force_dependents_only_sorted(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    _kb_get_corpus(kb)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    src = kb_get_fn(idx=idx, id="SRC", today=TODAY)
    assert src.dependents_to_review == ["DEP-A", "DEP-B"]
    assert src.derived_from == [] and src.derived_from_retired == []
    assert src.in_force is False


def test_kb_get_in_force_dependent_names_retired_sources(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    _kb_get_corpus(kb)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    dep = kb_get_fn(idx=idx, id="DEP-A", today=TODAY)
    assert dep.derived_from == ["NEW", "SRC"]
    assert dep.derived_from_retired == ["SRC"]
    assert dep.dependents_to_review == []
    assert dep.in_force is True
    # An in-force source never lists dependents.
    assert kb_get_fn(idx=idx, id="NEW", today=TODAY).dependents_to_review == []
    # A retired dependent is not asked to review its own sources.
    old = kb_get_fn(idx=idx, id="DEP-OLD", today=TODAY)
    assert old.derived_from == ["SRC"] and old.derived_from_retired == []
    # A dangling target is stored but never surfaced.
    assert kb_get_fn(idx=idx, id="DANGLING", today=TODAY).derived_from == []


def test_kb_get_forgotten_status_flip_counts_as_retired(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    _write(kb, "universal/foundation/src.md", "SRC")  # status never flipped
    _write(kb, "universal/foundation/new.md", "NEW", "supersedes: SRC\n")
    _write(kb, "universal/foundation/dep.md", "DEP", "derived_from: SRC\n")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    assert kb_get_fn(idx=idx, id="SRC", today=TODAY).dependents_to_review == ["DEP"]
    assert kb_get_fn(idx=idx, id="DEP", today=TODAY).derived_from_retired == ["SRC"]


def test_kb_get_expired_source_counts_as_retired(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    _write(kb, "universal/foundation/src.md", "SRC", "validity:\n  valid_until: 2026-07-01\n")
    _write(kb, "universal/foundation/dep.md", "DEP", "derived_from: SRC\n")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    assert kb_get_fn(idx=idx, id="SRC", today=TODAY).dependents_to_review == ["DEP"]
    assert kb_get_fn(idx=idx, id="SRC", today="2026-07-01").dependents_to_review == []


def test_kb_get_draft_source_is_not_retired(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = tmp_path / "kb"
    _write(kb, "universal/foundation/src.md", "SRC", status="draft")
    _write(kb, "universal/foundation/dep.md", "DEP", "derived_from: SRC\n")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    assert kb_get_fn(idx=idx, id="SRC", today=TODAY).dependents_to_review == []
    dep = kb_get_fn(idx=idx, id="DEP", today=TODAY)
    assert dep.derived_from == ["SRC"] and dep.derived_from_retired == []


def test_kb_get_compact_carries_the_lists_only_when_non_empty(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    _kb_get_corpus(kb)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    src = shape_response(kb_get_fn(idx=idx, id="SRC", today=TODAY), verbose=False)
    assert src["dependents_to_review"] == ["DEP-A", "DEP-B"]
    assert "derived_from" not in src and "derived_from_retired" not in src
    dep = shape_response(kb_get_fn(idx=idx, id="DEP-A", today=TODAY), verbose=False)
    assert dep["derived_from"] == ["NEW", "SRC"]
    assert dep["derived_from_retired"] == ["SRC"]
    assert "dependents_to_review" not in dep


# ---------------------------------------------------------------------------
# Check 11: MCP and REST parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_and_rest_kb_get_return_the_same_fields(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    _write(kb, "universal/foundation/src.md", "SRC", status="deprecated")
    _write(kb, "universal/foundation/dep.md", "DEP", "derived_from: SRC\n")
    app = build_app(
        kb_main_path=kb, kb_index_path=tmp_path / "idx.db", sync_interval_sec=60,
        staleness_degraded_sec=600, bootstrap_now=True,
    )
    keys = ("derived_from", "derived_from_retired", "dependents_to_review")
    async with Client(app) as client:
        mcp = {
            doc_id: (await client.call_tool("kb_get", {"id": doc_id, "verbose": True})).data
            for doc_id in ("SRC", "DEP")
        }
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rest = {
            doc_id: (await client.get(f"/api/v1/get/{doc_id}",
                                      params={"verbose": "true"})).json()
            for doc_id in ("SRC", "DEP")
        }
    for doc_id in ("SRC", "DEP"):
        assert {k: mcp[doc_id][k] for k in keys} == {k: rest[doc_id][k] for k in keys}
    assert mcp["SRC"]["dependents_to_review"] == ["DEP"]
    assert mcp["DEP"]["derived_from_retired"] == ["SRC"]


# ---------------------------------------------------------------------------
# Check 13: read and lint paths never modify anything
# ---------------------------------------------------------------------------


def test_read_and_lint_paths_modify_nothing(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = tmp_path / "kb"
    _kb_get_corpus(kb)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=kb, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=kb, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=kb, check=True, env=env)
    files = sorted(kb.rglob("*.md"))
    before = {p: p.read_bytes() for p in files}

    lint_files(files, today=TODAY, root=kb)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    for doc_id in ("SRC", "DEP-A", "DEP-B", "NEW"):
        kb_get_fn(idx=idx, id=doc_id, today=TODAY)

    status = subprocess.run(["git", "status", "--porcelain"], cwd=kb, check=True,
                            capture_output=True, text=True, env=env).stdout
    assert status == ""
    assert {p: p.read_bytes() for p in sorted(kb.rglob("*.md"))} == before
