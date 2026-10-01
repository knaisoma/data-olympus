"""Bootstrap writes only new files under the workspace (or component) it onboards."""
from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from data_olympus.auth import PathBlocklist
from data_olympus.git_ops import GitOps
from data_olympus.index import Index
from data_olympus.onboarding_inflight import BootstrapInFlight
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.tools_onboarding import kb_bootstrap_project_fn
from data_olympus.tools_write import _WriteRejected, commit_multifile_in_worktree
from data_olympus.worktrees import WorktreeRegistry
from data_olympus.write_gate import WriteSerializer

if TYPE_CHECKING:
    from pathlib import Path

_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}

DEC_1 = ("---\nid: DEC-1\ntype: decision\nstatus: accepted\ntier: meta\n"
         "---\nThe accepted decision text.\n")
DEC_1_DRAFT = ("---\nid: DEC-1\ntype: decision\nstatus: draft\ntier: meta\n"
               "---\nReplacement text.\n")


def _project_doc(doc_id: str, status: str = "draft") -> str:
    return f"---\nid: {doc_id}\ntype: project\nstatus: {status}\ntier: T3\n---\n# {doc_id}\n"


@pytest.fixture
def kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    for k, v in _GIT_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("KB_GOVERNED_LANE_PROTECTION", raising=False)
    repo = tmp_path / "main"
    repo.mkdir()
    env = {**os.environ, **_GIT_ENV}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True,
                   env=env, capture_output=True)
    (repo / "seed.md").write_text("seed\n")
    (repo / "decisions").mkdir()
    (repo / "decisions" / "DEC-1.md").write_text(DEC_1)
    (repo / "projects" / "half").mkdir(parents=True)
    (repo / "projects" / "half" / "README.md").write_text(_project_doc("half-README"))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=env,
                   capture_output=True)
    idx = Index(tmp_path / "index.db")
    idx.build(repo, source_commit="seed", today="2026-10-01")
    return {
        "repo": repo,
        "idx": idx,
        "worktrees": WorktreeRegistry(git=GitOps(repo), worktree_root=str(tmp_path / "wts")),
        "push_queue": PushQueue(queue_root=str(tmp_path / "pq")),
        "pending": PendingQueue(pending_root=str(tmp_path / "pending")),
        "inflight": str(tmp_path / "inflight"),
    }


def _bootstrap(kb: dict, workspace: str, files: list[dict[str, str]], *,
               confidence: float = 0.99, component: str | None = None):
    return kb_bootstrap_project_fn(
        idx=kb["idx"], workspace=workspace, component=component,
        workspace_remote_url=None, component_remote_url=None,
        files=files, source_session=f"s-{workspace}", agent_identity="agent",
        confidence=confidence, confidence_threshold=0.85,
        worktrees=kb["worktrees"], push_queue=kb["push_queue"], pending=kb["pending"],
        rate_limiter=SlidingWindowLimiter(max_per_hour=1000),
        blocklist=PathBlocklist(tier_blocks=[], path_blocks=[]),
        in_flight=BootstrapInFlight(kb["inflight"]),
    )


def _committed_text(kb: dict, sha: str, path: str) -> str:
    return subprocess.run(["git", "-C", str(kb["repo"]), "show", f"{sha}:{path}"],
                          capture_output=True, text=True, check=True).stdout


@pytest.mark.parametrize("confidence", [0.99, 0.10])
def test_new_workspace_bootstrap_cannot_target_an_existing_document(
    kb: dict, confidence: float,
) -> None:
    resp = _bootstrap(kb, "fresh-ws",
                      [{"target_path": "decisions/DEC-1.md", "postimage": DEC_1_DRAFT}],
                      confidence=confidence)
    assert resp.status == "rejected_path_not_indexable_or_blocked"
    assert resp.rejected_paths == ["decisions/DEC-1.md"]
    assert resp.commit_sha is None
    assert kb["push_queue"].size() == 0
    assert kb["pending"].size() == 0
    assert (kb["repo"] / "decisions" / "DEC-1.md").read_text() == DEC_1


def test_new_workspace_bootstrap_cannot_create_a_file_outside_it(kb: dict) -> None:
    resp = _bootstrap(kb, "fresh-ws", [
        {"target_path": "projects/fresh-ws/README.md",
         "postimage": _project_doc("fresh-ws-README")},
        {"target_path": "decisions/new.md",
         "postimage": "---\nid: DEC-NEW\ntype: decision\nstatus: draft\ntier: meta\n---\nx\n"},
    ])
    assert resp.status == "rejected_path_not_indexable_or_blocked"
    assert resp.rejected_paths == ["decisions/new.md"]
    assert kb["push_queue"].size() == 0
    assert kb["pending"].size() == 0


def test_bootstrap_cannot_reach_a_sibling_workspace_by_prefix(kb: dict) -> None:
    """``projects/fresh-ws-other/`` shares a string prefix with the workspace
    root but is a different workspace."""
    resp = _bootstrap(kb, "fresh-ws", [
        {"target_path": "projects/fresh-ws-other/README.md",
         "postimage": _project_doc("other-README")},
    ])
    assert resp.status == "rejected_path_not_indexable_or_blocked"


def test_component_bootstrap_stays_under_the_component(kb: dict) -> None:
    resp = _bootstrap(kb, "fresh-ws", [
        {"target_path": "projects/fresh-ws/README.md",
         "postimage": _project_doc("fresh-ws-README")},
    ], component="svc")
    assert resp.status == "rejected_path_not_indexable_or_blocked"
    assert resp.rejected_paths == ["projects/fresh-ws/README.md"]


def test_new_workspace_bootstrap_still_commits_under_its_root(kb: dict) -> None:
    files = [
        {"target_path": "projects/fresh-ws/README.md",
         "postimage": _project_doc("fresh-ws-README")},
        {"target_path": "projects/fresh-ws/AGENTS.md",
         "postimage": _project_doc("fresh-ws-AGENTS")},
    ]
    resp = _bootstrap(kb, "fresh-ws", files)
    assert resp.status == "committed", resp
    assert kb["push_queue"].size() == 1
    subject = subprocess.run(
        ["git", "-C", str(kb["repo"]), "log", "-1", "--format=%s", resp.commit_sha],
        capture_output=True, text=True, check=True).stdout.strip()
    # The subject names every path the commit writes.
    assert "projects/fresh-ws/README.md" in subject
    assert "projects/fresh-ws/AGENTS.md" in subject
    assert _committed_text(kb, resp.commit_sha, "decisions/DEC-1.md") == DEC_1


def test_partial_bootstrap_still_fills_only_the_gap(kb: dict) -> None:
    resp = _bootstrap(kb, "half", [
        {"target_path": "projects/half/README.md", "postimage": _project_doc("half-README-2")},
        {"target_path": "projects/half/AGENTS.md", "postimage": _project_doc("half-AGENTS")},
    ])
    assert resp.status == "committed", resp
    names = subprocess.run(
        ["git", "-C", str(kb["repo"]), "show", "--name-only", "--format=", resp.commit_sha],
        capture_output=True, text=True, check=True).stdout.split()
    assert names == ["projects/half/AGENTS.md"]


def test_partial_bootstrap_foreign_path_keeps_its_status(kb: dict) -> None:
    resp = _bootstrap(kb, "half", [
        {"target_path": "decisions/DEC-1.md", "postimage": DEC_1_DRAFT},
    ])
    assert resp.status == "rejected_already_onboarded"
    assert kb["push_queue"].size() == 0


# ---- commit layer: bootstrap bundles create new files only -----------------


def _commit(kb: dict, files: list[tuple[str, str]], **kwargs):
    return commit_multifile_in_worktree(
        worktrees=kb["worktrees"], push_queue=kb["push_queue"], pending=kb["pending"],
        serializer=WriteSerializer(), idx=None, source_session="s-commit",
        agent_identity="agent",
        files=[{"target_path": p, "postimage": t} for p, t in files],
        subject="bootstrap", target_tier="T3", target_path_for_msg="projects/half/",
        confidence=0.99, **kwargs,
    )


def test_new_files_only_commit_refuses_an_existing_target(kb: dict) -> None:
    with pytest.raises(_WriteRejected) as info:
        _commit(kb, [
            ("projects/half/AGENTS.md", _project_doc("half-AGENTS")),
            ("projects/half/README.md", _project_doc("half-README-2")),
        ], new_files_only=True)
    assert info.value.response.status == "rejected_already_onboarded"
    assert info.value.response.target_path == "projects/half/README.md"
    assert kb["push_queue"].size() == 0
    assert kb["pending"].locks_held() == 0
    wt = kb["worktrees"].get_or_create(source_session="s-commit", agent_identity="agent")
    status = subprocess.run(["git", "-C", wt.path, "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status.strip() == ""


def test_commit_without_new_files_only_still_updates_an_existing_file(kb: dict) -> None:
    updated = _project_doc("half-README") + "more\n"
    sha, _push = _commit(kb, [("projects/half/README.md", updated)])
    assert sha
    assert _committed_text(kb, sha, "projects/half/README.md") == updated
