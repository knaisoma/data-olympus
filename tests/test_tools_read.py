"""Unit tests for the MCP read tool functions (callable directly, no server)."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from data_olympus.index import Index
from data_olympus.models import HealthResponse, OutlineResponse, SearchResponse
from data_olympus.tools_read import kb_health_fn, kb_outline_fn, kb_search_fn

if TYPE_CHECKING:
    from pathlib import Path


def test_kb_health_fn(tmp_kb: Path, tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="aaa")
    resp = kb_health_fn(
        idx=idx, last_git_pull_at=time.time(), staleness_degraded_sec=600
    )
    assert isinstance(resp, HealthResponse)
    assert resp.kb_commit == "aaa"
    assert resp.total_rules == 10
    assert resp.degraded is False


def test_kb_outline_fn(tmp_kb: Path, tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="bbb")
    resp = kb_outline_fn(idx=idx)
    assert isinstance(resp, OutlineResponse)
    assert resp.source_commit == "bbb"
    tier_names = [t.name for t in resp.tiers]
    assert "T1" in tier_names


def test_kb_search_fn_returns_hits(tmp_kb: Path, tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="ccc")
    resp = kb_search_fn(idx=idx, query="worktree", limit=10)
    assert isinstance(resp, SearchResponse)
    assert resp.query == "worktree"
    assert resp.source_commit == "ccc"
    assert resp.total_returned >= 1
    ids = {h.id for h in resp.hits}
    assert "STD-U-001" in ids


def test_kb_search_fn_empty_for_unmatched(tmp_kb: Path, tmp_path: Path) -> None:
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="d")
    resp = kb_search_fn(idx=idx, query="absolutely_no_such_word_xyzzy", limit=10)
    assert resp.total_returned == 0
    assert resp.hits == []


def test_kb_search_fn_tier_filter_includes_matches(tmp_kb: Path, tmp_path: Path) -> None:
    """Filtering by tier=T1 includes the STDs in T1."""
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="e")
    resp = kb_search_fn(idx=idx, query="STD", limit=10, tier="T1")
    assert resp.total_returned >= 1
    # All returned docs should have come from a path under universal/ (T1).
    assert all("universal/" in h.path for h in resp.hits)


def test_kb_search_fn_category_filter_excludes_others(tmp_kb: Path, tmp_path: Path) -> None:
    """Filtering by category=foundation excludes docs not tagged foundation."""
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="f")
    resp = kb_search_fn(idx=idx, query="STD", limit=10, category="foundation")
    assert all(h.path.startswith("universal/foundation") for h in resp.hits)


def test_kb_health_fn_includes_write_side_placeholders(tmp_kb: Path, tmp_path: Path) -> None:
    """Slice 2A health response carries write-side placeholder fields (None/0)."""
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="g")
    resp = kb_health_fn(
        idx=idx, last_git_pull_at=time.time(), staleness_degraded_sec=600
    )
    assert resp.last_git_push_at is None
    assert resp.pending_count == 0
    assert resp.push_queue_size == 0


def test_kb_get_fn_returns_full_doc(tmp_kb: Path, tmp_path: Path) -> None:
    from data_olympus.models import GetResponse
    from data_olympus.tools_read import kb_get_fn
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="abc")
    resp = kb_get_fn(idx=idx, id="STD-U-001")
    assert isinstance(resp, GetResponse)
    assert resp.id == "STD-U-001"
    assert resp.source_commit == "abc"
    assert "worktree" in resp.content_markdown


def test_kb_get_fn_missing_id_raises(tmp_kb: Path, tmp_path: Path) -> None:
    from data_olympus.tools_read import KbNotFoundError, kb_get_fn
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="x")
    import pytest
    with pytest.raises(KbNotFoundError):
        kb_get_fn(idx=idx, id="STD-DOES-NOT-EXIST")


def test_kb_list_fn_filters_by_tier(tmp_kb: Path, tmp_path: Path) -> None:
    from data_olympus.models import ListResponse
    from data_olympus.tools_read import kb_list_fn
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="x")
    resp = kb_list_fn(idx=idx, tier="T1")
    assert isinstance(resp, ListResponse)
    assert resp.tier == "T1"
    assert resp.category is None
    assert resp.total == len(resp.entries)
    assert resp.entries, "expected at least one T1 entry"


def test_kb_list_fn_filters_by_tier_and_category(tmp_kb: Path, tmp_path: Path) -> None:
    from data_olympus.tools_read import kb_list_fn
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_kb, source_commit="x")
    resp = kb_list_fn(idx=idx, tier="T1", category="foundation")
    assert resp.category == "foundation"
    for entry in resp.entries:
        assert entry.path.startswith("universal/foundation/")


def test_kb_get_includes_git_remote_url(tmp_kb: Path, tmp_index_path: Path) -> None:
    """A doc with git_remote_url in front-matter surfaces it via kb_get."""
    from data_olympus.tools_read import kb_get_fn
    # Write a doc with the URL.
    p = tmp_kb / "projects" / "example-project" / "README.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\nid: projects-example-project-README\ntier: T3\n"
        "git_remote_url: git@github.com:example-org/example-project.git\n---\n"
        "# Example Project\n"
    )
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="test")
    resp = kb_get_fn(idx=idx, id="projects-example-project-README")
    assert resp.git_remote_url == "git@github.com:example-org/example-project.git"


def test_kb_get_git_remote_url_null_when_absent(tmp_kb: Path, tmp_index_path: Path) -> None:
    """STD-U-001 has no git_remote_url; kb_get returns null for the field."""
    from data_olympus.tools_read import kb_get_fn
    idx = Index(tmp_index_path)
    idx.build(tmp_kb, source_commit="test")
    resp = kb_get_fn(idx=idx, id="STD-U-001")
    assert resp.git_remote_url is None


def test_kb_search_fn_filters_by_status(status_kb, tmp_index_path):
    from data_olympus.index import Index
    from data_olympus.tools_read import kb_search_fn
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    resp = kb_search_fn(idx=idx, query="caching", status="active")
    assert {h.id for h in resp.hits} == {"STD-NEW"}
    assert resp.hits[0].status == "active"
    assert resp.hits[0].type == "standard"


def test_kb_get_fn_returns_status_and_type(status_kb, tmp_index_path):
    from data_olympus.index import Index
    from data_olympus.tools_read import kb_get_fn
    idx = Index(tmp_index_path)
    idx.build(status_kb, source_commit="x")
    resp = kb_get_fn(idx=idx, id="DEC-1")
    assert resp.status == "accepted"
    assert resp.type == "decision"
