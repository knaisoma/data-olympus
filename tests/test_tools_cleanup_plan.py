"""Tests for kb_cleanup_plan_fn (read-only dedup + thin-pointer plan)."""
from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from data_olympus.tools_onboarding import CleanupInputError, kb_cleanup_plan_fn


def _idx_with_doc(doc_id: str, path: str, content: str) -> MagicMock:
    idx = MagicMock()
    idx.list_by_prefix.return_value = [
        {"id": doc_id, "path": path, "tier": "T3", "git_remote_url": None},
    ]
    idx.get.return_value = SimpleNamespace(id=doc_id, path=path, content_markdown=content)
    return idx


def test_duplicate_file_gets_thin_pointer_text() -> None:
    body = "# Purpose\n\nThe gateway routes requests to services.\n"
    idx = _idx_with_doc("projects-foo-README", "projects/foo/README.md", body)
    resp = kb_cleanup_plan_fn(
        idx=idx, workspace="foo", component=None,
        local_files=[{"path": "README.md", "content": body}],
    )
    assert resp.summary["imported_duplicate"] == 1
    item = resp.items[0]
    assert item.classification == "imported_duplicate"
    assert item.kb_path == "projects/foo/README.md"
    assert item.thin_pointer_text is not None
    assert "kb get projects-foo-README" in item.thin_pointer_text


def test_unique_file_has_no_pointer() -> None:
    idx = _idx_with_doc(
        "projects-foo-README", "projects/foo/README.md", "# Purpose\n\naaa bbb ccc\n",
    )
    resp = kb_cleanup_plan_fn(
        idx=idx, workspace="foo", component=None,
        local_files=[{"path": "docs/unrelated.md", "content": "# Deploy\n\nzzz yyy xxx\n"}],
    )
    assert resp.items[0].classification == "unique"
    assert resp.items[0].thin_pointer_text is None
    assert resp.summary["unique"] == 1


def test_component_prefix_is_used() -> None:
    idx = _idx_with_doc(
        "projects-foo-components-svc-AGENTS",
        "projects/foo/components/svc/AGENTS.md", "# Conventions\n\nuse ruff\n",
    )
    kb_cleanup_plan_fn(
        idx=idx, workspace="foo", component="svc",
        local_files=[{"path": "AGENTS.md", "content": "x"}],
    )
    idx.list_by_prefix.assert_called_once_with("projects/foo/components/svc/")


def test_workspace_prefix_excludes_components() -> None:
    idx = _idx_with_doc(
        "projects-foo-README", "projects/foo/README.md", "# Purpose\n\naaa bbb ccc\n",
    )
    kb_cleanup_plan_fn(
        idx=idx, workspace="foo", component=None,
        local_files=[{"path": "README.md", "content": "x"}],
    )
    idx.list_by_prefix.assert_called_once_with(
        "projects/foo/", exclude_under="components/",
    )


def test_entry_missing_path_raises() -> None:
    """Validation is now centralized in kb_cleanup_plan_fn itself so BOTH the
    REST route and the MCP tool path are protected identically. entry.get("path")
    is None when the key is missing, which is not a str, so this must raise
    CleanupInputError rather than silently classifying with an empty local_path."""
    idx = _idx_with_doc(
        "projects-foo-README", "projects/foo/README.md", "# Purpose\n\naaa bbb ccc\n",
    )
    with pytest.raises(CleanupInputError):
        kb_cleanup_plan_fn(
            idx=idx, workspace="foo", component=None,
            local_files=[{"content": "# Deploy\n\nzzz yyy xxx\n"}],
        )


def test_best_match_wins_over_partial_overlap_by_rank() -> None:
    """When multiple KB docs exist, the exact-duplicate doc (higher rank) must
    win over a merely partially-overlapping doc, regardless of list order."""
    local_body = "# Purpose\n\nThe gateway routes requests to services.\n"
    partial_doc = SimpleNamespace(
        id="projects-foo-partial", path="projects/foo/partial.md",
        content_markdown="# Purpose\n\nThe gateway does something else entirely.\n",
    )
    exact_doc = SimpleNamespace(
        id="projects-foo-exact", path="projects/foo/README.md",
        content_markdown=local_body,
    )
    idx = MagicMock()
    idx.list_by_prefix.return_value = [
        {"id": "projects-foo-partial", "path": "projects/foo/partial.md",
         "tier": "T3", "git_remote_url": None},
        {"id": "projects-foo-exact", "path": "projects/foo/README.md",
         "tier": "T3", "git_remote_url": None},
    ]
    idx.get.side_effect = lambda doc_id: {
        "projects-foo-partial": partial_doc,
        "projects-foo-exact": exact_doc,
    }[doc_id]

    resp = kb_cleanup_plan_fn(
        idx=idx, workspace="foo", component=None,
        local_files=[{"path": "README.md", "content": local_body}],
    )
    item = resp.items[0]
    assert item.classification == "imported_duplicate"
    assert item.kb_id == "projects-foo-exact"


def test_local_files_not_a_list_raises() -> None:
    idx = MagicMock()
    with pytest.raises(CleanupInputError):
        kb_cleanup_plan_fn(idx=idx, workspace="foo", component=None, local_files="nope")  # type: ignore[arg-type]


def test_entry_not_a_dict_raises() -> None:
    idx = MagicMock()
    with pytest.raises(CleanupInputError):
        kb_cleanup_plan_fn(idx=idx, workspace="foo", component=None, local_files=["nope"])  # type: ignore[list-item]


def test_entry_path_not_str_raises() -> None:
    idx = MagicMock()
    with pytest.raises(CleanupInputError):
        kb_cleanup_plan_fn(
            idx=idx, workspace="foo", component=None,
            local_files=[{"path": 123, "content": "x"}],
        )


def test_entry_content_none_raises() -> None:
    """{"content": null} must be REJECTED, not silently treated as empty string.

    entry.get("content", "") returns None (not the default) when the key is
    present with an explicit JSON null, so the fn must check isinstance
    explicitly rather than relying on the .get default alone.
    """
    idx = MagicMock()
    with pytest.raises(CleanupInputError):
        kb_cleanup_plan_fn(
            idx=idx, workspace="foo", component=None,
            local_files=[{"path": "README.md", "content": None}],
        )


def test_entry_content_non_str_raises() -> None:
    idx = MagicMock()
    with pytest.raises(CleanupInputError):
        kb_cleanup_plan_fn(
            idx=idx, workspace="foo", component=None,
            local_files=[{"path": "README.md", "content": 42}],
        )


def test_max_content_bytes_exceeded_raises() -> None:
    idx = _idx_with_doc("projects-foo-README", "projects/foo/README.md", "x")
    with pytest.raises(CleanupInputError):
        kb_cleanup_plan_fn(
            idx=idx, workspace="foo", component=None,
            local_files=[{"path": "README.md", "content": "a" * 100}],
            max_content_bytes=10,
        )


def test_max_files_exceeded_raises() -> None:
    idx = _idx_with_doc("projects-foo-README", "projects/foo/README.md", "x")
    local_files = [{"path": f"f{i}.md", "content": "x"} for i in range(5)]
    with pytest.raises(CleanupInputError):
        kb_cleanup_plan_fn(
            idx=idx, workspace="foo", component=None,
            local_files=local_files, max_files=3,
        )


@pytest.mark.parametrize("bad_threshold", [math.nan, math.inf, -math.inf, -0.5, 2.0, "nan"])
def test_bad_jaccard_threshold_raises(bad_threshold: object) -> None:
    idx = _idx_with_doc("projects-foo-README", "projects/foo/README.md", "x")
    with pytest.raises(CleanupInputError):
        kb_cleanup_plan_fn(
            idx=idx, workspace="foo", component=None,
            local_files=[{"path": "README.md", "content": "x"}],
            jaccard_threshold=bad_threshold,  # type: ignore[arg-type]
        )


def test_jaccard_threshold_not_coercible_raises() -> None:
    idx = _idx_with_doc("projects-foo-README", "projects/foo/README.md", "x")
    with pytest.raises(CleanupInputError):
        kb_cleanup_plan_fn(
            idx=idx, workspace="foo", component=None,
            local_files=[{"path": "README.md", "content": "x"}],
            jaccard_threshold="not-a-number",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("boundary", [0.0, 1.0])
def test_jaccard_threshold_bounds_accepted(boundary: float) -> None:
    idx = _idx_with_doc("projects-foo-README", "projects/foo/README.md", "x")
    resp = kb_cleanup_plan_fn(
        idx=idx, workspace="foo", component=None,
        local_files=[{"path": "README.md", "content": "x"}],
        jaccard_threshold=boundary,
    )
    assert resp.workspace == "foo"
