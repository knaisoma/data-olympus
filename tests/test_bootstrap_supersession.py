"""Bootstrap batch rules for supersession targets and rejection precedence (#259)."""
from __future__ import annotations

import os
import subprocess

import pytest

from data_olympus.auth import PathBlocklist  # noqa: F401 - parity with other write tests
from data_olympus.git_ops import GitOps
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.tools_onboarding import _redacted_reason
from data_olympus.tools_write import _WriteRejected, commit_multifile_in_worktree
from data_olympus.worktrees import WorktreeRegistry
from data_olympus.write_gate import WriteSerializer

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


@pytest.fixture
def state(tmp_path, monkeypatch):
    for k, v in _ENV.items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=_ENV)
    (repo / "seed.md").write_text("seed")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_ENV)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=_ENV)
    git = GitOps(repo)
    return {
        "worktrees": WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts")),
        "push_queue": PushQueue(queue_root=str(tmp_path / "pq")),
        "pending": PendingQueue(pending_root=str(tmp_path / "pending")),
        "serializer": WriteSerializer(),
    }


def _doc(doc_id: str, extra: str = "") -> str:
    return f"---\nid: {doc_id}\ntype: project\nstatus: active\ntier: T3\n{extra}---\n# {doc_id}\n"


def _commit(state, files):  # noqa: ANN001, ANN202
    return commit_multifile_in_worktree(
        **state, idx=None, source_session="s", agent_identity="claude",
        files=[{"target_path": p, "postimage": t} for p, t in files],
        subject="bootstrap", target_tier="T3", target_path_for_msg="projects/p",
        confidence=0.95,
    )


def test_batch_member_resolves_a_sibling(state) -> None:
    sha, _push = _commit(state, [
        ("projects/p/old.md", _doc("P-OLD", "superseded_by: P-NEW\n")),
        ("projects/p/new.md", _doc("P-NEW", "supersedes: P-OLD\n")),
    ])
    assert sha


def test_batch_member_with_missing_target_rejects_the_batch(state) -> None:
    with pytest.raises(_WriteRejected) as info:
        _commit(state, [
            ("projects/p/a.md", _doc("P-A")),
            ("projects/p/b.md", _doc("P-B", "supersedes: P-GHOST\n")),
        ])
    resp = info.value.response
    assert resp.status == "rejected_invalid_document"
    assert resp.target_path == "projects/p/b.md"
    assert "unresolved_supersedes_target" in (resp.reason or "")


def test_secret_outranks_duplicate_and_later_validation(state) -> None:
    fake = "ghp_" + "FAKE" * 9
    with pytest.raises(_WriteRejected) as dup_and_secret:
        _commit(state, [
            ("projects/p/a.md", _doc("P-DUP")),
            ("projects/p/b.md", _doc("P-DUP", f"note: {fake}\n")),
        ])
    assert dup_and_secret.value.response.status == "rejected_secret_detected"
    assert dup_and_secret.value.response.target_path == "projects/p/b.md"

    with pytest.raises(_WriteRejected) as invalid_then_secret:
        _commit(state, [
            ("projects/p/a.md", _doc("P-A", "supersedes: P-GHOST\n")),
            ("projects/p/b.md", _doc("P-B", f"note: {fake}\n")),
        ])
    assert invalid_then_secret.value.response.status == "rejected_secret_detected"
    assert invalid_then_secret.value.response.target_path == "projects/p/b.md"


def test_redacted_reason_never_carries_a_credential() -> None:
    fake = "ghp_" + "FAKE" * 9
    assert _redacted_reason(None) is None
    assert _redacted_reason("plain reason") == "plain reason"
    redacted = _redacted_reason(f"id '{fake}' used by two files")
    assert fake not in (redacted or "") and "redacted" in (redacted or "")
