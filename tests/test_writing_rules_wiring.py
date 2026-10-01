"""KB_WRITING_RULES_* reach the running server (#283).

#291 showed that a setting can load, pass every unit test, and still never reach
the component that uses it. These tests set the environment, load the config,
build the app through the production entry point and exercise both the REST
route and the MCP tool, so a dropped setting fails here.
"""
from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import httpx
import pytest
from fastmcp import Client

import data_olympus.server as server
from data_olympus.audit_log import AuditLog
from data_olympus.config import load_config
from data_olympus.index import Index
from data_olympus.maintenance import maybe_update_ledger
from tests.test_maintenance_ledger_commit import LEDGER_PATH, _harness, _run

if TYPE_CHECKING:
    from pathlib import Path

DASH = "—"


@pytest.fixture(autouse=True)
def _git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}.items():
        monkeypatch.setenv(k, v)


def _app(tmp_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str):
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=tmp_kb, check=True)
    subprocess.run(["git", "-C", str(tmp_kb), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_kb), "commit", "-qm", "init"], check=True)
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_REMOTE_URL", "dummy")
    monkeypatch.setenv("KB_WORKTREE_ROOT", str(tmp_path / "wts"))
    monkeypatch.setenv("KB_PENDING_ROOT", str(tmp_path / "pending"))
    monkeypatch.setenv("KB_PUSH_QUEUE_ROOT", str(tmp_path / "pq"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return server.build_app_from_config(load_config(), bootstrap_now=True)


async def _propose_memory(app, text: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/v1/propose/memory", json={
            "text": text, "tags": [], "source_session": "s",
            "agent_identity": "claude", "confidence": 0.9})


@pytest.mark.asyncio
async def test_enforce_from_the_environment_rejects_over_rest(
    tmp_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(tmp_kb, tmp_path, monkeypatch, KB_WRITING_RULES_MODE="enforce")
    resp = await _propose_memory(app, f"a memory {DASH} with a dash")
    assert resp.status_code == 422, resp.text
    assert resp.json()["status"] == "rejected_writing_rule"
    assert resp.json()["writing_rule_findings"]


@pytest.mark.asyncio
async def test_the_default_warn_mode_commits_and_reports_over_rest(
    tmp_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KB_WRITING_RULES_MODE", raising=False)
    app = _app(tmp_kb, tmp_path, monkeypatch)
    resp = await _propose_memory(app, f"a memory {DASH} with a dash")
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "committed"
    assert "em-dash" in resp.json()["writing_rule_findings"][0]


@pytest.mark.asyncio
async def test_exclusions_from_the_environment_reach_the_gate(
    tmp_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(tmp_kb, tmp_path, monkeypatch, KB_WRITING_RULES_MODE="enforce",
               KB_WRITING_RULES_EXCLUDE_PATHS="memory/*")
    resp = await _propose_memory(app, f"a memory {DASH} with a dash")
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_enforce_from_the_environment_rejects_over_mcp(
    tmp_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(tmp_kb, tmp_path, monkeypatch, KB_WRITING_RULES_MODE="enforce")
    async with Client(app) as client:
        result = await client.call_tool("kb_propose_memory", {
            "text": f"a memory {DASH} with a dash", "tags": [],
            "source_session": "s", "agent_identity": "claude", "confidence": 0.9})
    assert result.data["status"] == "rejected_writing_rule"


def test_the_ledger_commit_records_the_machine_rendered_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    remote, main, git, worktrees, push_queue, pending, serializer = _harness(tmp_path)
    (main / "workflows").mkdir(parents=True)
    (main / "workflows" / "WF-001.md").write_text("# Ship\n\nno front matter.\n")
    _run("git", "add", "-A", cwd=str(main))
    _run("git", "commit", "-m", "dirty", cwd=str(main))
    _run("git", "push", "origin", "main", cwd=str(main))
    idx = Index(tmp_path / "idx.db", maintenance_ledger_path=LEDGER_PATH)
    idx.build(main, source_commit="c1", today="2026-07-08")
    log = tmp_path / "audit.log"
    sha = maybe_update_ledger(
        idx=idx, worktrees=worktrees, push_queue=push_queue, pending=pending,
        serializer=serializer, ledger_path=LEDGER_PATH,
        audit_log=AuditLog(log_path=str(log)))
    assert sha is not None
    assert '"writing_rules": "skipped:machine_rendered"' in log.read_text()
    assert os.path.exists(log)
