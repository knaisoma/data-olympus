"""Tests for kb_onboarding_status_fn + kb_bootstrap_project_fn."""
from __future__ import annotations

from unittest.mock import MagicMock

from data_olympus.tools_onboarding import (
    kb_bootstrap_project_fn,  # noqa: F401  used in later tasks; ensure import works
    kb_onboarding_status_fn,
)


def test_kb_onboarding_status_returns_absent_for_new_workspace() -> None:
    idx = MagicMock()
    idx.list_by_prefix.return_value = []
    idx.list_with_remote_url.return_value = []
    resp = kb_onboarding_status_fn(
        idx=idx, workspace="newproj", component=None,
        workspace_remote_url=None, component_remote_url=None,
    )
    assert resp.state == "absent"


def test_kb_onboarding_status_returns_onboarded() -> None:
    idx = MagicMock()
    idx.list_by_prefix.return_value = [
        {"path": "projects/example-project/README.md", "git_remote_url": "url1", "tier": "T3"},
        {"path": "projects/example-project/AGENTS.md", "git_remote_url": "url1", "tier": "T3"},
    ]
    idx.list_with_remote_url.return_value = []
    resp = kb_onboarding_status_fn(
        idx=idx, workspace="example-project", component=None,
        workspace_remote_url=None, component_remote_url=None,
    )
    assert resp.state == "onboarded"
