"""Tests for the single-sourced onboarding playbook."""
from __future__ import annotations

import pytest

from data_olympus.onboarding_playbook import render_playbook


def test_dispatch_asks_the_known_project_branch_question() -> None:
    out = render_playbook("dispatch")
    assert "kb_onboarding_status" in out
    assert "known project" in out.lower()


def test_project_playbook_names_the_workspace_and_core_steps() -> None:
    out = render_playbook("project", workspace="foo")
    assert "foo" in out
    assert "kb_bootstrap_project" in out
    assert "kb_cleanup_plan" in out
    assert "interview" in out.lower()


def test_component_playbook_reads_parent_agents_first() -> None:
    out = render_playbook("component", workspace="foo", component="svc")
    assert "svc" in out and "foo" in out
    assert "AGENTS.md" in out  # inherit parent lessons


def test_unknown_kind_raises() -> None:
    with pytest.raises(ValueError):
        render_playbook("bogus")
