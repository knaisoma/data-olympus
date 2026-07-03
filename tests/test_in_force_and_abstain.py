"""Tests for the in_force status-class filter and the abstain signal gate.

Covers the engine (Index.search in_force), the single-status filter regression,
the single-sourced abstain gate (search_gate), the kb_search_fn abstained
response shape, and REST param threading (issue #68, epic #75).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from data_olympus.format.validate import IN_FORCE_STATUSES
from data_olympus.index import Index
from data_olympus.search_gate import SIGNAL_COLUMNS, abstain_gate
from data_olympus.server import build_app
from data_olympus.tools_read import kb_search_fn

if TYPE_CHECKING:
    from pathlib import Path


def _write(kb: Path, rel: str, *, id_: str, status: str, title: str, body: str,
           tags: str = "", doc_type: str = "standard", tier: str = "T1") -> None:
    p = kb / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tags_line = f"tags: [{tags}]\n" if tags else ""
    p.write_text(
        f"---\nid: {id_}\ntype: {doc_type}\nstatus: {status}\ntier: {tier}\n"
        f"category: foundation\n{tags_line}title: {title}\n---\n"
        f"# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def _build_status_kb(tmp_path: Path) -> Path:
    """A KB where the SAME topic ('widget') exists across every status class, so a
    single query surfaces all of them and status filtering is observable."""
    kb = tmp_path / "kb"
    kb.mkdir()
    _write(kb, "universal/foundation/active.md", id_="DOC-ACTIVE", status="active",
           title="Widget Active", body="The widget policy currently in force.")
    _write(kb, "universal/foundation/accepted.md", id_="DOC-ACCEPTED", status="accepted",
           title="Widget Accepted", body="The widget decision, accepted.", doc_type="decision")
    _write(kb, "universal/foundation/approved.md", id_="DOC-APPROVED", status="approved",
           title="Widget Approved", body="The widget decision, approved.", doc_type="decision")
    _write(kb, "universal/foundation/superseded.md", id_="DOC-SUPERSEDED", status="superseded",
           title="Widget Superseded", body="The old widget policy, superseded.")
    _write(kb, "universal/foundation/deprecated.md", id_="DOC-DEPRECATED", status="deprecated",
           title="Widget Deprecated", body="The old widget policy, deprecated.")
    _write(kb, "universal/foundation/draft.md", id_="DOC-DRAFT", status="draft",
           title="Widget Draft", body="A proposed widget policy, draft.")
    return kb


def _idx(tmp_path: Path, index_path: Path) -> Index:
    kb = _build_status_kb(tmp_path)
    idx = Index(index_path)
    idx.build(kb, source_commit="test")
    return idx


# --- in_force filter -------------------------------------------------------


def test_in_force_includes_active_accepted_approved(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    hits = idx.search("widget", limit=20, in_force=True)
    ids = {h.id for h in hits}
    assert ids == {"DOC-ACTIVE", "DOC-ACCEPTED", "DOC-APPROVED"}


def test_in_force_excludes_superseded_and_deprecated(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    hits = idx.search("widget", limit=20, in_force=True)
    ids = {h.id for h in hits}
    assert "DOC-SUPERSEDED" not in ids
    assert "DOC-DEPRECATED" not in ids
    assert "DOC-DRAFT" not in ids


def test_in_force_is_hard_filter_not_soft_rerank(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """Without in_force, superseded/deprecated docs are RETURNED (only downranked);
    with in_force they must be absent entirely."""
    idx = _idx(tmp_path, tmp_index_path)
    plain_ids = {h.id for h in idx.search("widget", limit=20)}
    assert "DOC-SUPERSEDED" in plain_ids  # present when not filtered
    in_force_ids = {h.id for h in idx.search("widget", limit=20, in_force=True)}
    assert "DOC-SUPERSEDED" not in in_force_ids


def test_in_force_false_is_default_and_unchanged(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    default_ids = {h.id for h in idx.search("widget", limit=20)}
    explicit_ids = {h.id for h in idx.search("widget", limit=20, in_force=False)}
    assert default_ids == explicit_ids
    # All six status docs are surfaced when unfiltered.
    assert len(default_ids) == 6


def test_single_status_filter_still_works(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    hits = idx.search("widget", limit=20, status="superseded")
    ids = {h.id for h in hits}
    assert ids == {"DOC-SUPERSEDED"}


def test_status_and_in_force_compose(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    # accepted is in-force: status+in_force -> just the accepted doc.
    both = idx.search("widget", limit=20, status="accepted", in_force=True)
    assert {h.id for h in both} == {"DOC-ACCEPTED"}
    # superseded is NOT in-force: the AND yields nothing.
    none = idx.search("widget", limit=20, status="superseded", in_force=True)
    assert none == []


def test_in_force_class_is_single_source() -> None:
    """The class the engine filters on is exactly format.validate.IN_FORCE_STATUSES."""
    from data_olympus.index import _IN_FORCE_STATUS_LIST
    assert set(_IN_FORCE_STATUS_LIST) == IN_FORCE_STATUSES
    assert set(IN_FORCE_STATUSES) == {"active", "accepted", "approved"}


# --- abstain gate (single-sourced) -----------------------------------------


def test_abstain_gate_returns_none_on_disjoint_query(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    # A lexically-disjoint query matches no discriminating column -> gate fires.
    assert abstain_gate(idx, "quantum chromodynamics parrot", limit=5) is None


def test_abstain_gate_returns_hits_on_clear_signal(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    gated = abstain_gate(idx, "widget", limit=5)
    assert gated is not None
    assert gated  # non-empty: "widget" hits the title (a signal column)


def test_signal_columns_are_the_discriminating_ones() -> None:
    assert SIGNAL_COLUMNS == ["title", "tags", "applies_when"]


# --- kb_search_fn abstained shape ------------------------------------------


def test_kb_search_fn_abstained_shape(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    resp = kb_search_fn(idx=idx, query="quantum chromodynamics parrot", abstain=True)
    assert resp.abstained is True
    assert resp.abstain_reason == "no_signal_match"
    assert resp.hits == []
    assert resp.total_returned == 0


def test_kb_search_fn_no_abstain_when_signal(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    resp = kb_search_fn(idx=idx, query="widget", abstain=True)
    assert resp.abstained is False
    assert resp.abstain_reason is None
    assert resp.hits


def test_kb_search_fn_default_never_abstains(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    # A disjoint query with abstain OFF returns an ordinary empty result, NOT an
    # abstention (the two must be distinguishable).
    resp = kb_search_fn(idx=idx, query="quantum chromodynamics parrot")
    assert resp.abstained is False
    assert resp.abstain_reason is None
    assert resp.hits == []


def test_kb_search_fn_in_force_threads_through(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    resp = kb_search_fn(idx=idx, query="widget", in_force=True)
    ids = {h.id for h in resp.hits}
    assert ids == {"DOC-ACTIVE", "DOC-ACCEPTED", "DOC-APPROVED"}


# --- REST param threading --------------------------------------------------


def _status_http_app(tmp_path: Path):
    kb = _build_status_kb(tmp_path)
    app = build_app(
        kb_main_path=kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    return app.http_app()


@pytest.mark.asyncio
async def test_rest_in_force_filters(tmp_path: Path) -> None:
    http_app = _status_http_app(tmp_path)
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/search", params={"q": "widget", "in_force": "true"}
        )
    assert resp.status_code == 200
    ids = {h["id"] for h in resp.json()["hits"]}
    assert ids == {"DOC-ACTIVE", "DOC-ACCEPTED", "DOC-APPROVED"}


@pytest.mark.asyncio
async def test_rest_in_force_default_off(tmp_path: Path) -> None:
    http_app = _status_http_app(tmp_path)
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/search", params={"q": "widget"})
    ids = {h["id"] for h in resp.json()["hits"]}
    assert "DOC-SUPERSEDED" in ids  # unfiltered surfaces retired docs


@pytest.mark.asyncio
async def test_rest_abstain_threads(tmp_path: Path) -> None:
    http_app = _status_http_app(tmp_path)
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        fired = await client.get(
            "/api/v1/search",
            params={"q": "quantum chromodynamics parrot", "abstain": "true"},
        )
        ok = await client.get(
            "/api/v1/search", params={"q": "widget", "abstain": "true"}
        )
    fired_body = fired.json()
    assert fired_body["abstained"] is True
    assert fired_body["abstain_reason"] == "no_signal_match"
    assert fired_body["hits"] == []
    ok_body = ok.json()
    assert ok_body["abstained"] is False
    assert ok_body["hits"]
