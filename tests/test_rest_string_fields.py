"""Issue #310: REST routes refuse a non-string value in a string field.

The consult, gate-check and record-event routes checked that required fields
were present but not that they were strings, so a request with, say, a
numeric ``workspace`` was accepted and written to the audit log, after which
the audit and compliance readers failed with HTTP 500 while that line stayed
in the read window. Several write routes answered 500 for the same class of
input. The MCP tools were never affected, since their typed parameters
reject such values; the REST routes now accept the same shapes: a string
field must be a string, and only the fields the MCP tools type as optional
may also be null.
"""
from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastmcp import Client

from data_olympus.audit_log import AuditLog
from data_olympus.server import build_app

if TYPE_CHECKING:
    from pathlib import Path

_MEMORY = {"text": "a note", "tags": [], "source_session": "s",
           "agent_identity": "a", "confidence": 0.9}
_EDIT = {"target_path": "memory/x.md", "postimage": "# x\n\nbody\n",
         "base_commit": "HEAD", "source_session": "s", "agent_identity": "a",
         "confidence": 0.9}
_BOOTSTRAP = {"workspace": "newws", "source_session": "s", "agent_identity": "a",
              "confidence": 0.9,
              "files": [{"target_path": "projects/newws/README.md",
                         "postimage": "# newws\n\nbody\n"}]}
_GATE = {"workspace": "w", "session_id": "s"}
_CONSULT = {"workspace": "w", "source_session": "s"}
_EVENT = {"event_type": "gate_bypass", "workspace": "w"}
_CLEANUP = {"workspace": "w", "local_files": []}

# (route, body, the field the 400 must name)
_CASES: list[tuple[str, dict[str, Any], str]] = [
    ("/api/v1/consult", {**_CONSULT, "workspace": 5}, "workspace"),
    ("/api/v1/consult", {**_CONSULT, "source_session": 5}, "source_session"),
    ("/api/v1/consult", {**_CONSULT, "agent_identity": 5}, "agent_identity"),
    ("/api/v1/consult", {**_CONSULT, "intent": 5}, "intent"),
    ("/api/v1/consult", {**_CONSULT, "intent": None}, "intent"),
    ("/api/v1/consult", {**_CONSULT, "trigger": ["explicit"]}, "trigger"),
    ("/api/v1/gate/check", {**_GATE, "workspace": 5}, "workspace"),
    ("/api/v1/gate/check", {**_GATE, "session_id": ["s"]}, "session_id"),
    ("/api/v1/gate/check", {**_GATE, "tool_name": 5}, "tool_name"),
    ("/api/v1/gate/check", {**_GATE, "action_path": 5}, "action_path"),
    ("/api/v1/gate/check", {**_GATE, "action_diff": None}, "action_diff"),
    ("/api/v1/audit/event", {**_EVENT, "event_type": ["gate_bypass"]}, "event_type"),
    ("/api/v1/audit/event", {**_EVENT, "workspace": 5}, "workspace"),
    ("/api/v1/audit/event", {**_EVENT, "agent_identity": 5}, "agent_identity"),
    ("/api/v1/audit/event", {**_EVENT, "source_session": ["x"]}, "source_session"),
    ("/api/v1/audit/event", {**_EVENT, "reason": 5}, "reason"),
    ("/api/v1/propose/memory", {**_MEMORY, "text": 5}, "text"),
    ("/api/v1/propose/memory", {**_MEMORY, "agent_identity": 3}, "agent_identity"),
    ("/api/v1/propose/edit", {**_EDIT, "target_path": 5}, "target_path"),
    ("/api/v1/propose/edit", {**_EDIT, "postimage": 5}, "postimage"),
    ("/api/v1/propose/edit", {**_EDIT, "base_commit": 5}, "base_commit"),
    ("/api/v1/propose/edit", {**_EDIT, "reason": 5}, "reason"),
    ("/api/v1/propose/edit", {**_EDIT, "reason": None}, "reason"),
    ("/api/v1/onboarding/bootstrap", {**_BOOTSTRAP, "workspace": 5}, "workspace"),
    ("/api/v1/onboarding/bootstrap", {**_BOOTSTRAP, "component": 5}, "component"),
    ("/api/v1/onboarding/bootstrap", {**_BOOTSTRAP, "files": "x"}, "files"),
    ("/api/v1/onboarding/bootstrap", {**_BOOTSTRAP, "files": [1]}, "files"),
    ("/api/v1/onboarding/bootstrap", {**_BOOTSTRAP, "files": [{"x": 1}]}, "files"),
    ("/api/v1/onboarding/bootstrap",
     {**_BOOTSTRAP, "files": [{"target_path": 1, "postimage": "y"}]}, "files"),
    ("/api/v1/onboarding/cleanup-plan", {**_CLEANUP, "workspace": 5}, "workspace"),
    ("/api/v1/onboarding/cleanup-plan", {**_CLEANUP, "component": 5}, "component"),
]


def _git_kb(kb: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@e.com")
    env = {**os.environ}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=kb, check=True, env=env)
    subprocess.run(["git", "-C", str(kb), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(kb), "commit", "-m", "init"], check=True, env=env)


def _app(kb: Path, index: Path, tmp_path: Path):  # noqa: ANN202
    return build_app(
        kb_main_path=kb, kb_index_path=index,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        kb_remote_url="dummy", worktree_root=str(tmp_path / "wts"),
        pending_root=str(tmp_path / "pending"), push_queue_root=str(tmp_path / "pq"),
        audit_log_path=str(tmp_path / "audit.log"),
        ledger_path=str(tmp_path / "ledger.json"),
        write_block_tiers=[], write_block_paths=[],
    )


def _log_bytes(tmp_path: Path) -> bytes:
    p = tmp_path / "audit.log"
    return p.read_bytes() if p.exists() else b""


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "body", "field"), _CASES,
                         ids=[f"{r.rsplit('/', 1)[-1]}-{f}-{i}"
                              for i, (r, _b, f) in enumerate(_CASES)])
async def test_a_non_string_field_is_a_400_naming_it_and_writes_nothing(
    tmp_kb, tmp_index_path, tmp_path, monkeypatch, route, body, field,
) -> None:
    _git_kb(tmp_kb, monkeypatch)
    app = _app(tmp_kb, tmp_index_path, tmp_path).http_app()
    before = _log_bytes(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(route, json=body)
        pending = (await client.get("/api/v1/pending")).json()["pending"]
    assert resp.status_code == 400, resp.text
    payload = resp.json()
    assert payload["error"] == "bad_request", payload
    assert field in payload["message"], payload
    assert _log_bytes(tmp_path) == before
    assert pending == []


@pytest.mark.asyncio
async def test_the_optional_fields_still_accept_null(
    tmp_kb, tmp_index_path, tmp_path, monkeypatch,
) -> None:
    """The fields the MCP tools type as optional keep accepting an explicit
    null over REST, so existing clients that send one are unaffected."""
    _git_kb(tmp_kb, monkeypatch)
    app = _app(tmp_kb, tmp_index_path, tmp_path).http_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        gate = await client.post("/api/v1/gate/check", json={**_GATE, "action_path": None})
        cleanup = await client.post("/api/v1/onboarding/cleanup-plan",
                                    json={**_CLEANUP, "component": None})
        boot = await client.post("/api/v1/onboarding/bootstrap", json={
            **_BOOTSTRAP, "confidence": 0.1, "component": None,
            "workspace_remote_url": None, "component_remote_url": None})
        edit = await client.post("/api/v1/propose/edit", json={
            **_EDIT, "confidence": 0.1, "base_blob_sha": None,
            "target_file_hash": None})
    assert gate.status_code == 200, gate.text
    assert cleanup.status_code == 200, cleanup.text
    assert boot.status_code == 202, boot.text
    assert edit.status_code == 202, edit.text


_POISON = [
    # What the unchecked routes used to accept and write.
    {"ts": 1.0, "event_type": "consult", "status": "recorded",
     "agent_identity": 5, "source_session": "s", "target_path": "w"},
    {"ts": 2.0, "event_type": "gate_bypass", "status": "gate_bypass",
     "agent_identity": "a", "source_session": ["x"], "target_path": 5,
     "reason": 5},
]


@pytest.mark.asyncio
async def test_the_audit_readers_skip_a_malformed_line_and_count_it(
    tmp_kb, tmp_index_path, tmp_path, monkeypatch,
) -> None:
    """A malformed line already in an existing log is skipped and counted by
    kb_audit and kb_compliance, over REST and MCP, instead of failing."""
    _git_kb(tmp_kb, monkeypatch)
    log = AuditLog(log_path=str(tmp_path / "audit.log"))
    for ev in _POISON:
        log.append(ev)
    log.append({"ts": 3.0, "event_type": "consult", "status": "recorded",
                "agent_identity": "claude", "source_session": "s",
                "target_path": "w"})
    app = _app(tmp_kb, tmp_index_path, tmp_path)
    transport = httpx.ASGITransport(app=app.http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        audit = await client.get("/api/v1/audit")
        compliance = await client.get("/api/v1/compliance")
    assert audit.status_code == 200, audit.text
    assert [e["ts"] for e in audit.json()["events"]] == [3.0]
    assert audit.json()["returned"] == 1
    assert audit.json()["skipped"] == 2
    assert compliance.status_code == 200, compliance.text
    # Only the consult with a numeric agent cannot be attributed; the bypass
    # with a malformed path and session still counts.
    assert compliance.json()["counts"] == {"consult": 1, "gate_bypass": 1}
    assert compliance.json()["by_agent"] == {
        "claude": {"consult": 1}, "a": {"gate_bypass": 1}}
    assert compliance.json()["skipped"] == 1

    async with Client(app) as mcp:
        mcp_audit = await mcp.call_tool("kb_audit", {}, raise_on_error=False)
        mcp_compliance = await mcp.call_tool("kb_compliance", {}, raise_on_error=False)
    assert not mcp_audit.is_error, mcp_audit.content
    assert mcp_audit.structured_content["skipped"] == 2
    assert mcp_audit.structured_content["returned"] == 1
    assert not mcp_compliance.is_error, mcp_compliance.content
    assert mcp_compliance.structured_content["skipped"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "?since=0.5", "?agent=claude", "?status=recorded"])
async def test_a_filtered_audit_read_survives_a_line_it_cannot_evaluate(
    tmp_kb, tmp_index_path, tmp_path, monkeypatch, query,
) -> None:
    """A hand-edited line whose ``ts`` is not a number, or that is not an
    object at all, is dropped by a filtered read instead of failing it."""
    _git_kb(tmp_kb, monkeypatch)
    path = tmp_path / "audit.log"
    path.write_text(
        '{"ts": 3.0, "event_type": "consult", "status": "recorded", '
        '"agent_identity": "claude", "source_session": "s", "target_path": "w"}\n'
        '{"ts": "yesterday", "event_type": "consult", "status": "recorded", '
        '"agent_identity": "claude"}\n'
        '{"ts": null, "event_type": "consult"}\n'
        '{"ts": true, "event_type": "consult"}\n'
        '5\n'
        '["x"]\n',
        encoding="utf-8")
    app = _app(tmp_kb, tmp_index_path, tmp_path)
    transport = httpx.ASGITransport(app=app.http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        audit = await client.get(f"/api/v1/audit{query}")
        compliance = await client.get(f"/api/v1/compliance{query}")
    assert audit.status_code == 200, audit.text
    assert [e["ts"] for e in audit.json()["events"]] == [3.0]
    assert compliance.status_code == 200, compliance.text
    # Only a time window needs ``ts``: without one, compliance never reads it,
    # so the line with a malformed ts still counts. Compliance takes no status
    # filter, so ``?status=`` reads every object line, agentless ones included.
    expected = {
        "?since=0.5": {"claude": {"consult": 1}},
        "?agent=claude": {"claude": {"consult": 2}},
        "?status=recorded": {"claude": {"consult": 2}, "unknown": {"consult": 2}},
    }[query]
    assert compliance.json()["by_agent"] == expected
    assert compliance.json()["skipped"] == (2 if query == "?status=recorded" else 0)


@pytest.mark.asyncio
async def test_compliance_counts_a_falsy_non_string_agent_as_skipped(
    tmp_kb, tmp_index_path, tmp_path, monkeypatch,
) -> None:
    """A malformed agent such as ``0`` or ``[]`` is skipped and counted, not
    attributed to ``unknown``; only a missing or empty agent is."""
    _git_kb(tmp_kb, monkeypatch)
    log = AuditLog(log_path=str(tmp_path / "audit.log"))
    for agent in (0, [], False):
        log.append({"ts": 1.0, "event_type": "consult", "agent_identity": agent})
    log.append({"ts": 2.0, "event_type": "consult", "agent_identity": ""})
    app = _app(tmp_kb, tmp_index_path, tmp_path)
    transport = httpx.ASGITransport(app=app.http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        compliance = await client.get("/api/v1/compliance")
    assert compliance.status_code == 200, compliance.text
    assert compliance.json()["by_agent"] == {"unknown": {"consult": 1}}
    assert compliance.json()["skipped"] == 3
