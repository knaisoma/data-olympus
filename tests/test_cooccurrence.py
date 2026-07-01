"""Tests for corpus co-occurrence query expansion (issue #40).

Embedding-free semantic broadening. At build time the index learns, per term,
the handful of terms it most strongly co-occurs with (by PMI at document
granularity) into a bounded ``related_terms`` table swapped atomically with the
FTS index. At query time an expander appends those related terms (down-weighted)
via the ``query_expander`` seam, broadening recall.

These tests cover the pure table-builder + expander units, the SQLite
persistence, an end-to-end index search proving an unambiguous association
broadens recall while an unrelated query is unaffected, and the atomic rebuild.
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from data_olympus.cooccurrence import (
    build_cooccurrence_table,
    compose_expanders,
    cooccurrence_build_params,
    cooccurrence_enabled,
    is_reasonable_token,
    lookup_related_terms,
    make_cooccurrence_expander,
    tokenize_doc,
    write_cooccurrence_table,
)
from data_olympus.index import Index

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# --- tokenization ------------------------------------------------------------


def test_tokenize_drops_short_and_stopwords() -> None:
    tokens = tokenize_doc("The quick kubernetes db is deployed via helm")
    # "the", "is", "via" are stopwords; "db" is too short.
    assert "kubernetes" in tokens
    assert "quick" in tokens
    assert "deployed" in tokens
    assert "helm" in tokens
    assert "the" not in tokens
    assert "is" not in tokens
    assert "db" not in tokens


def test_tokenize_is_a_set_per_doc() -> None:
    # A term repeated in one doc counts once (document granularity).
    tokens = tokenize_doc("kafka kafka kafka streaming")
    assert tokens == {"kafka", "streaming"}


def test_is_reasonable_token() -> None:
    assert is_reasonable_token("kubernetes")
    assert is_reasonable_token("k8s")  # letter then digits
    assert not is_reasonable_token("db")  # too short
    assert not is_reasonable_token("the")  # stopword
    assert not is_reasonable_token("123")  # not letter-initial


# --- pure table builder ------------------------------------------------------


def test_build_table_relates_cooccurring_terms() -> None:
    # "helm" and "kubernetes" always appear together; "banana" never with them.
    docs = [
        {"helm", "kubernetes", "deploy"},
        {"helm", "kubernetes", "chart"},
        {"helm", "kubernetes", "release"},
        {"banana", "fruit", "yellow"},
        {"banana", "fruit", "smoothie"},
    ]
    table = build_cooccurrence_table(docs, k=5, min_count=2, min_pmi=0.0)
    assert "kubernetes" in table["helm"]
    assert "helm" in table["kubernetes"]
    # The unrelated cluster does not leak in.
    assert "banana" not in table.get("helm", [])
    assert "helm" not in table.get("banana", [])


def test_build_table_respects_top_k_bound() -> None:
    # One hub term co-occurs with many others; each partner appears together with
    # the hub often enough to clear min_count. top-k must cap the hub's list.
    docs = []
    partners = [f"term{i}" for i in range(10)]
    for _ in range(3):
        for p in partners:
            docs.append({"hub", p})
    table = build_cooccurrence_table(docs, k=3, min_count=2, min_pmi=-10.0)
    assert len(table["hub"]) <= 3


def test_build_table_honours_min_count() -> None:
    # A pair co-occurring in only one doc is below min_count=2 and excluded.
    docs = [{"alpha", "beta"}, {"gamma", "delta"}, {"gamma", "delta"}]
    table = build_cooccurrence_table(docs, k=5, min_count=2, min_pmi=-10.0)
    assert "beta" not in table.get("alpha", [])
    assert "delta" in table.get("gamma", [])


def test_build_table_empty_for_tiny_corpus() -> None:
    assert build_cooccurrence_table([{"a", "b"}], k=5) == {}
    assert build_cooccurrence_table([], k=5) == {}


# --- SQLite persistence ------------------------------------------------------


def test_write_and_lookup_related_terms(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    from data_olympus.cooccurrence import RELATED_TERMS_SCHEMA

    conn.executescript(RELATED_TERMS_SCHEMA)
    write_cooccurrence_table(conn, {"helm": ["kubernetes", "chart"]})
    conn.commit()
    got = lookup_related_terms(conn, "helm", limit=5)
    assert got == ["kubernetes", "chart"]  # rank order preserved
    assert lookup_related_terms(conn, "unknown", limit=5) == []
    conn.close()


# --- pure expander -----------------------------------------------------------


def test_expander_appends_related_after_originals() -> None:
    lookup = {"helm": ["kubernetes"]}
    expander = make_cooccurrence_expander(
        lambda term, k: lookup.get(term, [])[:k], k=5
    )
    out = expander(["helm"])
    assert out[0] == "helm"  # original first (down-weighting by position)
    assert "kubernetes" in out


def test_expander_is_bounded_and_deduped() -> None:
    lookup = {"a": [f"r{i}" for i in range(50)]}
    expander = make_cooccurrence_expander(
        lambda term, k: lookup.get(term, [])[:k], k=50, max_terms=10
    )
    out = expander(["a"])
    assert len(out) <= 10
    assert len(out) == len(set(out))


def test_expander_unaffected_for_unrelated_term() -> None:
    expander = make_cooccurrence_expander(lambda _term, _k: [], k=5)
    assert expander(["banana"]) == ["banana"]


# --- composition -------------------------------------------------------------


def test_compose_runs_left_to_right() -> None:
    syn = make_cooccurrence_expander(
        lambda term, k: {"k8s": ["kubernetes"]}.get(term, [])[:k], k=5
    )
    cooc = make_cooccurrence_expander(
        lambda term, k: {"kubernetes": ["helm"]}.get(term, [])[:k], k=5
    )
    composed = compose_expanders(syn, cooc)
    assert composed is not None
    out = composed(["k8s"])
    # synonym adds kubernetes, then cooccurrence (on the expanded set) adds helm.
    assert out[0] == "k8s"
    assert "kubernetes" in out
    assert "helm" in out


def test_compose_skips_none_and_returns_none_when_empty() -> None:
    assert compose_expanders(None, None) is None
    only = make_cooccurrence_expander(lambda _term, _k: [], k=5)
    composed = compose_expanders(None, only)
    assert composed is not None
    assert composed(["x"]) == ["x"]


def test_compose_bounds_total() -> None:
    a = make_cooccurrence_expander(
        lambda _term, k: [f"a{i}" for i in range(20)][:k], k=20, max_terms=100
    )
    b = make_cooccurrence_expander(
        lambda _term, k: [f"b{i}" for i in range(20)][:k], k=20, max_terms=100
    )
    composed = compose_expanders(a, b, max_terms=8)
    assert composed is not None
    assert len(composed(["seed"])) <= 8


# --- env config --------------------------------------------------------------


def test_cooccurrence_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_COOCCURRENCE_MODE", raising=False)
    assert cooccurrence_enabled() is True
    monkeypatch.setenv("KB_COOCCURRENCE_MODE", "off")
    assert cooccurrence_enabled() is False
    monkeypatch.setenv("KB_COOCCURRENCE_MODE", "on")
    assert cooccurrence_enabled() is True


def test_cooccurrence_build_params_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_COOCCURRENCE_K", "3")
    monkeypatch.setenv("KB_COOCCURRENCE_MIN_COUNT", "4")
    monkeypatch.setenv("KB_COOCCURRENCE_MIN_PMI", "1.5")
    params = cooccurrence_build_params()
    assert params == {"k": 3, "min_count": 4, "min_pmi": 1.5}


# --- end-to-end through Index ------------------------------------------------


def _write_cooccurrence_corpus(kb: Path) -> None:
    """A corpus where 'helm' and 'kubernetes' are unambiguously associated.

    Multiple docs mention BOTH helm and kubernetes; one doc mentions kubernetes
    WITHOUT helm. A search for 'helm' should, via co-occurrence expansion, also
    surface the kubernetes-only doc. An unrelated 'banana' cluster is isolated.
    """
    d = kb / "universal" / "foundation"
    d.mkdir(parents=True, exist_ok=True)
    # Each helm doc pairs helm+kubernetes and adds DISTINCT filler, so no filler
    # word co-occurs with helm as often as kubernetes does. That makes
    # helm<->kubernetes the strongest (highest-count) association, so kubernetes
    # wins a top-k slot unambiguously.
    fillers = [
        "chart templates release",
        "deploy rollout upgrade",
        "namespace values override",
        "repository registry pull",
    ]
    for i, filler in enumerate(fillers):
        (d / f"STD-HELM-{i}.md").write_text(
            f"---\nid: STD-HELM-{i}\n---\n\n# Helm deployment {i}\n\n"
            f"Using helm to install onto kubernetes. {filler}.\n",
            encoding="utf-8",
        )
    # kubernetes WITHOUT the word helm -- only co-occurrence expansion reaches it.
    (d / "STD-KUBEONLY.md").write_text(
        "---\nid: STD-KUBEONLY\n---\n\n# Kubernetes operations\n\n"
        "Operating kubernetes clusters: nodes, pods, and the kubernetes control "
        "plane. Scaling kubernetes workloads across the kubernetes cluster.\n",
        encoding="utf-8",
    )
    # Unrelated cluster -- must be unaffected by helm/kubernetes expansion.
    (d / "STD-BANANA.md").write_text(
        "---\nid: STD-BANANA\n---\n\n# Banana smoothies\n\n"
        "A banana smoothie recipe: banana, yoghurt, honey. Blend the banana "
        "until the smoothie is smooth.\n",
        encoding="utf-8",
    )


def test_index_related_terms_populated_after_build(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    _write_cooccurrence_corpus(kb)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    related = idx.related_terms("helm", limit=5)
    assert "kubernetes" in related


def test_cooccurrence_expansion_broadens_recall(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    _write_cooccurrence_corpus(kb)

    # Baseline: no expander -- 'helm' must NOT reach the kubernetes-only doc.
    plain = Index(tmp_index_path)
    plain.build(kb, source_commit="x")
    plain_ids = {h.id for h in plain.search("helm", limit=20)}
    assert "STD-KUBEONLY" not in plain_ids, (
        "baseline should not reach the kubernetes-only doc from a helm query"
    )

    # With co-occurrence expansion: 'helm' expands to include 'kubernetes',
    # reaching the kubernetes-only doc -- recall broadened.
    expanded = Index(tmp_index_path)
    expanded.query_expander = expanded.cooccurrence_expander()
    expanded_ids = {h.id for h in expanded.search("helm", limit=20)}
    assert "STD-KUBEONLY" in expanded_ids, (
        "co-occurrence expansion should reach the kubernetes-only doc"
    )

    # An unrelated query is unaffected: 'banana' does not drag in helm/kube docs.
    banana_ids = {h.id for h in expanded.search("banana", limit=20)}
    assert banana_ids == {"STD-BANANA"}


def test_related_terms_table_rebuilt_atomically(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """The related_terms table swaps atomically with the rest of the index.

    An open connection to the old inode keeps seeing the OLD table while a
    rebuild produces a new one; a fresh connection sees the new table. This
    mirrors the FTS atomic-swap guarantee for the co-occurrence table.
    """
    kb = tmp_path / "kb"
    kb.mkdir()
    _write_cooccurrence_corpus(kb)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="first")

    # Hold a connection open to the current inode.
    old_conn = sqlite3.connect(tmp_index_path)
    old_inode = tmp_index_path.stat().st_ino
    old_related = lookup_related_terms(old_conn, "helm", limit=5)
    assert "kubernetes" in old_related

    # Rebuild: the whole index (incl. related_terms) is swapped atomically.
    idx.build(kb, source_commit="second")
    assert tmp_index_path.stat().st_ino != old_inode, (
        "os.replace should produce a new inode"
    )

    # The old connection still answers from the old inode's related_terms table.
    assert lookup_related_terms(old_conn, "helm", limit=5) == old_related
    old_conn.close()

    # A fresh connection sees the new build's table (also well-formed).
    new_conn = sqlite3.connect(tmp_index_path)
    assert "kubernetes" in lookup_related_terms(new_conn, "helm", limit=5)
    new_conn.close()


def test_cooccurrence_disabled_skips_table(
    tmp_path: Path, tmp_index_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_COOCCURRENCE_MODE", "off")
    kb = tmp_path / "kb"
    kb.mkdir()
    _write_cooccurrence_corpus(kb)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    # Disabled -> empty table -> no related terms.
    assert idx.related_terms("helm", limit=5) == []
