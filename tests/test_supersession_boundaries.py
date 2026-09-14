"""Boundary behaviour of the supersession write gate through the tool layer (#259)."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import data_olympus.write_gate as write_gate
from data_olympus.auth import PathBlocklist
from data_olympus.git_ops import GitOps
from data_olympus.index import Index
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.tools_onboarding import kb_bootstrap_project_fn
from data_olympus.tools_write import (
    commit_multifile_in_worktree,
    kb_propose_edit_fn,
    kb_resolve_pending_fn,
)
from data_olympus.worktrees import WorktreeRegistry
from data_olympus.write_gate import CommitSnapshot, SnapshotUnavailable, WriteSerializer

FAKE = "ghp_" + "FAKE" * 9


@pytest.fixture
def state(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}.items():
        monkeypatch.setenv(k, v)
    repo = tmp_path / "main"
    (repo / "projects" / "demo").mkdir(parents=True)
    (repo / "projects" / "demo" / "old.md").write_text(
        "---\nid: DEMO-OLD\ntype: decision\nstatus: draft\ntier: T3\n---\n# Old\n")
    (repo / "universal" / "foundation").mkdir(parents=True)
    (repo / "universal" / "foundation" / "STD-U-001.md").write_text(
        "---\nid: STD-U-001\ntype: standard\nstatus: active\ntier: T1\n---\n# T1\n")
    env = {**os.environ}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=env,
                   capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, env=env,
                   capture_output=True)
    git = GitOps(repo)
    idx = Index(Path(tempfile.mkdtemp()) / "index.db", status_autofill=False)
    idx.build(repo, source_commit="seed")
    return {
        "repo": repo, "idx": idx,
        "worktrees": WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts")),
        "push_queue": PushQueue(queue_root=str(tmp_path / "pq")),
        "pending": PendingQueue(pending_root=str(tmp_path / "pending")),
        "rate_limiter": SlidingWindowLimiter(max_per_hour=1000),
        "blocklist": PathBlocklist(tier_blocks=[], path_blocks=[]),
    }


def _draft(doc_id: str, extra: str = "") -> str:
    return f"---\nid: {doc_id}\ntype: decision\nstatus: draft\ntier: T3\n{extra}---\n# {doc_id}\n"


def _propose(state, path: str, text: str, confidence: float):  # noqa: ANN001, ANN202
    return kb_propose_edit_fn(
        target_path=path, postimage=text, base_commit="HEAD", base_blob_sha=None,
        target_file_hash=None, reason="t", source_session="s", agent_identity="claude",
        confidence=confidence, confidence_threshold=0.85, worktrees=state["worktrees"],
        push_queue=state["push_queue"], pending=state["pending"],
        rate_limiter=state["rate_limiter"], blocklist=state["blocklist"],
        remote_addr="1.2.3.4", idx=state["idx"],
    )


def _resolve(state, pending_id: str, **kw):  # noqa: ANN001, ANN003, ANN202
    return kb_resolve_pending_fn(
        pending_id=pending_id, decision="approve", edited_text=kw.pop("edited_text", None),
        worktrees=state["worktrees"], push_queue=state["push_queue"],
        pending=state["pending"], source_session="s", agent_identity="operator", **kw,
    )


def test_snapshot_failure_at_approval_restores_the_entry(state, monkeypatch) -> None:
    text = _draft("DEMO-NEW", "supersedes: DEMO-OLD\n")
    parked = _propose(state, "projects/demo/new.md", text, 0.3)
    assert parked.status == "pending_confirmation"

    def unavailable(_self):  # noqa: ANN001, ANN202
        raise SnapshotUnavailable("boom")

    monkeypatch.setattr(CommitSnapshot, "_load", unavailable)
    resolved = _resolve(state, parked.pending_id)

    assert resolved.status == "rejected_invalid_document"
    assert "unresolved_target_unverifiable" in (resolved.reason or "")
    assert [e["state"] for e in state["pending"].list()] == ["pending"]
    assert state["pending"].locks_held() == 1


def test_pending_only_target_does_not_resolve(state) -> None:
    parked = _propose(state, "projects/demo/pred.md", _draft("DEMO-PRED"), 0.3)
    assert parked.status == "pending_confirmation"

    text = _draft("DEMO-SUCC", "supersedes: DEMO-PRED\n")
    resp = _propose(state, "projects/demo/succ.md", text, 0.95)

    assert resp.status == "rejected_invalid_document", (resp.status, resp.reason)
    assert "unresolved_supersedes_target" in (resp.reason or "")


def test_reciprocal_pending_entries_recover_by_the_documented_sequence(state) -> None:
    a_text = _draft("DEMO-A", "supersedes: DEMO-B\n")
    b_text = _draft("DEMO-B", "superseded_by: DEMO-A\n")
    a = _propose(state, "projects/demo/a.md", a_text, 0.3)
    b = _propose(state, "projects/demo/b.md", b_text, 0.3)

    assert _resolve(state, a.pending_id).status == "rejected_invalid_document"
    assert _resolve(state, b.pending_id).status == "rejected_invalid_document"

    assert _resolve(state, a.pending_id, edited_text=_draft("DEMO-A")).status == "committed"
    assert _resolve(state, b.pending_id).status == "committed"
    again = _propose(state, "projects/demo/a.md", a_text, 0.95)
    assert again.status == "committed", (again.status, again.reason)


def test_override_resolve_still_redacts_a_credential_shaped_target(state) -> None:
    text = _draft("DEMO-NEW", f"supersedes: {FAKE}\n")
    parked = _propose(state, "projects/demo/new.md", text, 0.3)
    resolved = _resolve(state, parked.pending_id, override_secret_scan=True)

    assert resolved.status == "rejected_invalid_document"
    assert "unresolved_supersedes_target" in (resolved.reason or "")
    assert FAKE not in (resolved.reason or "")


def test_mixed_deterministic_and_snapshot_errors_still_reject_single_file(state) -> None:
    text = ("---\nid: STD-U-001\ntype: bogus\nstatus: active\ntier: T1\n"
            "supersedes: GHOST\n---\n# T1\n")
    resp = _propose(state, "universal/foundation/STD-U-001.md", text, 0.95)
    assert resp.status == "rejected_invalid_document", (resp.status, resp.reason)


def test_mixed_errors_still_reject_at_the_bootstrap_prediction(state) -> None:
    idx = MagicMock()
    idx.list_by_prefix.return_value = []
    idx.list_with_remote_url.return_value = []
    idx.id_to_path_map.return_value = {}
    files = [{"target_path": "projects/p/README.md",
              "postimage": "---\nid: projects-p-README\ntype: bogus\nstatus: active\n"
                           "tier: T3\nsupersedes: GHOST\n---\n# P\n"}]
    resp = kb_bootstrap_project_fn(
        idx=idx, workspace="p", component=None, workspace_remote_url=None,
        component_remote_url=None, files=files, source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85, worktrees=state["worktrees"],
        push_queue=state["push_queue"], pending=state["pending"],
        rate_limiter=state["rate_limiter"], blocklist=state["blocklist"],
    )
    assert resp.status == "rejected_invalid_document", (resp.status, resp.reason)
    assert resp.reason


def test_bundle_transaction_reads_the_tree_with_at_most_three_git_calls(state, monkeypatch) -> None:
    calls: list[list[str]] = []
    real_run = subprocess.run

    def counting(args, *a, **kw):  # noqa: ANN001, ANN002, ANN003, ANN202
        # ``subprocess`` is one module object, so this sees every git call in
        # the process (worktree setup, add, commit). Count only tree reads.
        if args and args[0] == "git" and (
            "ls-tree" in args or "cat-file" in args or "HEAD^{commit}" in args
            or "show" in args
        ):
            calls.append(list(args))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(write_gate.subprocess, "run", counting)
    sha, _push = commit_multifile_in_worktree(
        worktrees=state["worktrees"], push_queue=state["push_queue"],
        pending=state["pending"], serializer=WriteSerializer(), idx=None,
        source_session="s", agent_identity="claude",
        files=[
            {"target_path": "projects/demo/one.md",
             "postimage": _draft("DEMO-1", "supersedes: DEMO-OLD\n")},
            {"target_path": "projects/demo/two.md",
             "postimage": _draft("DEMO-2", "supersedes: DEMO-1\n")},
            {"target_path": "projects/demo/three.md", "postimage": _draft("DEMO-3")},
        ],
        subject="bootstrap", target_tier="T3", target_path_for_msg="projects/demo",
        confidence=0.95,
    )
    assert sha
    assert len(calls) <= 3, [c[3:5] for c in calls]
