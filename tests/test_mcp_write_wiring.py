"""MCP-surface wiring parity with REST (0.3.0 epic #72 scope item 9):

(a) the MCP kb_resolve_pending tool applies the KB_MAX_POSTIMAGE_BYTES edited_text
    cap (REST already did; MCP did not);
(b) the MCP consult / gate-check / cleanup-plan tools apply the shared rate
    limiter (REST already did; MCP did not).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastmcp import Client

from data_olympus.server import build_app

if TYPE_CHECKING:
    from pathlib import Path


def _app_with_pipeline(tmp_git_kb: Path, tmp_path: Path, **overrides):
    """Build an app with the write pipeline enabled (kb_remote_url set) so the
    rate limiter and write tools are wired. The remote is the KB repo itself; the
    tests here never push, only exercise the reject-before-commit paths."""
    return build_app(
        kb_main_path=tmp_git_kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        kb_remote_url=str(tmp_git_kb),
        worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"),
        push_queue_root=str(tmp_path / "pushq"),
        audit_log_path=str(tmp_path / "audit.log"),
        **overrides,
    )


@pytest.mark.asyncio
async def test_mcp_resolve_edited_text_cap_enforced(
    tmp_git_kb: Path, tmp_path: Path,
) -> None:
    """item 9(a): a low-confidence memory parked as pending, then resolved via the
    MCP tool with an oversize edited_text, is rejected with the same distinct
    status the REST path returns."""
    app = _app_with_pipeline(tmp_git_kb, tmp_path, max_postimage_bytes=100)
    async with Client(app) as client:
        park = await client.call_tool("kb_propose_memory", {
            "text": "small note", "tags": [], "source_session": "s",
            "agent_identity": "claude", "confidence": 0.3,
        })
        data = park.data if hasattr(park, "data") else None
        pid = (data or {}).get("pending_id") if isinstance(data, dict) else None
        if pid is None:
            # Fallback: parse from the serialized result.
            import re
            m = re.search(r'"pending_id"\s*:\s*"([0-9a-f]{32})"', str(park))
            assert m, f"no pending_id in {park}"
            pid = m.group(1)
        res = await client.call_tool("kb_resolve_pending", {
            "pending_id": pid, "decision": "approve",
            "edited_text": "X" * 5000,
        })
        assert "rejected_edited_text_too_large" in str(res)


@pytest.mark.asyncio
async def test_mcp_consult_rate_limited(tmp_git_kb: Path, tmp_path: Path) -> None:
    """item 9(b): the MCP kb_consult tool honors the shared limiter. With the
    per-hour quota set to 0, the first consult is rejected_rate_limited."""
    app = _app_with_pipeline(tmp_git_kb, tmp_path, rate_limit_per_hour=0)
    async with Client(app) as client:
        res = await client.call_tool("kb_consult", {
            "workspace": "example-project", "intent": "add a feature",
            "source_session": "s", "agent_identity": "claude",
        })
        assert "rejected_rate_limited" in str(res)


@pytest.mark.asyncio
async def test_mcp_gate_check_not_rate_limited_by_default(
    tmp_git_kb: Path, tmp_path: Path,
) -> None:
    """kb_gate_check must NOT share the write/consult limiter: it is the hook's
    per-tool-action probe. Even with the write quota exhausted (rate_limit=0) and
    no gate ceiling configured, gate_check is unthrottled."""
    app = _app_with_pipeline(tmp_git_kb, tmp_path, rate_limit_per_hour=0)
    async with Client(app) as client:
        for _ in range(3):
            res = await client.call_tool("kb_gate_check", {
                "workspace": "example-project", "session_id": "s",
                "tool_name": "Edit",
            })
            assert "rejected_rate_limited" not in str(res)


@pytest.mark.asyncio
async def test_mcp_gate_check_rate_limited_when_ceiling_set(
    tmp_git_kb: Path, tmp_path: Path,
) -> None:
    """With an explicit KB_GATE_CHECK_RATE_LIMIT_PER_HOUR backstop, gate_check
    throttles at that ceiling (here 1/hour), independent of the write limiter."""
    app = _app_with_pipeline(
        tmp_git_kb, tmp_path, rate_limit_per_hour=1000,
        gate_check_rate_limit_per_hour=1,
    )
    async with Client(app) as client:
        first = await client.call_tool("kb_gate_check", {
            "workspace": "example-project", "session_id": "s", "tool_name": "Edit",
        })
        second = await client.call_tool("kb_gate_check", {
            "workspace": "example-project", "session_id": "s", "tool_name": "Edit",
        })
        assert "rejected_rate_limited" not in str(first)
        assert "rejected_rate_limited" in str(second)


@pytest.mark.asyncio
async def test_mcp_cleanup_plan_rate_limited(tmp_git_kb: Path, tmp_path: Path) -> None:
    """Codex Nit 1: the MCP kb_cleanup_plan tool honors the shared limiter too."""
    app = _app_with_pipeline(tmp_git_kb, tmp_path, rate_limit_per_hour=0)
    async with Client(app) as client:
        res = await client.call_tool("kb_cleanup_plan", {
            "workspace": "example-project",
            "local_files": [{"path": "README.md", "content": "hi"}],
        })
        assert "rejected_rate_limited" in str(res)


@pytest.mark.asyncio
async def test_mcp_kb_session_recap_counts_writes(
    tmp_git_kb: Path, tmp_path: Path,
) -> None:
    """issue #112: kb_session_recap is registered and reflects the audit log
    for the given source_session."""
    app = _app_with_pipeline(tmp_git_kb, tmp_path)
    async with Client(app) as client:
        await client.call_tool("kb_propose_memory", {
            "text": "recap note", "tags": [], "source_session": "recap-mcp",
            "agent_identity": "claude", "confidence": 0.9,
        })
        res = await client.call_tool("kb_session_recap", {
            "source_session": "recap-mcp",
        })
        data = res.data if hasattr(res, "data") else res
        assert isinstance(data, dict)
        assert data["source_session"] == "recap-mcp"
        assert data["committed"] == 1
