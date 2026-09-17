"""Tests for kb_curate (issue #31, first slice): lists in-force documents
that are review-due, most-overdue first. Advisory and human-gated -- this
tool recommends, it never proposes, edits, promotes or demotes anything.
Pattern promotion (issue #31's other half) is explicitly NOT in scope here.

Reuses compute_freshness (issue #142) for the reason, so the two surfaces
cannot disagree about what "review-due" means.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from data_olympus.index import Index
from data_olympus.server import build_app
from data_olympus.tools_curate import kb_curate_fn

if TYPE_CHECKING:
    from pathlib import Path

TODAY = "2026-07-08"


def _write(
    kb: Path, rel: str, *, id_: str, status: str = "active", validity: str = "",
) -> None:
    p = kb / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nid: {id_}\ntype: standard\nstatus: {status}\ntier: T1\n"
        f"category: foundation\ntitle: {id_}\n{validity}---\n# {id_}\n\nbody\n",
        encoding="utf-8",
    )


def _idx(tmp_path: Path, tmp_index_path: Path, builder) -> Index:
    kb = tmp_path / "kb"
    kb.mkdir()
    builder(kb)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="test")
    return idx


# ---------------------------------------------------------------------------
# Empty corpus / nothing review-due: valid empty result, not an error
# (matching the behaviour verified for other read tools while checking #156).
# ---------------------------------------------------------------------------

def test_empty_index_returns_empty_result_not_error(tmp_index_path) -> None:
    idx = Index(tmp_index_path)
    resp = kb_curate_fn(idx=idx, today=TODAY, review_due_after_days=90)
    assert resp.entries == []
    assert resp.total == 0


def test_nothing_review_due_returns_empty_result(tmp_path, tmp_index_path) -> None:
    """A recently-verified doc is genuinely fresh, not review-due, even with
    the derivation on. A doc with NO validity block at all is a DIFFERENT
    case (never verified) covered by test_lists_doc_with_no_last_verified_at_all."""
    idx = _idx(
        tmp_path, tmp_index_path,
        lambda kb: _write(
            kb, "a.md", id_="DOC-FRESH",
            validity="validity:\n  last_verified: 2026-07-01\n",
        ),
    )
    resp = kb_curate_fn(idx=idx, today=TODAY, review_due_after_days=90)
    assert resp.entries == []
    assert resp.total == 0


def test_review_due_after_days_unset_returns_empty_result(tmp_path, tmp_index_path) -> None:
    """The feature-off default: no doc is ever reported review-due, even one
    that has never been verified, matching the pre-#142 behaviour."""
    idx = _idx(
        tmp_path, tmp_index_path,
        lambda kb: _write(kb, "a.md", id_="DOC-NEVER-VERIFIED"),
    )
    resp = kb_curate_fn(idx=idx, today=TODAY, review_due_after_days=None)
    assert resp.entries == []


# ---------------------------------------------------------------------------
# Review-due detection: both stale sources (#142) surface here.
# ---------------------------------------------------------------------------

def test_lists_doc_with_old_last_verified(tmp_path, tmp_index_path) -> None:
    idx = _idx(
        tmp_path, tmp_index_path,
        lambda kb: _write(
            kb, "a.md", id_="DOC-OLD",
            validity="validity:\n  last_verified: 2025-01-01\n",
        ),
    )
    resp = kb_curate_fn(idx=idx, today=TODAY, review_due_after_days=90)
    assert [e.id for e in resp.entries] == ["DOC-OLD"]
    assert "last_verified" in resp.entries[0].reason


def test_lists_doc_with_no_last_verified_at_all(tmp_path, tmp_index_path) -> None:
    idx = _idx(tmp_path, tmp_index_path, lambda kb: _write(kb, "a.md", id_="DOC-NEVER"))
    resp = kb_curate_fn(idx=idx, today=TODAY, review_due_after_days=90)
    assert [e.id for e in resp.entries] == ["DOC-NEVER"]
    assert "not set" in resp.entries[0].reason


def test_lists_doc_with_recheck_by_in_the_past(tmp_path, tmp_index_path) -> None:
    idx = _idx(
        tmp_path, tmp_index_path,
        lambda kb: _write(
            kb, "a.md", id_="DOC-STALE",
            validity="validity:\n  recheck_by: 2026-01-01\n",
        ),
    )
    # No review_due_after_days needed: recheck_by alone triggers stale,
    # exactly as before #142.
    resp = kb_curate_fn(idx=idx, today=TODAY, review_due_after_days=None)
    assert [e.id for e in resp.entries] == ["DOC-STALE"]
    assert "recheck_by" in resp.entries[0].reason


def test_future_recheck_by_override_excludes_a_long_unverified_doc(
    tmp_path, tmp_index_path,
) -> None:
    """The recheck_by override composes correctly: an operator who
    deliberately deferred review is not second-guessed by kb_curate."""
    idx = _idx(
        tmp_path, tmp_index_path,
        lambda kb: _write(
            kb, "a.md", id_="DOC-DEFERRED",
            validity="validity:\n  last_verified: 2020-01-01\n  recheck_by: 2027-01-01\n",
        ),
    )
    resp = kb_curate_fn(idx=idx, today=TODAY, review_due_after_days=30)
    assert resp.entries == []


# ---------------------------------------------------------------------------
# Ordering: most-overdue first.
# ---------------------------------------------------------------------------

def test_orders_most_overdue_first(tmp_path, tmp_index_path) -> None:
    def builder(kb):
        _write(kb, "recent.md", id_="DOC-RECENT-OVERDUE",
               validity="validity:\n  last_verified: 2026-05-01\n")  # ~68 days
        _write(kb, "ancient.md", id_="DOC-ANCIENT-OVERDUE",
               validity="validity:\n  last_verified: 2020-01-01\n")  # years
        _write(kb, "fresh.md", id_="DOC-FRESH",
               validity="validity:\n  last_verified: 2026-07-01\n")  # not overdue

    idx = _idx(tmp_path, tmp_index_path, builder)
    resp = kb_curate_fn(idx=idx, today=TODAY, review_due_after_days=30)
    assert [e.id for e in resp.entries] == ["DOC-ANCIENT-OVERDUE", "DOC-RECENT-OVERDUE"]


def test_never_verified_sorts_ahead_of_a_merely_old_verification(
    tmp_path, tmp_index_path,
) -> None:
    """A document nobody has EVER verified is the most urgent case -- it
    sorts ahead of one that was at least checked once, long ago."""
    def builder(kb):
        _write(kb, "old.md", id_="DOC-OLD",
               validity="validity:\n  last_verified: 2020-01-01\n")
        _write(kb, "never.md", id_="DOC-NEVER")

    idx = _idx(tmp_path, tmp_index_path, builder)
    resp = kb_curate_fn(idx=idx, today=TODAY, review_due_after_days=30)
    assert [e.id for e in resp.entries] == ["DOC-NEVER", "DOC-OLD"]


# ---------------------------------------------------------------------------
# limit and exclusions.
# ---------------------------------------------------------------------------

def test_respects_limit(tmp_path, tmp_index_path) -> None:
    def builder(kb):
        for i in range(5):
            _write(kb, f"d{i}.md", id_=f"DOC-{i}",
                   validity="validity:\n  last_verified: 2020-01-01\n")

    idx = _idx(tmp_path, tmp_index_path, builder)
    resp = kb_curate_fn(idx=idx, today=TODAY, review_due_after_days=30, limit=2)
    assert len(resp.entries) == 2
    assert resp.total == 2


def test_excludes_out_of_force_docs(tmp_path, tmp_index_path) -> None:
    """A superseded/expired doc never appears, even if wildly overdue: it is
    not in force, so it does not govern and does not need review pressure."""
    def builder(kb):
        _write(kb, "old.md", id_="DOC-SUPERSEDED", status="superseded",
               validity="validity:\n  last_verified: 2020-01-01\n")
        _write(kb, "expired.md", id_="DOC-EXPIRED",
               validity="validity:\n  valid_until: 2020-01-01\n  last_verified: 2020-01-01\n")

    idx = _idx(tmp_path, tmp_index_path, builder)
    resp = kb_curate_fn(idx=idx, today=TODAY, review_due_after_days=30)
    assert resp.entries == []


# ---------------------------------------------------------------------------
# Advisory: kb_curate never mutates anything (it has no write dependency at
# all -- verified by the function's own signature taking only idx).
# ---------------------------------------------------------------------------

def test_kb_curate_fn_signature_has_no_write_dependency() -> None:
    import inspect
    params = set(inspect.signature(kb_curate_fn).parameters)
    assert not (params & {"pending", "worktrees", "push_queue", "audit_log"})


# ---------------------------------------------------------------------------
# MCP + REST wiring, matching the existing read tools' auth posture (no auth
# required, same as kb_search/kb_get -- NOT gated like kb_list_pending).
# ---------------------------------------------------------------------------

def _http_app(tmp_path: Path, builder):
    kb = tmp_path / "kb"
    kb.mkdir()
    builder(kb)
    app = build_app(
        kb_main_path=kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
        review_due_after_days=1,
    )
    return app.http_app()


@pytest.mark.asyncio
async def test_rest_curate_route_returns_review_due_docs(tmp_path: Path) -> None:
    def builder(kb):
        _write(kb, "old.md", id_="DOC-OLD",
               validity="validity:\n  last_verified: 2020-01-01\n")

    http_app = _http_app(tmp_path, builder)
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/curate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"][0]["id"] == "DOC-OLD"


@pytest.mark.asyncio
async def test_rest_curate_route_needs_no_auth_token(tmp_path: Path) -> None:
    """Matches kb_search/kb_get's posture, not kb_list_pending's: this tool
    surfaces document metadata already retrievable through ordinary reads."""
    def builder(kb):
        _write(kb, "a.md", id_="DOC-A")

    http_app = _http_app(tmp_path, builder)
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/curate")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_mcp_kb_curate_tool_is_registered(tmp_path: Path) -> None:
    from fastmcp import Client

    def builder(kb):
        _write(kb, "old.md", id_="DOC-OLD",
               validity="validity:\n  last_verified: 2020-01-01\n")

    kb = tmp_path / "kb"
    kb.mkdir()
    builder(kb)
    app = build_app(
        kb_main_path=kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
        review_due_after_days=1,
    )
    async with Client(app) as client:
        result = await client.call_tool("kb_curate", {})
    assert result.data["entries"][0]["id"] == "DOC-OLD"


# ---------------------------------------------------------------------------
# Malformed recheck_by must not crash kb_curate for the whole corpus
# (found in implementation review): compute_freshness's recheck_by<today
# check is a lexical string comparison, unchanged since before #142, so a
# malformed-but-lexically-earlier recheck_by (e.g. a truncated "2026") is
# already classified "stale" upstream. _overdue_days must not crash on it.
# ---------------------------------------------------------------------------

def test_malformed_recheck_by_does_not_crash_curate(tmp_path, tmp_index_path) -> None:
    """A value this malformed cannot reach the index through normal ingestion
    (normalize_validity_date rejects it at parse time and drops the whole
    validity block, confirmed by building a real corpus with this exact
    frontmatter first). It CAN reach the docs table through a source that
    writes SQLite directly and skips that normalization -- an older schema
    version, or tooling other than this build path -- which is the scenario
    this test constructs, matching the precedent in test_index.py for
    exercising columns through the schema rather than through ingestion."""
    import sqlite3

    idx = _idx(tmp_path, tmp_index_path, lambda kb: _write(kb, "a.md", id_="DOC-A"))
    conn = sqlite3.connect(tmp_index_path)
    conn.execute(
        "UPDATE docs SET recheck_by = ? WHERE id = 'DOC-A'", ("2026",),
    )
    conn.commit()
    conn.close()

    resp = kb_curate_fn(idx=idx, today=TODAY, review_due_after_days=None)

    assert [e.id for e in resp.entries] == ["DOC-A"]


def test_overdue_days_malformed_recheck_by_does_not_raise() -> None:
    from data_olympus.tools_curate import _overdue_days

    result = _overdue_days(
        recheck_by="2026", last_verified="", today=TODAY, review_due_after_days=None,
    )
    assert result == float("inf")
