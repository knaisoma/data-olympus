"""KB_GATE_CLEARANCE must reach the running gate (issue #296, the #291 path).

A setting that load_config reads but the production construction path drops is
silently restored to its default while every unit test of the gate passes.
These tests go through the environment, load_config, build_app_from_config and
the real HTTP routes, so they fail if any link in that chain is missing.
"""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import httpx
import pytest

from data_olympus.config import load_config
from data_olympus.server import build_app_from_config

if TYPE_CHECKING:
    from pathlib import Path


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clearance: str | None):
    kb = tmp_path / "kb"
    (kb / "decisions").mkdir(parents=True)
    (kb / "decisions" / "D-1.md").write_text(
        "---\nid: D-1\ntype: decision\nstatus: active\ntier: meta\n"
        "title: Seed\n---\n# Seed\n\nSeed document.\n",
        encoding="utf-8",
    )
    for args in (
        ["init", "-q", "--initial-branch=main"],
        ["-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(["git", *args], cwd=kb, check=True)
    monkeypatch.setenv("KB_MAIN_PATH", str(kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_REMOTE_URL", "")
    monkeypatch.setenv("KB_DISABLE_VERSION_CHECK", "1")
    monkeypatch.setenv("KB_LEDGER_PATH", str(tmp_path / "ledger.json"))
    monkeypatch.setenv("KB_AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    if clearance is None:
        monkeypatch.delenv("KB_GATE_CLEARANCE", raising=False)
    else:
        monkeypatch.setenv("KB_GATE_CLEARANCE", clearance)
    return build_app_from_config(load_config(), bootstrap_now=True)


async def _unrelated_consult_then_install(app) -> str:  # noqa: ANN001
    transport = httpx.ASGITransport(app=app.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        c = await client.post("/api/v1/consult", json={
            "workspace": "proj", "intent": "change the schema",
            "source_session": "s1", "agent_identity": "claude"})
        assert c.status_code == 200
        g = await client.post("/api/v1/gate/check", json={
            "workspace": "proj", "session_id": "s1", "tool_name": "Bash",
            "action_diff": "pip install requests"})
        assert g.status_code == 200
        return str(g.json()["verdict"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clearance", "expected_setting", "expected_verdict"),
    [(None, "intent", "consult_required"), ("pair", "pair", "allow")],
)
async def test_configured_clearance_reaches_the_running_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    clearance: str | None, expected_setting: str, expected_verdict: str,
) -> None:
    app = _app(tmp_path, monkeypatch, clearance)
    state = app._dolympus_state  # type: ignore[attr-defined]
    assert state.config.gate_clearance == expected_setting
    assert await _unrelated_consult_then_install(app) == expected_verdict


@pytest.mark.parametrize(
    ("clearance", "expected_verdict"),
    [(None, "consult_required"), ("pair", "allow")],
)
def test_configured_clearance_reaches_the_mcp_gate_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    clearance: str | None, expected_verdict: str,
) -> None:
    import asyncio

    app = _app(tmp_path, monkeypatch, clearance)

    async def run() -> str:
        await app.call_tool("kb_consult", {
            "workspace": "proj", "intent": "change the schema",
            "source_session": "s1", "agent_identity": "claude"})
        result = await app.call_tool("kb_gate_check", {
            "workspace": "proj", "session_id": "s1", "tool_name": "Bash",
            "action_diff": "pip install requests"})
        payload = result.structured_content
        assert payload is not None
        return str(payload["verdict"])

    assert asyncio.run(run()) == expected_verdict
