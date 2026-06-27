"""Config-driven taxonomy: generic public defaults + deploy-time overrides.

The shipped default path-classification table is generic and carries no
deployment-specific tier names. A deployment supplies its own taxonomy via
KB_TAXONOMY_PATH (a JSON file), its own writable prefixes via
KB_INDEXED_PREFIXES, and its own memory inbox location via
KB_MEMORY_INBOX_PREFIX.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from data_olympus.auth import is_writable_path
from data_olympus.index import _classify_by_path

if TYPE_CHECKING:
    from pathlib import Path


# ---- default taxonomy is generic ----------------------------------------

def test_default_taxonomy_has_no_deployment_specific_tier() -> None:
    # No 'operator/' tier ships in the public default.
    assert _classify_by_path("operator/memory/inbox/x.md") == ("meta", "meta")


def test_default_taxonomy_has_generic_memory_tier() -> None:
    assert _classify_by_path("memory/inbox/2026-01-01-x.md") == ("memory", "memory-inbox")
    assert _classify_by_path("memory/accepted/x.md") == ("memory", "memory-accepted")


def test_tech_stacks_classified_dynamically() -> None:
    # Any stack name works, not just an enumerated allow-list.
    assert _classify_by_path("tech-stacks/backend-go/x.md") == ("T2", "stack:backend-go")
    assert _classify_by_path("tech-stacks/backend-nestjs/x.md") == ("T2", "stack:backend-nestjs")


# ---- KB_TAXONOMY_PATH override ------------------------------------------

def test_taxonomy_path_malformed_shape_fails_loudly(tmp_path: Path, monkeypatch) -> None:
    # A JSON file of the wrong shape (object, or rows that are not 3-item
    # lists) must raise, not silently produce garbage one-character rules.
    bad_obj = tmp_path / "obj.json"
    bad_obj.write_text(json.dumps({"tier": "T1"}))
    monkeypatch.setenv("KB_TAXONOMY_PATH", str(bad_obj))
    import pytest
    with pytest.raises(ValueError, match="KB_TAXONOMY_PATH"):
        _classify_by_path("universal/foundation/x.md")

    bad_rows = tmp_path / "rows.json"
    bad_rows.write_text(json.dumps([["universal/", "T1"]]))  # only 2 items
    monkeypatch.setenv("KB_TAXONOMY_PATH", str(bad_rows))
    with pytest.raises(ValueError, match="KB_TAXONOMY_PATH"):
        _classify_by_path("universal/foundation/x.md")


def test_taxonomy_path_override_replaces_default(tmp_path: Path, monkeypatch) -> None:
    taxonomy = tmp_path / "taxonomy.json"
    taxonomy.write_text(json.dumps([
        ["operator/memory/inbox/", "operator", "memory-inbox"],
        ["operator/agent-overrides/", "operator", "agent-overrides"],
    ]))
    monkeypatch.setenv("KB_TAXONOMY_PATH", str(taxonomy))
    assert _classify_by_path("operator/memory/inbox/x.md") == ("operator", "memory-inbox")
    assert _classify_by_path("operator/agent-overrides/c.md") == ("operator", "agent-overrides")


# ---- KB_INDEXED_PREFIXES override ---------------------------------------

def test_default_indexed_prefixes_exclude_operator() -> None:
    assert is_writable_path("operator/x.md") is False
    assert is_writable_path("memory/inbox/x.md") is True


def test_indexed_prefixes_override(monkeypatch) -> None:
    monkeypatch.setenv("KB_INDEXED_PREFIXES", "universal/,operator/")
    assert is_writable_path("operator/x.md") is True
    # A prefix not in the override is no longer writable.
    assert is_writable_path("tooling/x.md") is False


# ---- KB_MEMORY_INBOX_PREFIX override ------------------------------------

def test_memory_inbox_prefix_default() -> None:
    from data_olympus.tools_write import _memory_inbox_prefix
    assert _memory_inbox_prefix() == "memory/inbox/"


def test_memory_inbox_prefix_override(monkeypatch) -> None:
    from data_olympus.tools_write import _memory_inbox_prefix
    monkeypatch.setenv("KB_MEMORY_INBOX_PREFIX", "operator/memory/inbox/")
    assert _memory_inbox_prefix() == "operator/memory/inbox/"
