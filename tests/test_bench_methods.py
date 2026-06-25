from __future__ import annotations

from typing import TYPE_CHECKING

from benchmarks.corpus_gen import generate_corpus

if TYPE_CHECKING:
    from pathlib import Path
from benchmarks.methods.base import RetrievalResult
from benchmarks.methods.bm25 import Bm25Method
from benchmarks.methods.data_olympus import DataOlympusMethod
from benchmarks.methods.grep_read import GrepReadMethod
from benchmarks.methods.whole_dump import WholeDumpMethod
from data_olympus.index import Index


def _corpus(tmp_path: Path):
    root = tmp_path / "kb"
    manifest = generate_corpus(root, n=120, seed=5)
    return root, manifest


def test_whole_dump_returns_everything(tmp_path: Path) -> None:
    root, manifest = _corpus(tmp_path)
    m = WholeDumpMethod(root)
    res = m.retrieve("caching")
    assert isinstance(res, RetrievalResult)
    assert len(res.retrieved_ids) == len(manifest.concepts)


def test_grep_read_finds_topic(tmp_path: Path) -> None:
    root, manifest = _corpus(tmp_path)
    m = GrepReadMethod(root)
    res = m.retrieve("caching")
    assert any("CACHING" in rid for rid in res.retrieved_ids)


def test_bm25_ranks_topic_concept_first(tmp_path: Path) -> None:
    root, _ = _corpus(tmp_path)
    m = Bm25Method(root, k=5)
    res = m.retrieve("pagination")
    assert res.ranked_ids
    assert "PAGINATION" in res.ranked_ids[0]


def test_data_olympus_status_filter_excludes_superseded(tmp_path: Path) -> None:
    root, manifest = _corpus(tmp_path)
    idx = Index(tmp_path / "idx.db")
    idx.build(root, source_commit="bench")
    m = DataOlympusMethod(idx)
    pair = next(t for t in manifest.topics if t.stale_id is not None)
    res = m.retrieve(pair.topic.replace("-", " "))
    assert pair.stale_id not in res.ranked_ids, "active filter must drop the superseded concept"


def test_data_olympus_payload_smaller_than_dump(tmp_path: Path) -> None:
    from benchmarks.tokenizer import SimpleTokenizer
    root, _ = _corpus(tmp_path)
    idx = Index(tmp_path / "idx.db")
    idx.build(root, source_commit="bench")
    tok = SimpleTokenizer()
    do = DataOlympusMethod(idx).retrieve("caching")
    dump = WholeDumpMethod(root).retrieve("caching")
    assert tok.count(do.payload_text) < tok.count(dump.payload_text)


def test_vector_rag_ranks_topic_when_available(tmp_path: Path) -> None:
    import pytest
    pytest.importorskip("sentence_transformers")
    from benchmarks.methods.vector_rag import VectorRagMethod
    root, _ = _corpus(tmp_path)
    m = VectorRagMethod(root, k=5)
    res = m.retrieve("pagination")
    assert res.ranked_ids  # at least returns something
