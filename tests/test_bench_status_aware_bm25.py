"""Tests for the status-aware BM25 baseline and serves-stale semantics."""
from __future__ import annotations

from typing import TYPE_CHECKING

from benchmarks.corpus_gen import generate_corpus
from benchmarks.methods.bm25 import Bm25Method, StatusAwareBm25Method
from benchmarks.query_gen import build_queries

if TYPE_CHECKING:
    from pathlib import Path


def _supersession_topic(manifest):  # noqa: ANN001
    return next(t for t in manifest.topics if t.stale_id is not None)


def test_status_aware_excludes_superseded_doc(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    m = generate_corpus(root, n=250, seed=0)
    pair = _supersession_topic(m)

    plain = Bm25Method(root, k=5)
    aware = StatusAwareBm25Method(root, k=5)

    # An exact query on the topic: plain BM25 retrieves BOTH docs (identical
    # bodies), status-aware retrieves only the current one.
    q = next(x for x in build_queries(m)
             if x.category == "exact" and x.stale_id == pair.stale_id)
    plain_ids = plain.retrieve(q.text).retrieved_ids
    aware_ids = aware.retrieve(q.text).retrieved_ids

    assert pair.stale_id in plain_ids, "plain BM25 must retrieve the stale doc"
    assert pair.stale_id not in aware_ids, "status-aware BM25 must drop the stale doc"
    assert pair.current_id in aware_ids, "status-aware BM25 keeps the current doc"


def test_status_aware_is_a_ranking_method(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    generate_corpus(root, n=60, seed=1)
    assert StatusAwareBm25Method(root, k=5).ranks is True
    assert Bm25Method(root, k=5).ranks is True


def test_status_aware_never_serves_stale_on_lifecycle(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    m = generate_corpus(root, n=250, seed=0)
    aware = StatusAwareBm25Method(root, k=5)
    lifecycle = [q for q in build_queries(m)
                 if q.stale_id is not None and q.category in ("status", "graph")]
    assert lifecycle
    for q in lifecycle:
        assert q.stale_id not in aware.retrieve(q.text).retrieved_ids


def test_plain_bm25_serves_stale_on_lifecycle(tmp_path: Path) -> None:
    # The whole point of the contrast: a status-blind ranker DOES serve the stale
    # doc, since old and new are lexically identical in the de-leaked corpus.
    root = tmp_path / "kb"
    m = generate_corpus(root, n=250, seed=0)
    plain = Bm25Method(root, k=5)
    lifecycle = [q for q in build_queries(m)
                 if q.stale_id is not None and q.category in ("status", "graph")]
    served = sum(
        1 for q in lifecycle if q.stale_id in plain.retrieve(q.text).retrieved_ids
    )
    assert served > 0, "plain BM25 must serve the stale doc on lifecycle queries"
