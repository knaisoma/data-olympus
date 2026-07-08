"""Tests for the memory-inbox in-force floor and the computed `in_force`
boolean on verbose kb_get / kb_search surfaces (issue #109).

A doc under the memory-inbox prefix is NEVER in force regardless of claimed
status (covers a legacy inbox file or forged frontmatter on an agent-written
memory), implemented as an ``is_inbox`` column derived once at index build
time (format.validate.is_inbox_path) and composed into the single-sourced
``is_in_force`` predicate / SQL fragments -- not a forked predicate.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.index import Index
from data_olympus.tools_read import kb_get_fn, kb_search_fn

if TYPE_CHECKING:
    from pathlib import Path


def _write(kb: Path, rel: str, *, id_: str, status: str, title: str, body: str,
           validity: str = "") -> None:
    p = kb / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nid: {id_}\ntype: standard\nstatus: {status}\ntier: T1\n"
        f"category: foundation\ntitle: {title}\n{validity}---\n"
        f"# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def _build_inbox_kb(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    kb.mkdir()
    # Same topic ('gizmo'), same claimed status (active), one under the memory
    # inbox and one outside it, so a query surfaces both when unfiltered and
    # the floor is observable when in_force=True is requested.
    _write(kb, "memory/inbox/2026-01-01-gizmo.md", id_="DOC-INBOX-ACTIVE",
           status="active", title="Gizmo Inbox", body="An inbox memory about gizmos.")
    _write(kb, "universal/foundation/gizmo.md", id_="DOC-OUTSIDE-ACTIVE",
           status="active", title="Gizmo Outside", body="A real rule about gizmos.")
    return kb


def _idx(tmp_path: Path, index_path: Path) -> Index:
    kb = _build_inbox_kb(tmp_path)
    idx = Index(index_path)
    idx.build(kb, source_commit="test")
    return idx


# --- hard in_force filter excludes inbox docs -------------------------------


def test_in_force_excludes_inbox_doc_even_with_active_status(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    hits = idx.search("gizmo", limit=20, in_force=True)
    ids = {h.id for h in hits}
    assert ids == {"DOC-OUTSIDE-ACTIVE"}
    assert "DOC-INBOX-ACTIVE" not in ids


def test_default_search_still_surfaces_inbox_doc(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """The floor is specific to in_force=True; an unfiltered search still finds
    the inbox doc (it just is not treated as a governing rule)."""
    idx = _idx(tmp_path, tmp_index_path)
    ids = {h.id for h in idx.search("gizmo", limit=20)}
    assert ids == {"DOC-INBOX-ACTIVE", "DOC-OUTSIDE-ACTIVE"}


def test_kb_search_fn_in_force_excludes_inbox(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    resp = kb_search_fn(idx=idx, query="gizmo", in_force=True)
    ids = {h.id for h in resp.hits}
    assert ids == {"DOC-OUTSIDE-ACTIVE"}


# --- computed `in_force` boolean on verbose surfaces ------------------------


def test_search_hit_in_force_field_reflects_the_floor(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    # Unfiltered search returns both; verbose in_force must distinguish them.
    resp = kb_search_fn(idx=idx, query="gizmo", today="2026-06-01")
    by_id = {h.id: h for h in resp.hits}
    assert by_id["DOC-INBOX-ACTIVE"].in_force is False
    assert by_id["DOC-OUTSIDE-ACTIVE"].in_force is True


def test_kb_get_in_force_field_reflects_the_floor(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    inbox_doc = kb_get_fn(idx=idx, id="DOC-INBOX-ACTIVE", today="2026-06-01")
    outside_doc = kb_get_fn(idx=idx, id="DOC-OUTSIDE-ACTIVE", today="2026-06-01")
    assert inbox_doc.in_force is False
    assert outside_doc.in_force is True


def test_kb_get_in_force_false_for_proposed_status(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb2"
    kb.mkdir()
    _write(kb, "universal/foundation/draftdoc.md", id_="DOC-PROPOSED",
           status="proposed", title="Proposed Doc", body="Not yet accepted.")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="test")
    doc = kb_get_fn(idx=idx, id="DOC-PROPOSED", today="2026-06-01")
    assert doc.in_force is False


def test_kb_get_in_force_false_when_expired(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb3"
    kb.mkdir()
    _write(
        kb, "universal/foundation/expired.md", id_="DOC-EXPIRED",
        status="active", title="Expired Doc", body="Was in force.",
        validity="validity:\n  valid_until: 2020-01-01\n",
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="test")
    doc = kb_get_fn(idx=idx, id="DOC-EXPIRED", today="2026-06-01")
    assert doc.status == "active"
    assert doc.in_force is False


def test_kb_get_in_force_true_for_active_no_validity(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb4"
    kb.mkdir()
    _write(kb, "universal/foundation/plain.md", id_="DOC-PLAIN",
           status="active", title="Plain Doc", body="In force, no validity block.")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="test")
    doc = kb_get_fn(idx=idx, id="DOC-PLAIN", today="2026-06-01")
    assert doc.in_force is True


# --- compact responses are unchanged (in_force is verbose-only) -------------


def test_compact_search_hit_omits_in_force(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    resp = kb_search_fn(idx=idx, query="gizmo", today="2026-06-01")
    compact = resp.compact_dump()
    assert all("in_force" not in h for h in compact["hits"])


def test_compact_get_omits_in_force(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    doc = kb_get_fn(idx=idx, id="DOC-OUTSIDE-ACTIVE", today="2026-06-01")
    assert "in_force" not in doc.compact_dump()
