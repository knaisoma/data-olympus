from __future__ import annotations

from pathlib import Path

from benchmarks.corpus_gen import generate_corpus
from benchmarks.query_gen import build_queries, load_queries, write_queries


def test_build_queries_covers_all_categories(tmp_path: Path) -> None:
    manifest = generate_corpus(tmp_path / "kb", n=150, seed=3)
    queries = build_queries(manifest)
    cats = {q.category for q in queries}
    assert {"exact", "semantic", "status", "graph"} <= cats


def test_status_queries_carry_stale_id(tmp_path: Path) -> None:
    manifest = generate_corpus(tmp_path / "kb", n=150, seed=3)
    status_qs = [q for q in build_queries(manifest) if q.category == "status"]
    assert status_qs
    for q in status_qs:
        assert q.stale_id is not None
        assert q.gold_ids == [q.current_id]


def test_gold_ids_exist_in_corpus(tmp_path: Path) -> None:
    manifest = generate_corpus(tmp_path / "kb", n=150, seed=3)
    valid = {c.id for c in manifest.concepts}
    for q in build_queries(manifest):
        for gid in q.gold_ids:
            assert gid in valid, f"gold id {gid} not in corpus"


def test_write_then_load_roundtrips(tmp_path: Path) -> None:
    manifest = generate_corpus(tmp_path / "kb", n=80, seed=2)
    queries = build_queries(manifest)
    out = tmp_path / "queries.yaml"
    write_queries(queries, out)
    loaded = load_queries(out)
    assert [q.text for q in loaded] == [q.text for q in queries]
    assert [q.gold_ids for q in loaded] == [q.gold_ids for q in queries]
