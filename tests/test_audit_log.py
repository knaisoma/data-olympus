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


# --- tamper-evident hash chain --------------------------------------------

def test_append_adds_chain_fields(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    al.append({"ts": 1.0, "event_type": "propose_memory", "status": "committed"})
    ev = json.loads((tmp_path / "events.log").read_text().splitlines()[0])
    assert ev["event_id"]
    assert ev["prev_hash"] == ""  # genesis
    assert ev["hash"]


def test_chain_links_prev_hash(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    al.append({"ts": 1.0, "status": "a"})
    al.append({"ts": 2.0, "status": "b"})
    lines = [json.loads(x) for x in (tmp_path / "events.log").read_text().splitlines()]
    assert lines[1]["prev_hash"] == lines[0]["hash"]


def test_verify_intact_chain(tmp_path) -> None:
    al = AuditLog(log_path=str(tmp_path / "events.log"))
    for i in range(5):
        al.append({"ts": float(i), "status": "committed"})
    assert al.verify() == (True, -1)


def test_verify_detects_tampered_event(tmp_path) -> None:
    path = tmp_path / "events.log"
    al = AuditLog(log_path=str(path))
    for i in range(3):
        al.append({"ts": float(i), "status": "committed"})
    # Tamper with the body of line 1 (leave its hash) -> recomputed != stored.
    lines = path.read_text().splitlines()
    ev = json.loads(lines[1])
    ev["status"] = "FORGED"
    lines[1] = json.dumps(ev)
    path.write_text("\n".join(lines) + "\n")
    ok, idx = AuditLog(log_path=str(path)).verify()
    assert ok is False
    assert idx == 1


def test_verify_detects_deleted_event(tmp_path) -> None:
    path = tmp_path / "events.log"
    al = AuditLog(log_path=str(path))
    for i in range(3):
        al.append({"ts": float(i), "status": "committed"})
    lines = path.read_text().splitlines()
    del lines[1]  # drop the middle event -> prev_hash linkage breaks at new line 1
    path.write_text("\n".join(lines) + "\n")
    ok, idx = AuditLog(log_path=str(path)).verify()
    assert ok is False
    assert idx == 1


def test_hmac_chain_requires_key_to_verify(tmp_path) -> None:
    path = tmp_path / "events.log"
    AuditLog(log_path=str(path), hmac_key="secret").append({"ts": 1.0, "status": "x"})
    AuditLog(log_path=str(path), hmac_key="secret").append({"ts": 2.0, "status": "y"})
    assert AuditLog(log_path=str(path), hmac_key="secret").verify() == (True, -1)
    # Wrong/absent key cannot validate the HMAC chain.
    assert AuditLog(log_path=str(path), hmac_key="wrong").verify()[0] is False
    assert AuditLog(log_path=str(path)).verify()[0] is False


def test_verify_tolerates_legacy_unhashed_lines(tmp_path) -> None:
    path = tmp_path / "events.log"
    # Pre-existing legacy line with no hash field.
    path.write_text(json.dumps({"ts": 0.0, "status": "legacy"}) + "\n")
    al = AuditLog(log_path=str(path))
    al.append({"ts": 1.0, "status": "new"})
    al.append({"ts": 2.0, "status": "new2"})
    # The legacy prefix line is tolerated; the new chained lines validate.
    assert al.verify() == (True, -1)


def test_verify_rejects_unhashed_line_after_chain_started(tmp_path) -> None:
    """An unhashed (legacy-shaped) line appended AFTER the chain has started is
    treated as tampering, so an attacker cannot forge an event past the chain."""
    path = tmp_path / "events.log"
    al = AuditLog(log_path=str(path))
    al.append({"ts": 1.0, "status": "real"})
    al.append({"ts": 2.0, "status": "real2"})
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 3.0, "status": "FORGED"}) + "\n")
    ok, idx = AuditLog(log_path=str(path)).verify()
    assert ok is False
    assert idx == 2


# --- size-based rotation with chain continuity ----------------------------

def _rotated(tmp_path) -> list:
    return sorted(p for p in tmp_path.glob("events-*.log"))


def test_no_rotation_when_disabled(tmp_path) -> None:
    """max_bytes=0 (default) never rotates: single file, backward compatible."""
    path = tmp_path / "events.log"
    al = AuditLog(log_path=str(path))
    for i in range(50):
        al.append({"ts": float(i), "status": "committed", "blob": "x" * 200})
    assert _rotated(tmp_path) == []
    assert al.verify() == (True, -1)


def test_rotation_creates_segment_and_keeps_live_file(tmp_path) -> None:
    path = tmp_path / "events.log"
    al = AuditLog(log_path=str(path), max_bytes=400)
    for i in range(20):
        al.append({"ts": float(i), "status": "committed", "blob": "y" * 100})
    rotated = _rotated(tmp_path)
    assert rotated, "expected at least one rotated segment"
    assert path.exists(), "live file must still exist after rotation"


def test_chain_continuity_across_rotation(tmp_path) -> None:
    """The first event of a new segment links to the last hash of the previous
    segment, so verify() validates across the boundary."""
    path = tmp_path / "events.log"
    al = AuditLog(log_path=str(path), max_bytes=300)
    for i in range(30):
        al.append({"ts": float(i), "status": "committed", "blob": "z" * 80})
    rotated = _rotated(tmp_path)
    assert len(rotated) >= 1
    # The live file's first hashed event must chain to the last hash of the most
    # recent rotated segment.
    last_rotated_lines = [
        json.loads(x) for x in rotated[-1].read_text().splitlines() if x.strip()
    ]
    live_lines = [
        json.loads(x) for x in path.read_text().splitlines() if x.strip()
    ]
    assert live_lines[0]["prev_hash"] == last_rotated_lines[-1]["hash"]
    # Full chain across every segment validates.
    assert al.verify() == (True, -1)
    # A fresh reader (new process) sees the same intact chain.
    assert AuditLog(log_path=str(path), max_bytes=300).verify() == (True, -1)


def test_rotation_survives_process_restart(tmp_path) -> None:
    """After rotation, a fresh AuditLog resumes the chain from the true tail
    (live file's last hash), not a rotated segment's."""
    path = tmp_path / "events.log"
    al = AuditLog(log_path=str(path), max_bytes=300)
    for i in range(30):
        al.append({"ts": float(i), "status": "committed", "blob": "z" * 80})
    live_last = [
        json.loads(x) for x in path.read_text().splitlines() if x.strip()
    ][-1]["hash"]
    al2 = AuditLog(log_path=str(path), max_bytes=300)
    al2.append({"ts": 999.0, "status": "committed"})
    new_first = [
        json.loads(x) for x in path.read_text().splitlines() if x.strip()
    ][-1]
    assert new_first["prev_hash"] == live_last
    assert al2.verify() == (True, -1)


def test_verify_detects_tamper_in_rotated_segment(tmp_path) -> None:
    path = tmp_path / "events.log"
    al = AuditLog(log_path=str(path), max_bytes=300)
    for i in range(30):
        al.append({"ts": float(i), "status": "committed", "blob": "z" * 80})
    rotated = _rotated(tmp_path)
    assert rotated
    # Tamper with a line inside the oldest rotated segment.
    seg = rotated[0]
    lines = seg.read_text().splitlines()
    ev = json.loads(lines[0])
    ev["status"] = "FORGED"
    lines[0] = json.dumps(ev)
    seg.write_text("\n".join(lines) + "\n")
    ok, _idx = AuditLog(log_path=str(path), max_bytes=300).verify()
    assert ok is False


def test_iter_includes_rotated_with_since(tmp_path) -> None:
    """A since-filtered read walks rotated segments so history that rotated out of
    the live file is still visible."""
    path = tmp_path / "events.log"
    al = AuditLog(log_path=str(path), max_bytes=300)
    for i in range(40):
        al.append({"ts": float(i), "status": "committed", "blob": "q" * 80})
    assert _rotated(tmp_path), "test needs at least one rotation"
    # Without rotated segments the live file alone would miss early ts.
    all_ts = [
        ev["ts"] for ev in al.iter_filtered(since=0.0, include_rotated=True)
    ]
    assert min(all_ts) == 0.0
    assert max(all_ts) == 39.0
    assert len(all_ts) == 40
    # Most-recent-first ordering preserved across the boundary.
    assert all_ts == sorted(all_ts, reverse=True)


def test_iter_default_reads_live_file_only(tmp_path) -> None:
    path = tmp_path / "events.log"
    al = AuditLog(log_path=str(path), max_bytes=300)
    for i in range(40):
        al.append({"ts": float(i), "status": "committed", "blob": "q" * 80})
    live_only = list(al.iter_filtered())
    all_events = list(al.iter_filtered(include_rotated=True))
    assert len(live_only) < len(all_events)


def test_iter_bounded_scan(tmp_path) -> None:
    path = tmp_path / "events.log"
    al = AuditLog(log_path=str(path))
    for i in range(100):
        al.append({"ts": float(i), "status": "committed"})
    bounded = list(al.iter_filtered(max_scan_events=10))
    assert len(bounded) == 10


def test_old_single_file_still_verifies(tmp_path) -> None:
    """An existing single-file log (written before rotation existed) verifies
    unchanged when opened by a rotation-aware AuditLog."""
    path = tmp_path / "events.log"
    writer = AuditLog(log_path=str(path))  # no max_bytes: single file
    for i in range(10):
        writer.append({"ts": float(i), "status": "committed"})
    # Reader with rotation enabled sees the same intact single-file chain.
    assert AuditLog(log_path=str(path), max_bytes=1_000_000).verify() == (True, -1)
