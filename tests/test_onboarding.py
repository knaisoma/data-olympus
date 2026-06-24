"""Tests for onboarding logic: compute_status + rename detection."""
from __future__ import annotations

from unittest.mock import MagicMock

from data_olympus.onboarding import (
    compute_status,
    detect_rename_candidate,
)


def _idx_with(entries):
    """Build a mock Index that returns the given list of dicts from list_by_prefix."""
    idx = MagicMock()
    idx.list_by_prefix = lambda prefix, exclude_under=None: [
        e for e in entries
        if e["path"].startswith(prefix) and (
            exclude_under is None
            or not e["path"][len(prefix):].startswith(exclude_under)
        )
    ]
    idx.list_with_remote_url = lambda: entries
    return idx


def test_compute_status_absent_workspace_no_rename_candidate() -> None:
    idx = _idx_with([])
    s = compute_status(workspace="newproj", component=None,
                      workspace_remote_url=None, component_remote_url=None, idx=idx)
    assert s.state == "absent"
    assert s.workspace == "newproj"
    assert s.component is None
    assert s.rename_candidates == []


def test_compute_status_onboarded_workspace_with_canonical_files() -> None:
    entries = [
        {"path": "projects/example-project/README.md", "git_remote_url": "url1", "tier": "T3"},
        {"path": "projects/example-project/AGENTS.md", "git_remote_url": "url1", "tier": "T3"},
    ]
    idx = _idx_with(entries)
    s = compute_status(workspace="example-project", component=None,
                      workspace_remote_url=None, component_remote_url=None, idx=idx)
    assert s.state == "onboarded"
    assert s.missing_files == []


def test_compute_status_partial_workspace_missing_agents() -> None:
    entries = [
        {"path": "projects/example-project/README.md", "git_remote_url": "url1", "tier": "T3"},
    ]
    idx = _idx_with(entries)
    s = compute_status(workspace="example-project", component=None,
                      workspace_remote_url=None, component_remote_url=None, idx=idx)
    assert s.state == "partial"
    assert "AGENTS.md" in s.missing_files


def test_compute_status_absent_component_for_onboarded_workspace() -> None:
    entries = [
        {"path": "projects/example-project/README.md", "git_remote_url": "url1", "tier": "T3"},
        {"path": "projects/example-project/AGENTS.md", "git_remote_url": "url1", "tier": "T3"},
    ]
    idx = _idx_with(entries)
    s = compute_status(workspace="example-project", component="payment-service",
                      workspace_remote_url=None, component_remote_url=None, idx=idx)
    assert s.state == "absent"
    assert s.component == "payment-service"


def test_compute_status_onboarded_component() -> None:
    entries = [
        {"path": "projects/example-project/components/payment-service/AGENTS.md",
         "git_remote_url": "comp-url", "tier": "T4"},
    ]
    idx = _idx_with(entries)
    s = compute_status(workspace="example-project", component="payment-service",
                      workspace_remote_url=None, component_remote_url=None, idx=idx)
    assert s.state == "onboarded"


def test_detect_rename_candidate_matches_git_remote_url() -> None:
    entries = [
        {
            "path": "projects/example-project/README.md",
            "git_remote_url": "git@github.com:example-org/example-project.git",
            "tier": "T3",
        },
    ]
    idx = _idx_with(entries)
    cands = detect_rename_candidate(
        "git@github.com:example-org/example-project.git", idx, target_tier="T3"
    )
    assert len(cands) == 1
    assert cands[0].target_workspace == "example-project"


def test_detect_rename_candidate_normalizes_ssh_vs_https() -> None:
    entries = [
        {
            "path": "projects/example-project/README.md",
            "git_remote_url": "git@github.com:example-org/example-project.git",
            "tier": "T3",
        },
    ]
    idx = _idx_with(entries)
    cands = detect_rename_candidate(
        "https://github.com/example-org/example-project", idx, target_tier="T3"
    )
    assert len(cands) == 1


def test_detect_rename_candidate_empty_url_returns_empty() -> None:
    idx = _idx_with([])
    assert detect_rename_candidate(None, idx, target_tier="T3") == []
