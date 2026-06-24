"""Tests for PushQueue: durable enqueue, retry with backoff, INIT RECOVERY."""
from __future__ import annotations

import json

from data_olympus.push_queue import PushQueue


def test_enqueue_creates_durable_entry(tmp_path) -> None:
    q = PushQueue(queue_root=str(tmp_path / "q"))
    q.enqueue(sha="abc123", worktree_path="/tmp/wt-abc", meta={"agent": "claude"})
    entry_path = tmp_path / "q" / "abc123.json"
    assert entry_path.exists()
    body = json.loads(entry_path.read_text())
    assert body["sha"] == "abc123"
    assert body["worktree_path"] == "/tmp/wt-abc"
    assert body["meta"] == {"agent": "claude"}
    assert "enqueued_at" in body
    assert body["attempts"] == 0


def test_drain_removes_pushed_entries(tmp_path) -> None:
    q = PushQueue(queue_root=str(tmp_path / "q"))
    q.enqueue(sha="abc123", worktree_path="/tmp/wt-abc", meta={})

    pushed = []
    def push_fn(worktree_path: str) -> None:
        pushed.append(worktree_path)

    q.drain(push_fn=push_fn, max_attempts=3)
    assert pushed == ["/tmp/wt-abc"]
    assert not (tmp_path / "q" / "abc123.json").exists()


def test_drain_records_failure_and_increments_attempts(tmp_path) -> None:
    q = PushQueue(queue_root=str(tmp_path / "q"))
    q.enqueue(sha="abc123", worktree_path="/tmp/wt-abc", meta={})

    def failing_push(_worktree_path: str) -> None:
        raise RuntimeError("transient network")

    q.drain(push_fn=failing_push, max_attempts=3)
    entry = json.loads((tmp_path / "q" / "abc123.json").read_text())
    assert entry["attempts"] == 1
    assert "transient network" in entry["last_error"]


def test_size_reports_current_entry_count(tmp_path) -> None:
    q = PushQueue(queue_root=str(tmp_path / "q"))
    assert q.size() == 0
    q.enqueue(sha="a", worktree_path="/x", meta={})
    q.enqueue(sha="b", worktree_path="/y", meta={})
    assert q.size() == 2


def test_init_recovery_re_enqueues_orphan_commits_for_existing_worktrees(tmp_path) -> None:
    """If a worktree has commits unreachable from origin/main but no queue
    entry, INIT RECOVERY re-enqueues them."""
    q_root = tmp_path / "q"
    wt_root = tmp_path / "wts"
    wt_root.mkdir()
    (wt_root / "session-abc").mkdir()
    q = PushQueue(queue_root=str(q_root))

    def fake_unpushed_shas(_wt_path: str) -> list[str]:
        return ["orphan-sha-1"]

    q.init_recovery(worktree_root=str(wt_root), list_unpushed_shas=fake_unpushed_shas)
    entry = json.loads((q_root / "orphan-sha-1.json").read_text())
    assert entry["sha"] == "orphan-sha-1"
    assert entry["worktree_path"].endswith("session-abc")
    assert entry.get("recovered") is True
