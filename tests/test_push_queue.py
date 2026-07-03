"""Tests for PushQueue: durable enqueue, retry with backoff, INIT RECOVERY."""
from __future__ import annotations

import json
import os
import subprocess

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


def test_drain_freezes_entry_at_max_attempts(tmp_path) -> None:
    """After max_attempts failures the entry is marked frozen."""
    q = PushQueue(queue_root=str(tmp_path / "q"))
    q.enqueue(sha="abc", worktree_path="/tmp/wt", meta={})

    def failing_push(_wt: str) -> None:
        raise RuntimeError("boom")

    # max_attempts=1 => the very first failure crosses the cap and freezes.
    q.drain(push_fn=failing_push, max_attempts=1)
    entry = json.loads((tmp_path / "q" / "abc.json").read_text())
    assert entry["frozen"] is True
    assert entry["attempts"] == 1


def test_drain_skips_frozen_entries(tmp_path) -> None:
    """A frozen entry is NOT retried on subsequent drains (push_fn not called)."""
    q = PushQueue(queue_root=str(tmp_path / "q"))
    q.enqueue(sha="abc", worktree_path="/tmp/wt", meta={})

    def failing_push(_wt: str) -> None:
        raise RuntimeError("boom")

    q.drain(push_fn=failing_push, max_attempts=1)  # freezes it

    calls: list[str] = []

    def tracking_push(wt: str) -> None:
        calls.append(wt)

    # Second drain: the frozen entry must be skipped, so tracking_push is never
    # called and the attempt count does not climb.
    q.drain(push_fn=tracking_push, max_attempts=1)
    assert calls == []
    entry = json.loads((tmp_path / "q" / "abc.json").read_text())
    assert entry["attempts"] == 1  # unchanged; not retried
    assert entry["frozen"] is True


def test_frozen_count_reports_frozen_entries(tmp_path) -> None:
    q = PushQueue(queue_root=str(tmp_path / "q"))
    assert q.frozen_count() == 0
    q.enqueue(sha="live", worktree_path="/x", meta={})
    q.enqueue(sha="dead", worktree_path="/y", meta={})
    assert q.frozen_count() == 0

    def failing_push(wt: str) -> None:
        if wt == "/y":
            raise RuntimeError("boom")

    # Only "/y" fails; with max_attempts=1 it freezes, "/x" is pushed + removed.
    q.drain(push_fn=failing_push, max_attempts=1)
    assert q.frozen_count() == 1
    assert q.size() == 1  # only the frozen one remains


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


def _env() -> dict[str, str]:
    return {**os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def test_init_recovery_recovers_crash_injected_orphan_via_real_git(tmp_path) -> None:
    """Crash-injection: a session worktree has a real commit that was NEVER
    enqueued (simulating a crash between `git commit` and push_queue.enqueue).
    Startup recovery, wired exactly as main() wires it (real
    GitOps.list_unpushed_shas), must re-enqueue that commit's sha."""
    from data_olympus.git_ops import GitOps
    from data_olympus.worktrees import WorktreeRegistry

    # Real repo with origin/main so unpushed detection works.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, env=_env())
    repo = tmp_path / "main"
    subprocess.run(["git", "clone", str(remote), str(repo)], check=True, env=_env())
    (repo / "seed.md").write_text("seed")
    subprocess.run(["git", "-C", str(repo), "add", "seed.md"], check=True, env=_env())
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, env=_env())
    subprocess.run(["git", "-C", str(repo), "push", "origin", "main"], check=True, env=_env())

    git = GitOps(repo)
    reg = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"))
    wt = reg.get_or_create(source_session="session-crash", agent_identity="claude")

    # Commit inside the session worktree but DO NOT enqueue (the crash).
    with open(os.path.join(wt.path, "orphan.md"), "w") as f:
        f.write("committed but never queued")
    subprocess.run(["git", "-C", wt.path, "add", "orphan.md"], check=True, env=_env())
    subprocess.run(["git", "-C", wt.path, "commit", "-m", "orphan commit"], check=True, env=_env())
    subprocess.run(["git", "-C", wt.path, "fetch", "origin"], check=True, env=_env())
    orphan_sha = subprocess.run(
        ["git", "-C", wt.path, "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, env=_env()).stdout.strip()

    q = PushQueue(queue_root=str(tmp_path / "q"))
    assert q.size() == 0

    # Boot-time recovery (identical wiring to server.main()).
    q.init_recovery(
        worktree_root=str(tmp_path / "wts"),
        list_unpushed_shas=git.list_unpushed_shas,
    )
    assert q.size() == 1
    entry = json.loads((tmp_path / "q" / f"{orphan_sha}.json").read_text())
    assert entry["sha"] == orphan_sha
    assert entry.get("recovered") is True


def test_init_recovery_does_not_double_enqueue_already_queued_sha(tmp_path) -> None:
    """If a sha is already queued (normal enqueue happened before the crash),
    recovery must NOT overwrite/duplicate it."""
    q_root = tmp_path / "q"
    wt_root = tmp_path / "wts"
    wt_root.mkdir()
    (wt_root / "session-abc").mkdir()
    q = PushQueue(queue_root=str(q_root))
    q.enqueue(sha="already-here", worktree_path="/orig/path", meta={"agent": "claude"})

    q.init_recovery(
        worktree_root=str(wt_root),
        list_unpushed_shas=lambda _wt: ["already-here"],
    )
    entry = json.loads((q_root / "already-here.json").read_text())
    # Untouched: original meta + path preserved, not marked recovered.
    assert entry["worktree_path"] == "/orig/path"
    assert entry["meta"] == {"agent": "claude"}
    assert "recovered" not in entry
    assert q.size() == 1
