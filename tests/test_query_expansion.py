"""Tests for synonym/acronym query expansion (issue #38).

The expander plugs into the ``query_expander`` seam established by the search
pipeline (issue #36): it rewrites the term list before the FTS5 MATCH is built,
and because terms are OR-matched, adding curated synonyms broadens recall
(e.g. a ``k8s`` query also matches documents mentioning ``kubernetes``).

These tests cover the pure expander unit (bounded, de-duplicated, order-stable)
and an end-to-end index search proving the short form finds the long form.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.index import Index
from data_olympus.query_expansion import (
    DEFAULT_SYNONYMS,
    build_synonym_map,
    load_synonyms_from_env,
    make_synonym_expander,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# --- pure expander unit ------------------------------------------------------


def test_expander_adds_synonyms_bidirectionally() -> None:
    expander = make_synonym_expander({"k8s": ["kubernetes"]})
    # short -> long
    assert set(expander(["k8s"])) == {"k8s", "kubernetes"}
    # long -> short (map is symmetrised)
    assert set(expander(["kubernetes"])) == {"kubernetes", "k8s"}


def test_expander_preserves_original_terms_first() -> None:
    expander = make_synonym_expander({"k8s": ["kubernetes"]})
    out = expander(["deploy", "k8s"])
    # original terms come first, in order; synonyms appended after.
    assert out[:2] == ["deploy", "k8s"]
    assert "kubernetes" in out


def test_expander_is_case_insensitive_on_lookup() -> None:
    expander = make_synonym_expander({"auth": ["authentication"]})
    out = expander(["AUTH"])
    # the original term keeps its casing; synonym is added.
    assert "AUTH" in out
    assert "authentication" in out


def test_expander_deduplicates() -> None:
    expander = make_synonym_expander({"k8s": ["kubernetes"]})
    out = expander(["k8s", "kubernetes"])
    assert out.count("k8s") == 1
    assert out.count("kubernetes") == 1


def test_expander_is_bounded() -> None:
    # A term with many synonyms plus a large query must not explode past the cap.
    big = {"x": [f"syn{i}" for i in range(100)]}
    expander = make_synonym_expander(big, max_terms=8)
    out = expander(["x", "a", "b", "c"])
    assert len(out) <= 8
    # originals are never dropped by the cap.
    for t in ("x", "a", "b", "c"):
        assert t in out


def test_expander_passthrough_when_no_match() -> None:
    expander = make_synonym_expander({"k8s": ["kubernetes"]})
    assert expander(["hello", "world"]) == ["hello", "world"]


def test_default_map_covers_expected_acronyms() -> None:
    sym = build_synonym_map(DEFAULT_SYNONYMS)
    assert "kubernetes" in sym["k8s"]
    assert "k8s" in sym["kubernetes"]
    assert "authentication" in sym["auth"]
    assert "auth" in sym["authentication"]
    assert "rls" in sym
    assert "adr" in sym


# --- end-to-end via the Index search seam ------------------------------------


def test_search_short_form_finds_long_form(tmp_kb: Path, tmp_index_path: Path) -> None:
    # The tmp_kb corpus does not mention "kubernetes"; add a doc that does.
    (tmp_kb / "tooling" / "deploy.md").write_text(
        "# Deploy\n\nWe run workloads on kubernetes clusters.\n"
    )
    expander = make_synonym_expander(build_synonym_map(DEFAULT_SYNONYMS))
    idx = Index(tmp_index_path, query_expander=expander)
    idx.build(tmp_kb, source_commit="abc")
    hits = idx.search("k8s", limit=10)
    assert hits, "k8s query should reach the kubernetes doc via synonym expansion"
    assert any("kubernetes" in (h.snippet + h.title).lower() for h in hits)


def test_search_long_form_finds_short_form(tmp_kb: Path, tmp_index_path: Path) -> None:
    (tmp_kb / "tooling" / "k8s-notes.md").write_text(
        "# k8s notes\n\nCluster runs on k8s and microk8s.\n"
    )
    expander = make_synonym_expander(build_synonym_map(DEFAULT_SYNONYMS))
    idx = Index(tmp_index_path, query_expander=expander)
    idx.build(tmp_kb, source_commit="abc")
    hits = idx.search("kubernetes", limit=10)
    assert hits, "kubernetes query should reach the k8s doc via synonym expansion"
    assert any("k8s" in (h.snippet + h.title).lower() for h in hits)


def test_search_without_expander_misses_synonym(
    tmp_kb: Path, tmp_index_path: Path
) -> None:
    (tmp_kb / "tooling" / "deploy.md").write_text(
        "# Deploy\n\nWe run workloads on kubernetes clusters.\n"
    )
    idx = Index(tmp_index_path)  # no expander
    idx.build(tmp_kb, source_commit="abc")
    assert idx.search("k8s", limit=10) == []


# --- env-based configuration -------------------------------------------------


def test_load_synonyms_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_SYNONYMS", raising=False)
    monkeypatch.delenv("KB_SYNONYMS_MODE", raising=False)
    sym = load_synonyms_from_env()
    # Defaults to the curated map.
    assert "kubernetes" in sym["k8s"]


def test_load_synonyms_from_env_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_SYNONYMS", "foo=bar,baz")
    monkeypatch.delenv("KB_SYNONYMS_MODE", raising=False)  # default merge
    sym = load_synonyms_from_env()
    # curated entries survive
    assert "kubernetes" in sym["k8s"]
    # new entry added, symmetrised
    assert "bar" in sym["foo"]
    assert "foo" in sym["bar"]


def test_load_synonyms_from_env_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_SYNONYMS", "foo=bar")
    monkeypatch.setenv("KB_SYNONYMS_MODE", "replace")
    sym = load_synonyms_from_env()
    assert "foo" in sym
    assert "k8s" not in sym  # curated defaults dropped


def test_load_synonyms_from_env_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_SYNONYMS_MODE", "off")
    sym = load_synonyms_from_env()
    assert sym == {}


# --- server wiring -----------------------------------------------------------


def test_build_app_wires_expander_by_default(
    tmp_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import data_olympus.server as server

    monkeypatch.delenv("KB_SYNONYMS", raising=False)
    monkeypatch.delenv("KB_SYNONYMS_MODE", raising=False)
    (tmp_kb / "tooling" / "deploy.md").write_text(
        "# Deploy\n\nWe run workloads on kubernetes clusters.\n"
    )
    app = server.build_app(
        kb_main_path=tmp_kb,
        kb_index_path=tmp_path / "kb.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    state = app._dolympus_state  # type: ignore[attr-defined]
    assert state.idx.query_expander is not None
    # End-to-end: the short form reaches the long-form doc through the wiring.
    hits = state.idx.search("k8s", limit=10)
    assert any("kubernetes" in (h.snippet + h.title).lower() for h in hits)
