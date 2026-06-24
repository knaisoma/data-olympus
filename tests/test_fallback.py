"""Tests for bin/_kb_fallback.py (the CLI's local-grep fallback)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


# Load the script as a module by file path (since it's outside the package)
def _load_fallback():
    repo_root = Path(__file__).parent.parent  # tests/ -> repo root
    fallback_path = repo_root / "bin" / "_kb_fallback.py"
    spec = importlib.util.spec_from_file_location("_kb_fallback", fallback_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fallback_health(tmp_kb: Path) -> None:
    mod = _load_fallback()
    out = mod.cmd_health(kb_local_path=tmp_kb, endpoint="http://x")
    parsed = json.loads(out)
    assert parsed["degraded"] is True
    assert parsed["kb_commit"] == "fallback"
    assert "MCP unreachable" in parsed["warning"]
    assert parsed["total_rules"] >= 1  # tmp_kb has at least one .md


def test_fallback_search(tmp_kb: Path) -> None:
    mod = _load_fallback()
    out = mod.cmd_search(query="worktree", limit=5, kb_local_path=tmp_kb, endpoint="http://x")
    parsed = json.loads(out)
    assert parsed["degraded"] is True
    assert parsed["source_commit"] == "fallback"
    # At least one hit
    assert parsed["total_returned"] >= 1
    assert parsed["hits"][0]["score"] == 0.0  # fallback has no ranking


def test_fallback_get(tmp_kb: Path) -> None:
    mod = _load_fallback()
    out = mod.cmd_get(id="STD-U-001", kb_local_path=tmp_kb, endpoint="http://x")
    parsed = json.loads(out)
    assert parsed["degraded"] is True
    assert parsed["id"] == "STD-U-001"
    assert parsed["source_commit"] == "fallback"
    assert "worktree" in parsed["content_markdown"]


def test_fallback_get_missing_returns_error(tmp_kb: Path) -> None:
    mod = _load_fallback()
    out = mod.cmd_get(id="STD-DOES-NOT-EXIST", kb_local_path=tmp_kb, endpoint="http://x")
    parsed = json.loads(out)
    assert parsed["degraded"] is True
    assert parsed.get("error") == "not_found"


def test_fallback_list(tmp_kb: Path) -> None:
    mod = _load_fallback()
    out = mod.cmd_list(tier="T1", category="foundation", kb_local_path=tmp_kb, endpoint="http://x")
    parsed = json.loads(out)
    assert parsed["degraded"] is True
    assert parsed["tier"] == "T1"
    assert parsed["entries"]


def test_fallback_outline(tmp_kb: Path) -> None:
    mod = _load_fallback()
    out = mod.cmd_outline(kb_local_path=tmp_kb, endpoint="http://x")
    parsed = json.loads(out)
    assert parsed["degraded"] is True
    assert "tiers" in parsed
