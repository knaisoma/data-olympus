"""Tests for AuditLog: JSONL append + reverse-iteration filter."""
from __future__ import annotations

import json

from data_olympus.audit_log import AuditLog


def test_append_writes_jsonl_line(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    al.append({"ts": 100.0, "event_type": "propose_memory", "status": "committed"})
    lines = (tmp_path / "events.log").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "propose_memory"


def test_iter_filtered_by_status(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    al.append({"ts": 100.0, "event_type": "propose_memory", "status": "committed"})
    al.append({"ts": 200.0, "event_type": "propose_memory", "status": "pending_confirmation"})
    results = list(al.iter_filtered(status="committed"))
    assert len(results) == 1
    assert results[0]["status"] == "committed"


def test_iter_filtered_by_agent(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    al.append({
        "ts": 100.0, "event_type": "propose_memory",
        "agent_identity": "claude", "status": "committed",
    })
    al.append({
        "ts": 200.0, "event_type": "propose_memory",
        "agent_identity": "codex", "status": "committed",
    })
    results = list(al.iter_filtered(agent="claude"))
    assert len(results) == 1
    assert results[0]["agent_identity"] == "claude"


def test_iter_filtered_by_since(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    al.append({"ts": 100.0, "event_type": "propose_memory", "status": "committed"})
    al.append({"ts": 200.0, "event_type": "propose_memory", "status": "committed"})
    results = list(al.iter_filtered(since=150.0))
    assert len(results) == 1
    assert results[0]["ts"] == 200.0


def test_iter_returns_most_recent_first(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    for i in range(5):
        al.append({"ts": float(i * 100), "event_type": "propose_memory", "status": "committed"})
    results = list(al.iter_filtered())
    assert [r["ts"] for r in results] == [400.0, 300.0, 200.0, 100.0, 0.0]


def test_iter_missing_log_returns_empty(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "never-existed.log"))
    assert list(al.iter_filtered()) == []
