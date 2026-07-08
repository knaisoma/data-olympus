"""Integration tests for maintenance.maybe_update_ledger's commit side effect
(issue #113, scenarios 5, 6, 7): the committed ledger, idempotence / loop
guard, commit-failure resilience, and the remediation flow.

Exercises the SAME write machinery (WorktreeRegistry / PushQueue /
PendingQueue / WriteSerializer / tools_write.commit_multifile_in_worktree)
real writes go through, against real git repos (a bare "origin" plus a main
checkout), mirroring tests/test_write_pipeline_integration.py.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import TYPE_CHECKING

from data_olympus.audit_log import AuditLog
from data_olympus.git_ops import GitOps
from data_olympus.index import Index
from data_olympus.maintenance import maybe_update_ledger
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.tools_read import kb_health_fn
from data_olympus.worktrees import WorktreeRegistry
from data_olympus.write_gate import WriteSerializer

if TYPE_CHECKING:
    from pathlib import Path

LEDGER_PATH = "tooling/maintenance-ledger.md"


def _env() -> dict[str, str]:
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def _run(*args: str, cwd: str) -> None:
    subprocess.run(list(args), cwd=cwd, check=True, env=_env(), capture_output=True)


def _bare_remote_with_clone(tmp_path: Path) -> tuple[Path, Path]:
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


def _harness(tmp_path: Path):
    remote, main = _bare_remote_with_clone(tmp_path)
    git = GitOps(main)
    worktrees = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"))
    push_queue = PushQueue(queue_root=str(tmp_path / "push-q"))
    pending = PendingQueue(pending_root=str(tmp_path / "pending"))
    serializer = WriteSerializer()
    return remote, main, git, worktrees, push_queue, pending, serializer


def _sync_main_from_origin(main: Path) -> None:
    subprocess.run(["git", "-C", str(main), "fetch", "origin", "main"], check=True,
                   env=_env(), capture_output=True)
    subprocess.run(["git", "-C", str(main), "merge", "--ff-only", "origin/main"],
                   check=True, env=_env(), capture_output=True)


def _publish_ledger_commit(worktrees: WorktreeRegistry, git: GitOps, main: Path) -> None:
    """Simulate the next production tick: push the system worktree's commit to
    origin, then fast-forward "main" from it (what git_pull_loop's ff_merge
    would do)."""
    wt = worktrees.get_or_create(
        source_session="system:maintenance-ledger",
        agent_identity="data-olympus-system",
    )
    git.push(wt.path)
    _sync_main_from_origin(main)


def test_ledger_commit_lifecycle_idempotence_and_remediation(
    tmp_path: Path, monkeypatch,
) -> None:
    """Combined scenario 5 + 7 walk: dirty corpus commits the ledger; an
    unchanged recomputation (the ledger's own commit landing) does NOT create a
    new commit; fixing the underlying doc flips the flag and produces a NEW
    commit, after which pending_actions disappears."""
    for k, v in _env().items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)
    remote, main, git, worktrees, push_queue, pending, serializer = _harness(tmp_path)

    # Make the corpus dirty: a doc with no front matter at all.
    workflows = main / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "WF-001.md").write_text("# Ship something\n\nno front matter.\n")
    _run("git", "add", "-A", cwd=str(main))
    _run("git", "commit", "-m", "add dirty doc", cwd=str(main))
    _run("git", "push", "origin", "main", cwd=str(main))

    idx = Index(tmp_path / "idx.db", maintenance_ledger_path=LEDGER_PATH)
    idx.build(main, source_commit="c1", today="2026-07-08")
    assert idx.maintenance_state is not None
    assert idx.maintenance_state.is_dirty is True

    al = AuditLog(log_path=str(tmp_path / "audit.log"))

    # --- scenario 5: first dirty computation commits the ledger -----------
    sha1 = maybe_update_ledger(
        idx=idx, worktrees=worktrees, push_queue=push_queue, pending=pending,
        serializer=serializer, ledger_path=LEDGER_PATH, audit_log=al,
    )
    assert sha1 is not None

    _publish_ledger_commit(worktrees, git, main)
    idx.build(main, source_commit="c2", today="2026-07-08")
    assert idx.maintenance_state.is_dirty is True  # WF-001 is still status-less

    # Recomputation triggered by the ledger's own commit landing must be a
    # no-op: no new commit for an unchanged state.
    sha2 = maybe_update_ledger(
        idx=idx, worktrees=worktrees, push_queue=push_queue, pending=pending,
        serializer=serializer, ledger_path=LEDGER_PATH, audit_log=al,
    )
    assert sha2 is None

    events = list(al.iter_filtered())
    committed = [
        e for e in events
        if e.get("event_type") == "maintenance_ledger" and e.get("status") == "committed"
    ]
    assert len(committed) == 1
    assert committed[0]["commit_sha"] == sha1

    # --- scenario 7: remediate, rebuild, flag flips, ledger re-committed ---
    (workflows / "WF-001.md").write_text(
        "---\nid: WF-001\ntype: workflow\nstatus: active\ntier: meta\n---\nfixed.\n"
    )
    _run("git", "add", "-A", cwd=str(main))
    _run("git", "commit", "-m", "fix missing status", cwd=str(main))
    _run("git", "push", "origin", "main", cwd=str(main))

    idx.build(main, source_commit="c3", today="2026-07-08")
    assert idx.maintenance_state.is_dirty is False
    assert idx.maintenance_state.status_present_in_all_kb_entries is True

    sha3 = maybe_update_ledger(
        idx=idx, worktrees=worktrees, push_queue=push_queue, pending=pending,
        serializer=serializer, ledger_path=LEDGER_PATH, audit_log=al,
    )
    assert sha3 is not None
    assert sha3 != sha1

    # pending_actions has silenced automatically (no manual acking).
    resp = kb_health_fn(idx=idx, last_git_pull_at=None, staleness_degraded_sec=600)
    assert resp.pending_actions is None

    events2 = list(al.iter_filtered())
    committed2 = [
        e for e in events2
        if e.get("event_type") == "maintenance_ledger" and e.get("status") == "committed"
    ]
    assert len(committed2) == 2


def test_commit_failure_is_logged_and_does_not_break_serving(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    """scenario 6: a simulated commit failure is logged/audited and swallowed;
    kb_health/kb_consult keep serving off the unaffected index."""
    for k, v in _env().items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)
    _remote, main, _git, worktrees, push_queue, pending, serializer = _harness(tmp_path)

    idx = Index(tmp_path / "idx.db", maintenance_ledger_path=LEDGER_PATH)
    idx.build(main, source_commit="seed", today="2026-07-08")
    assert idx.maintenance_state is not None

    def _boom(**_kwargs: object) -> tuple[str, str]:
        raise RuntimeError("simulated git failure")

    monkeypatch.setattr(
        "data_olympus.tools_write.commit_multifile_in_worktree", _boom,
    )
    al = AuditLog(log_path=str(tmp_path / "audit.log"))
    caplog.set_level(logging.WARNING, logger="data_olympus.maintenance")

    sha = maybe_update_ledger(
        idx=idx, worktrees=worktrees, push_queue=push_queue, pending=pending,
        serializer=serializer, ledger_path=LEDGER_PATH, audit_log=al,
    )
    assert sha is None
    assert "simulated git failure" in caplog.text

    events = list(al.iter_filtered())
    failed = [
        e for e in events
        if e.get("event_type") == "maintenance_ledger" and e.get("status") == "commit_failed"
    ]
    assert len(failed) == 1

    # Serving continues: kb_health must not raise and reports off the index
    # exactly as before the failed commit attempt.
    resp = kb_health_fn(idx=idx, last_git_pull_at=None, staleness_degraded_sec=600)
    assert resp.kb_commit == "seed"


def _dirty_main(tmp_path: Path):
    """A harness whose main checkout carries one status-less doc (dirty corpus),
    pushed to origin, plus a built Index over it."""
    remote, main, git, worktrees, push_queue, pending, serializer = _harness(tmp_path)
    workflows = main / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "WF-001.md").write_text("# Ship something\n\nno front matter.\n")
    _run("git", "add", "-A", cwd=str(main))
    _run("git", "commit", "-m", "add dirty doc", cwd=str(main))
    _run("git", "push", "origin", "main", cwd=str(main))
    idx = Index(tmp_path / "idx.db", maintenance_ledger_path=LEDGER_PATH)
    idx.build(main, source_commit="c1", today="2026-07-08")
    assert idx.maintenance_state is not None
    assert idx.maintenance_state.is_dirty is True
    return remote, main, git, worktrees, push_queue, pending, serializer, idx


def test_no_duplicate_commit_before_publication(tmp_path: Path, monkeypatch) -> None:
    """Codex review blocker: two consecutive maybe_update_ledger calls on the
    SAME dirty index, with the first ledger commit NOT yet pushed / merged /
    re-indexed (the pull loop's steady state between publication ticks), must
    produce exactly ONE commit. Before the in-process last-committed memo, the
    second call saw a still-stale index copy, decided 'state changed', and
    committed a duplicate on every tick."""
    for k, v in _env().items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)
    (_remote, _main, _git, worktrees, push_queue, pending, serializer, idx) = (
        _dirty_main(tmp_path)
    )
    al = AuditLog(log_path=str(tmp_path / "audit.log"))

    sha1 = maybe_update_ledger(
        idx=idx, worktrees=worktrees, push_queue=push_queue, pending=pending,
        serializer=serializer, ledger_path=LEDGER_PATH, audit_log=al,
    )
    assert sha1 is not None

    # NO push, NO merge into main, NO index rebuild: the very next tick.
    sha2 = maybe_update_ledger(
        idx=idx, worktrees=worktrees, push_queue=push_queue, pending=pending,
        serializer=serializer, ledger_path=LEDGER_PATH, audit_log=al,
    )
    assert sha2 is None

    committed = [
        e for e in al.iter_filtered()
        if e.get("event_type") == "maintenance_ledger" and e.get("status") == "committed"
    ]
    assert len(committed) == 1


def test_no_duplicate_commit_after_restart_before_publication(
    tmp_path: Path, monkeypatch,
) -> None:
    """Restart window: a NEW process (fresh Index, empty in-process memo) whose
    index still predates the ledger's publication must NOT re-commit an
    identical state -- the system worktree's HEAD already carries it (the
    worktree survives restarts; unpushed commits block its GC)."""
    for k, v in _env().items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)
    (_remote, main, _git, worktrees, push_queue, pending, serializer, idx) = (
        _dirty_main(tmp_path)
    )
    al = AuditLog(log_path=str(tmp_path / "audit.log"))
    sha1 = maybe_update_ledger(
        idx=idx, worktrees=worktrees, push_queue=push_queue, pending=pending,
        serializer=serializer, ledger_path=LEDGER_PATH, audit_log=al,
    )
    assert sha1 is not None

    # Simulate a process restart: a brand-new Index (memo lost), rebuilt from
    # the same main checkout (the ledger commit has not been merged into it).
    idx2 = Index(tmp_path / "idx2.db", maintenance_ledger_path=LEDGER_PATH)
    idx2.build(main, source_commit="c1", today="2026-07-08")
    sha2 = maybe_update_ledger(
        idx=idx2, worktrees=worktrees, push_queue=push_queue, pending=pending,
        serializer=serializer, ledger_path=LEDGER_PATH, audit_log=al,
    )
    assert sha2 is None

    committed = [
        e for e in al.iter_filtered()
        if e.get("event_type") == "maintenance_ledger" and e.get("status") == "committed"
    ]
    assert len(committed) == 1


def test_unindexable_ledger_path_is_skipped(tmp_path: Path, monkeypatch, caplog) -> None:
    """Codex review concern: a KB_MAINTENANCE_LEDGER_PATH outside every indexed
    prefix must be refused (logged, audited, no commit) rather than committing
    a doc the index will never serve."""
    for k, v in _env().items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)
    (_remote, _main, _git, worktrees, push_queue, pending, serializer, idx) = (
        _dirty_main(tmp_path)
    )
    al = AuditLog(log_path=str(tmp_path / "audit.log"))
    caplog.set_level(logging.WARNING, logger="data_olympus.maintenance")

    sha = maybe_update_ledger(
        idx=idx, worktrees=worktrees, push_queue=push_queue, pending=pending,
        serializer=serializer, ledger_path="outside/ledger.md", audit_log=al,
    )
    assert sha is None
    assert "not_in_indexed_prefixes" in caplog.text

    events = list(al.iter_filtered())
    assert not any(
        e.get("event_type") == "maintenance_ledger" and e.get("status") == "committed"
        for e in events
    )
    skipped = [
        e for e in events
        if e.get("event_type") == "maintenance_ledger" and e.get("status") == "skipped_bad_path"
    ]
    assert len(skipped) == 1
