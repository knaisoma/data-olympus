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

import contextlib
import json
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
    # Wire the pieces the way the server does (issues #253, #254): one shared
    # serializer across the claim lifecycle and the GC teardown, and a real
    # claim guard on git's history-rewriting paths. A fixture without these
    # cannot exercise the protections at all.
    serializer = WriteSerializer()
    holder = {}

    def claims_at_risk(ref):
        # Production's guard RECONCILES before reporting, at the configured
        # claim TTL rather than at zero age. A fixture that only inspects
        # cannot exercise the races that ordering exists to prevent.
        pen_ = holder.get("pending")
        if pen_ is None:
            return []
        with contextlib.suppress(Exception):
            pen_.reconcile_claims(min_age_sec=900)
        return list(pen_.claims_at_risk(ref))

    git = GitOps(main, claim_guard=claims_at_risk, serializer=serializer)
    reg = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"),
                           serializer=serializer)
    pq = PushQueue(queue_root=str(tmp_path / "push-q"))
    pen = PendingQueue(pending_root=str(tmp_path / "pending"),
                       serializer=serializer)
    holder["pending"] = pen
    rl = SlidingWindowLimiter(max_per_hour=1000)
    bl = PathBlocklist(tier_blocks=[], path_blocks=[])
    return git, reg, pq, pen, rl, bl, serializer


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
    git, reg, pq, pen, rl, bl, serializer = _server_pieces(tmp_path, main)

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
    git, reg, pq, pen, rl, bl, serializer = _server_pieces(tmp_path, main)
    audit = AuditLog(log_path=str(tmp_path / "audit.log"), hmac_key="")

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
    git, reg, pq, pen, rl, bl, serializer = _server_pieces(tmp_path, main)

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

# --- interrupted resolve recovery (issues #253, #254) -------------------------


def _propose_pending(reg, pq, pen, rl, bl, serializer):  # noqa: ANN001, ANN202
    """Propose below the auto-commit threshold so the write parks as pending."""
    from data_olympus.tools_write import kb_propose_edit_fn

    resp = kb_propose_edit_fn(
        target_path="decisions/DEC-resolve.md",
        postimage="---\nid: DEC-resolve\ntype: decision\nstatus: accepted\n"
                  "tier: meta\n---\nresolved content\n",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        reason="r", source_session="s1", agent_identity="claude",
        confidence=0.4, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.1.1.1",
        serializer=serializer,
    )
    assert resp.status == "pending_confirmation", resp
    return resp.pending_id


def _resolve_env(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    for k, v in _env().items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)
    monkeypatch.setenv("KB_GOVERNED_LANE_PROTECTION", "off")
    _remote, main = _bare_remote_with_clone(tmp_path)
    git, reg, pq, pen, rl, bl, serializer = _server_pieces(tmp_path, main)
    # The SAME serializer the queue and the registry hold, not a fresh one:
    # otherwise nothing in these tests shares a lock and the concurrency
    # guarantees they claim to cover are not exercised at all.
    return main, git, reg, pq, pen, rl, bl, serializer


def test_resolved_commit_carries_its_claim_link_and_records_the_outcome(
    tmp_path, monkeypatch,
) -> None:
    """The happy path establishes the evidence recovery depends on: the commit
    names the claim it satisfied, and the entry is consumed, not restored."""
    from data_olympus.tools_write import kb_resolve_pending_fn

    main, git, reg, pq, pen, rl, bl, serializer = _resolve_env(tmp_path, monkeypatch)
    pending_id = _propose_pending(reg, pq, pen, rl, bl, serializer)

    resp = kb_resolve_pending_fn(
        pending_id=pending_id, decision="approve", edited_text=None,
        worktrees=reg, push_queue=pq, pending=pen, source_session="s1",
        agent_identity="claude", serializer=serializer,
    )

    assert resp.status == "committed"
    assert pen.list() == []
    assert pen.locks_held() == 0
    wt_path = str(reg.get_or_create(
        source_session="s1", agent_identity="claude").path)
    body = subprocess.run(
        ["git", "-C", wt_path, "log", "-1", "--format=%B"],
        check=True, capture_output=True, text=True, env=_env(),
    ).stdout
    # A real trailer LINE, not a substring anywhere in the message: forged text
    # in a subject or another trailer's value must never count as evidence.
    assert f"KB-Pending-Id: {pending_id}" in body.splitlines()
    assert "KB-Target-Path: decisions/DEC-resolve.md" in body.splitlines()


def test_an_unobserved_commit_is_not_restored(tmp_path, monkeypatch) -> None:
    """The reviewer's named regression. git commit succeeds, the following sha
    lookup fails, and the entry must NOT go back to pending: the decision may
    already have been applied, and re-presenting it would duplicate the write.
    """
    import data_olympus.tools_write as tw
    from data_olympus.tools_write import _WriteOutcomeUnknown, kb_resolve_pending_fn

    main, git, reg, pq, pen, rl, bl, serializer = _resolve_env(tmp_path, monkeypatch)
    pending_id = _propose_pending(reg, pq, pen, rl, bl, serializer)

    real_check_output = tw.subprocess.check_output
    # Fail ONLY the post-commit sha lookup. The pre-write context capture also
    # runs rev-parse, and breaking that too would test a different fault.
    seen = {"pre_write": False}

    def flaky(cmd, *a, **kw):  # noqa: ANN001, ANN202
        if "rev-parse" in cmd and "HEAD" in cmd and "--abbrev-ref" not in cmd:
            if seen["pre_write"]:
                raise OSError("cannot read HEAD")
            seen["pre_write"] = True
        return real_check_output(cmd, *a, **kw)

    monkeypatch.setattr(tw.subprocess, "check_output", flaky)
    try:
        kb_resolve_pending_fn(
            pending_id=pending_id, decision="approve", edited_text=None,
            worktrees=reg, push_queue=pq, pending=pen, source_session="s1",
            agent_identity="claude", serializer=serializer,
        )
    except _WriteOutcomeUnknown:
        pass
    else:
        raise AssertionError("an unobserved commit must surface as unknown")
    monkeypatch.setattr(tw.subprocess, "check_output", real_check_output)

    entries = pen.list()
    assert [e["state"] for e in entries] == ["claimed"]
    assert pen.claim_record(pending_id)["write_outcome"]["outcome"] == "unknown"
    # And reconciliation finds the commit that really was made, so the decision
    # is closed rather than offered again.
    def find_commit(record):  # noqa: ANN001, ANN202
        # The production search, not a hand-written substring match, so this
        # test exercises the strict trailer parsing and the target binding.
        return git.find_claim_commit(
            ref=record["session_ref"],
            since_sha=record["pre_write_ref_sha"],
            pending_id=record["pending_id"],
            target_path=record["target_path"],
        )

    results = pen.reconcile_claims(min_age_sec=0, find_commit=find_commit)
    assert [r["outcome"] for r in results] == ["committed"], results
    assert pen.list() == []
    assert pen.locks_held() == 0


def test_a_failed_write_context_prevents_the_commit(tmp_path, monkeypatch) -> None:
    """Recovery needs the session ref and the pre-write tip, captured before the
    write. If they cannot be recorded the write must not start: continuing would
    leave recovery with no search reference and no guard protecting the
    evidence. Failing here is safe because nothing has been written, so it is a
    provable non-commit and the entry is restorable."""
    from data_olympus.tools_write import (
        _WriteContextUnavailable,
        kb_resolve_pending_fn,
    )

    main, git, reg, pq, pen, rl, bl, serializer = _resolve_env(tmp_path, monkeypatch)
    pending_id = _propose_pending(reg, pq, pen, rl, bl, serializer)

    def refuse(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("state volume is read-only")

    monkeypatch.setattr(pen, "record_write_context", refuse)
    try:
        kb_resolve_pending_fn(
            pending_id=pending_id, decision="approve", edited_text=None,
            worktrees=reg, push_queue=pq, pending=pen, source_session="s1",
            agent_identity="claude", serializer=serializer,
        )
    except _WriteContextUnavailable:
        pass
    else:
        raise AssertionError("the write must not start without recovery context")

    monkeypatch.undo()
    # Provably nothing committed, so the entry is back and re-resolvable.
    assert [e["state"] for e in pen.list()] == ["pending"]
    wt_path = str(reg.get_or_create(
        source_session="s1", agent_identity="claude").path)
    assert not os.path.exists(os.path.join(wt_path, "decisions/DEC-resolve.md"))


def test_a_signal_killed_commit_leaves_the_entry_claimed(tmp_path, monkeypatch) -> None:
    """git can update the ref and then be killed. Recording that as a failure
    would restore an entry whose write may already have landed."""
    import data_olympus.tools_write as tw
    from data_olympus.tools_write import _WriteOutcomeUnknown, kb_resolve_pending_fn

    main, git, reg, pq, pen, rl, bl, serializer = _resolve_env(tmp_path, monkeypatch)
    pending_id = _propose_pending(reg, pq, pen, rl, bl, serializer)
    real_run = tw.subprocess.run

    def killed(cmd, *a, **kw):  # noqa: ANN001, ANN202
        if "commit" in cmd:
            raise subprocess.CalledProcessError(-9, cmd)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(tw.subprocess, "run", killed)
    try:
        kb_resolve_pending_fn(
            pending_id=pending_id, decision="approve", edited_text=None,
            worktrees=reg, push_queue=pq, pending=pen, source_session="s1",
            agent_identity="claude", serializer=serializer,
        )
    except _WriteOutcomeUnknown:
        pass
    else:
        raise AssertionError("a signalled commit must surface as unknown")
    monkeypatch.undo()

    assert [e["state"] for e in pen.list()] == ["claimed"]
    assert pen.claim_record(pending_id)["write_outcome"]["outcome"] == "unknown"


def test_a_live_resolver_still_reports_its_own_success(tmp_path, monkeypatch) -> None:
    """Reconciliation forced INTO the gap between commit and finalization, with
    the claim already older than the TTL, must not consume the claim out from
    under its live owner.

    Age thresholds only shrink that gap: with KB_PENDING_CLAIM_TTL_SEC=1 a
    resolver taking two seconds is eligible while still alive. What removes it
    is finalizing inside the commit's own serializer acquisition. This test
    drives reconciliation at the exact moment the old code lost the race, so it
    fails if finalization moves back outside that acquisition.
    """
    import data_olympus.tools_write as tw
    from data_olympus.tools_write import kb_resolve_pending_fn

    main, git, reg, pq, pen, rl, bl, serializer = _resolve_env(tmp_path, monkeypatch)
    pending_id = _propose_pending(reg, pq, pen, rl, bl, serializer)

    reconciled: list[object] = []
    real_enqueue = tw._enqueue_after_commit

    def enqueue_then_reconcile(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        # The commit is durable and its outcome is recorded; this is exactly
        # where a guard or sweep pass used to consume the claim.
        state = real_enqueue(*args, **kwargs)
        reconciled.append(pen.reconcile_claims(min_age_sec=0))
        return state

    monkeypatch.setattr(tw, "_enqueue_after_commit", enqueue_then_reconcile)
    resp = kb_resolve_pending_fn(
        pending_id=pending_id, decision="approve", edited_text=None,
        worktrees=reg, push_queue=pq, pending=pen, source_session="s1",
        agent_identity="claude", serializer=serializer,
    )
    monkeypatch.undo()

    assert reconciled, "the interleaving did not run"
    assert resp.status == "committed", resp
    assert resp.commit_sha
    assert pen.list() == []
    assert pen.locks_held() == 0


def test_a_deferred_push_does_not_consume_its_retry_budget(tmp_path) -> None:
    """A rebase deferred to preserve claim evidence is not a publication
    failure. Charging it to the retry budget freezes a perfectly good commit
    while it waits for reconciliation, and a frozen entry stays frozen."""
    from data_olympus.git_ops import ClaimEvidenceAtRiskError
    from data_olympus.push_queue import PushQueue

    pq = PushQueue(queue_root=str(tmp_path / "q"))
    pq.enqueue(sha="a" * 40, worktree_path=str(tmp_path / "wt"), meta={})

    def always_deferred(*_a, **_k):
        raise ClaimEvidenceAtRiskError(ref="kb-session/x", pending_ids=["z" * 32])

    for _ in range(3):
        pq.drain(push_fn=always_deferred, max_attempts=2)

    assert pq.size() == 1
    assert pq.frozen_count() == 0, "a deferral must not freeze a good commit"
    with open(os.path.join(str(tmp_path / "q"), "a" * 40 + ".json")) as f:
        entry = json.load(f)
    assert entry["attempts"] == 0, entry
