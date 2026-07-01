"""Tests for the review fixes on the foundation PR (#50).

1. `/api/v1/health` must be served inline, NOT through the shared anyio worker
   pool, so a saturated pool cannot delay the readiness probe it needs to survive.
2. `ConsultationLedger` must bound an oversized persisted ledger on load (the
   hard max-entries cap), not only on the first new record().
3. `Index.search()` must over-fetch a candidate pool when a reranker is set and
   truncate the reranked result back to `limit` (fixes the status reranker being
   unable to promote a doc outside the BM25 window, and the id/tag reranker
   returning limit+1 rows).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from data_olympus.enforce_policy import ConsultationLedger
from data_olympus.index import Index, SearchHit
from data_olympus.server import build_app

if TYPE_CHECKING:
    from pathlib import Path


# --- 1. health served inline (off the shared worker pool) --------------------


@pytest.mark.asyncio
async def test_health_does_not_use_the_worker_pool(tmp_kb, tmp_path, monkeypatch) -> None:
    app = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
    )
    http_app = app.http_app()

    # If /api/v1/health went through _offload (anyio.to_thread.run_sync), breaking
    # run_sync would break the probe. Inline serving is unaffected.
    import data_olympus.rest_api as rest_api

    async def boom(*_a, **_k):
        raise RuntimeError("worker pool is saturated / unavailable")

    monkeypatch.setattr(rest_api.anyio.to_thread, "run_sync", boom)

    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/api/v1/health")
    assert resp.status_code in (200, 503)  # served, not crashed
    assert "kb_commit" in resp.json()


# --- 2. ledger bounded on load ----------------------------------------------


def test_persisted_ledger_is_capped_on_load(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    rows = [
        {"session_id": f"s{i}", "workspace": "w", "consulted_at": float(i), "rule_ids": []}
        for i in range(50)
    ]
    path.write_text(json.dumps(rows))

    led = ConsultationLedger(path=str(path), retention_sec=1e12, max_entries=10)
    # Oversized file must not load 50 entries into memory; capped to newest 10.
    assert len(led._entries) == 10
    # Newest by consulted_at survive (s40..s49).
    assert led.get(session_id="s49", workspace="w") is not None
    assert led.get(session_id="s0", workspace="w") is None


# --- 3. search over-fetch + truncate around a reranker -----------------------


def test_reranker_receives_wider_pool_than_limit(tmp_kb, tmp_index_path) -> None:
    seen = {}

    def spy(_query: str, hits: list[SearchHit]) -> list[SearchHit]:
        seen["n"] = len(hits)
        return hits

    idx = Index(tmp_index_path, reranker=spy)
    idx.build(tmp_kb, source_commit="abc")
    idx.search("STD", limit=2)  # tmp_kb has >2 STD docs
    assert seen["n"] > 2, "reranker should see a candidate pool wider than limit"


def test_reranked_results_truncated_to_limit(tmp_kb, tmp_index_path) -> None:
    def prepend_extra(_query: str, hits: list[SearchHit]) -> list[SearchHit]:
        extra = SearchHit(id="SYNTH", path="p", title="t", snippet="", score=-999.0)
        return [extra, *hits]

    idx = Index(tmp_index_path, reranker=prepend_extra)
    idx.build(tmp_kb, source_commit="abc")
    hits = idx.search("STD", limit=2)
    assert len(hits) == 2, "prepended synthetic hit must not push result past limit"
    assert hits[0].id == "SYNTH"


def test_no_reranker_keeps_exact_limit(tmp_kb, tmp_index_path) -> None:
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="abc")
    assert len(idx.search("STD", limit=2)) == 2  # unchanged default path
