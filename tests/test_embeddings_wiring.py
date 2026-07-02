"""build_app reranker-stack composition for embeddings (issue #42).

Verifies:

- Default OFF: no embedding library is imported and an exact-id / exact-tag /
  status ordering is exactly today's behaviour (the hybrid layer is absent).
- Enabled: the hybrid layer composes UNDER the id/tag short-circuit, so an exact
  id still wins, and the status prior still applies. Enabled cases SKIP when the
  model/dep is unavailable.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from data_olympus.server import build_app

from .test_embeddings import _needs_model

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _kb(kb: Path) -> None:
    (kb / "std.md").write_text(
        "---\nid: STD-U-001\nstatus: active\n---\nCaching policy for services.\n",
        encoding="utf-8",
    )
    (kb / "old.md").write_text(
        "---\nid: STD-U-002\nstatus: superseded\n---\nOld caching guidance.\n",
        encoding="utf-8",
    )


def _build(kb: Path, index_path: Path, **kw: object) -> object:
    return build_app(
        kb_main_path=kb,
        kb_index_path=index_path,
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
        **kw,  # type: ignore[arg-type]
    )


def test_default_off_does_not_import_fastembed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    # Drop any prior import so we can prove the default path never triggers one.
    monkeypatch.delitem(sys.modules, "fastembed", raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
    _kb(kb)
    _build(kb, tmp_path / "kb.db", embeddings_enabled=False)
    assert "fastembed" not in sys.modules


def test_default_off_exact_id_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
    _kb(kb)
    app = _build(kb, tmp_path / "kb.db", embeddings_enabled=False)
    idx = app._dolympus_state.idx  # type: ignore[attr-defined]
    hits = idx.search("STD-U-001", limit=5)
    assert hits[0].id == "STD-U-001"


def test_default_off_active_outranks_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KB_EMBEDDINGS_MODE", raising=False)
    kb = tmp_path / "kb"
    kb.mkdir()
    _kb(kb)
    app = _build(kb, tmp_path / "kb.db", embeddings_enabled=False)
    idx = app._dolympus_state.idx  # type: ignore[attr-defined]
    hits = idx.search("caching", limit=5)
    order = [h.id for h in hits]
    # Both match "caching"; the active doc must outrank the superseded one.
    assert order.index("STD-U-001") < order.index("STD-U-002")


@_needs_model
def test_enabled_exact_id_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KB_EMBEDDINGS_MODE", "on")
    kb = tmp_path / "kb"
    kb.mkdir()
    _kb(kb)
    app = _build(
        kb, tmp_path / "kb.db", embeddings_enabled=True, embeddings_weight=0.5
    )
    idx = app._dolympus_state.idx  # type: ignore[attr-defined]
    # Exact id must still short-circuit to the top even with the hybrid blend
    # active (id/tag reranker wraps the hybrid layer as inner).
    hits = idx.search("STD-U-001", limit=5)
    assert hits[0].id == "STD-U-001"


@_needs_model
def test_enabled_active_still_outranks_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KB_EMBEDDINGS_MODE", "on")
    kb = tmp_path / "kb"
    kb.mkdir()
    _kb(kb)
    app = _build(
        kb, tmp_path / "kb.db", embeddings_enabled=True, embeddings_weight=0.3
    )
    idx = app._dolympus_state.idx  # type: ignore[attr-defined]
    hits = idx.search("caching", limit=5)
    order = [h.id for h in hits]
    assert order.index("STD-U-001") < order.index("STD-U-002")


@_needs_model
def test_config_model_honored_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer concern 2: the programmatic ``embeddings_model`` passed via
    build_app/Config is the single source of truth. Even when the environment
    names a DIFFERENT model, the Index (and its embedder) must use the Config
    value, not re-read ``KB_EMBEDDINGS_MODEL`` from env."""
    # Env names a bogus model that would fail to load if it were consulted.
    monkeypatch.setenv("KB_EMBEDDINGS_MODE", "on")
    monkeypatch.setenv("KB_EMBEDDINGS_MODEL", "env/nonexistent-model-should-be-ignored")
    kb = tmp_path / "kb"
    kb.mkdir()
    _kb(kb)
    # If the enabled path re-read env, build_embedder would raise on the bogus
    # env model; instead it must honour the real Config model and succeed.
    app = _build(
        kb,
        tmp_path / "kb.db",
        embeddings_enabled=True,
        embeddings_model="BAAI/bge-small-en-v1.5",
        embeddings_weight=0.3,
    )
    idx = app._dolympus_state.idx  # type: ignore[attr-defined]
    embedder = idx._resolve_embedder()  # type: ignore[attr-defined]
    assert embedder is not None
    assert embedder.model_name == "BAAI/bge-small-en-v1.5"
