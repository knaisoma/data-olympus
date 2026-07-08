"""Integration tests for the 0.3.0 write-pipeline core (epic #72).

These exercise the whole write path against REAL git repos and a shared bare
remote, per the epic's acceptance criteria:

- Two-session interleaved write: both writes publish (one via non-FF rebase
  recovery), none lost, push queue drains to empty.
- Rebase-conflict demotion: a commit that cannot rebase cleanly is demoted to a
  pending entry instead of retrying forever, with a distinct audit event.
- Threaded concurrent auto-commit on one session/one path: no interleaved
  commits, the per-path lock is respected across the write path.
"""
from __future__ import annotations

import os
import subprocess
import threading

from data_olympus.audit_log import AuditLog
from data_olympus.auth import PathBlocklist
from data_olympus.git_ops import GitOps
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.refresh import demote_conflict_to_pending
from data_olympus.tools_write import kb_propose_edit_fn
from data_olympus.worktrees import WorktreeRegistry
from data_olympus.write_gate import WriteSerializer


def _env() -> dict[str, str]:
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def _run(*args: str, cwd: str) -> None:
    subprocess.run(list(args), cwd=cwd, check=True, env=_env(),
                   capture_output=True)


def _bare_remote_with_clone(tmp_path):
    """Create a bare remote + a clone that will act as the server's main repo.
    Returns (remote_path, main_repo_path). The main repo has one seed commit on
    origin/main with a T1 file the tests edit."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, env=_env())
    main = tmp_path / "main"
    subprocess.run(["git", "clone", str(remote), str(main)], check=True,
                   env=_env(), capture_output=True)
    seed = main / "universal" / "foundation" / "STD-U-001.md"
    seed.parent.mkdir(parents=True)
    seed.write_text("---\nid: STD-U-001\ntype: standard\nstatus: active\ntier: T1\n"
                    "---\nbase body\n")
    _run("git", "add", "-A", cwd=str(main))
    _run("git", "commit", "-m", "seed", cwd=str(main))
    _run("git", "push", "origin", "main", cwd=str(main))
    return remote, main


def _server_pieces(tmp_path, main):
    git = GitOps(main)
    reg = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"))
    pq = PushQueue(queue_root=str(tmp_path / "push-q"))
    pen = PendingQueue(pending_root=str(tmp_path / "pending"))
    rl = SlidingWindowLimiter(max_per_hour=1000)
    bl = PathBlocklist(tier_blocks=[], path_blocks=[])
    return git, reg, pq, pen, rl, bl


def test_two_session_interleaved_writes_both_publish(tmp_path, monkeypatch) -> None:
    """Two overlapping sessions each auto-commit an edit to DIFFERENT files. A
    second real clone pushes to origin/main between them so the second session's
    push is non-fast-forward. Both must end up on origin/main (one via rebase
    recovery); neither is lost; the push queue drains to empty."""
    for k, v in _env().items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)
    # This test is about rebase-recovery publishing, not governance; the
    # postimages' status: accepted would otherwise trip the issue #112
    # governed-lane status clamp and demote instead of commit.
    monkeypatch.setenv("KB_GOVERNED_LANE_PROTECTION", "off")
    remote, main = _bare_remote_with_clone(tmp_path)
    git, reg, pq, pen, rl, bl = _server_pieces(tmp_path, main)
    serializer = WriteSerializer()

    # Session A commits an edit to file-a.md.
    ra = kb_propose_edit_fn(
        target_path="decisions/DEC-a.md",
        postimage="---\nid: DEC-a\ntype: decision\nstatus: accepted\ntier: meta\n"
                  "---\nA content\n",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        reason="a", source_session="session-A", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.1.1.1",
        serializer=serializer,
    )
    assert ra.status == "committed"

    # A SECOND real clone pushes an unrelated commit to origin/main, moving it
    # forward so session A's (and B's) push will be non-fast-forward.
    other = tmp_path / "other-clone"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True,
                   env=_env(), capture_output=True)
    (other / "outside.md").write_text("from another writer\n")
    _run("git", "add", "-A", cwd=str(other))
    _run("git", "commit", "-m", "outside commit", cwd=str(other))
    _run("git", "push", "origin", "main", cwd=str(other))

    # Session B commits an edit to a different file, on a base that is now stale.
    rb = kb_propose_edit_fn(
        target_path="decisions/DEC-b.md",
        postimage="---\nid: DEC-b\ntype: decision\nstatus: accepted\ntier: meta\n"
                  "---\nB content\n",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        reason="b", source_session="session-B", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="2.2.2.2",
        serializer=serializer,
    )
    assert rb.status == "committed"
    assert pq.size() == 2

    # Drain the push queue: A pushes (non-FF now because origin moved), triggering
    # rebase recovery; B likewise. Both must publish. Retry a few times to let
    # the non-FF -> rebase -> retry sequence settle deterministically.
    for _ in range(5):
        pq.drain(
            push_fn=lambda wt: git.push_with_rebase_recovery(wt, timeout_sec=30),
            max_attempts=10,
        )
        if pq.size() == 0:
            break

    assert pq.size() == 0, "both writes must publish; queue must be empty"

    # origin/main now carries the outside commit AND both session files.
    _run("git", "fetch", "origin", "main", cwd=str(main))
    files = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "origin/main"],
        cwd=str(main), text=True, env=_env(),
    ).split()
    assert "decisions/DEC-a.md" in files
    assert "decisions/DEC-b.md" in files
    assert "outside.md" in files


def test_rebase_conflict_demotes_to_pending(tmp_path, monkeypatch) -> None:
    """A commit that conflicts on rebase (same file, incompatible content moved
    origin/main) is demoted to a pending entry with a distinct audit event, not
    retried forever."""
    for k, v in _env().items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)
    # This test is about rebase-conflict demotion, not governance; the
    # postimage's status: active would otherwise trip the issue #112
    # governed-lane status clamp and demote for a DIFFERENT reason than the
    # one under test.
    monkeypatch.setenv("KB_GOVERNED_LANE_PROTECTION", "off")
    remote, main = _bare_remote_with_clone(tmp_path)
    git, reg, pq, pen, rl, bl = _server_pieces(tmp_path, main)
    audit = AuditLog(log_path=str(tmp_path / "audit.log"), hmac_key="")
    serializer = WriteSerializer()

    # Session A edits the shared STD-U-001 file.
    ra = kb_propose_edit_fn(
        target_path="universal/foundation/STD-U-001.md",
        postimage="---\nid: STD-U-001\ntype: standard\nstatus: active\ntier: T1\n"
                  "---\nSESSION A rewrite\n",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        reason="a", source_session="session-A", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.1.1.1",
        serializer=serializer, audit_log=audit,
    )
    assert ra.status == "committed"

    # Another writer pushes an INCOMPATIBLE change to the SAME file -> A's commit
    # cannot rebase cleanly.
    other = tmp_path / "other-clone"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True,
                   env=_env(), capture_output=True)
    (other / "universal" / "foundation" / "STD-U-001.md").write_text(
        "---\nid: STD-U-001\ntype: standard\nstatus: active\ntier: T1\n"
        "---\nOTHER WRITER rewrite\n")
    _run("git", "add", "-A", cwd=str(other))
    _run("git", "commit", "-m", "conflicting", cwd=str(other))
    _run("git", "push", "origin", "main", cwd=str(other))

    def on_conflict(entry):
        demote_conflict_to_pending(entry, git=git, pending=pen, audit_log=audit)

    pq.drain(
        push_fn=lambda wt: git.push_with_rebase_recovery(wt, timeout_sec=30),
        max_attempts=10,
        on_rebase_conflict=on_conflict,
    )

    # The queue entry was removed (demoted), a pending entry now exists.
    assert pq.size() == 0
    assert pen.size() == 1
    pending_list = pen.list()
    assert pending_list[0]["target_path"] == "universal/foundation/STD-U-001.md"

    # A push_conflict_demoted audit event was recorded.
    events = list(audit.iter_filtered())
    kinds = {e.get("event_type") for e in events}
    assert "push_conflict_demoted" in kinds


def test_threaded_concurrent_writes_one_path_no_interleave(
    tmp_path, monkeypatch,
) -> None:
    """Two threads auto-commit edits to the SAME path in one session. The
    per-path advisory lock (shared with the pending queue) plus the process-wide
    write serializer must prevent interleaved commits: exactly one wins the lock
    per instant, the other either commits after or is rejected path_lock_busy;
    never a corrupt/mixed commit. The repo history must stay linear and clean."""
    for k, v in _env().items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)
    # This test is about the path-lock/serializer race, not governance; the
    # postimages' status: active would otherwise trip the issue #112
    # governed-lane status clamp and demote instead of commit.
    monkeypatch.setenv("KB_GOVERNED_LANE_PROTECTION", "off")
    _remote, main = _bare_remote_with_clone(tmp_path)
    git, reg, pq, pen, rl, bl = _server_pieces(tmp_path, main)
    serializer = WriteSerializer()

    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def writer(n: int) -> None:
        barrier.wait()
        resp = kb_propose_edit_fn(
            target_path="universal/foundation/STD-U-001.md",
            postimage=f"---\nid: STD-U-001\ntype: standard\nstatus: active\n"
                      f"tier: T1\n---\nthread {n} body\n",
            base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
            reason=f"t{n}", source_session="session-shared",
            agent_identity="claude", confidence=0.95, confidence_threshold=0.85,
            worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl,
            blocklist=bl, remote_addr="1.1.1.1", serializer=serializer,
        )
        with lock:
            results.append(resp.status)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Both calls returned a well-defined status (committed or path_lock_busy),
    # never a crash or a partial state.
    assert set(results) <= {"committed", "rejected_path_lock_busy"}
    assert len(results) == 2
    # The session worktree history must be clean and linear (no dangling merge /
    # rebase state, no mixed staged leftovers from an interleave).
    wt = reg.get_or_create(source_session="session-shared", agent_identity="claude")
    status = subprocess.check_output(
        ["git", "-C", wt.path, "status", "--porcelain"], text=True, env=_env())
    assert status.strip() == "", "worktree must be clean (no staged leftovers)"
    # Every committed write produced exactly one queue entry.
    committed = results.count("committed")
    assert pq.size() == committed