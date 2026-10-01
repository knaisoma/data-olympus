"""kb_propose_edit accepts base_commit only as HEAD or a commit id."""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from data_olympus.audit_log import AuditLog
from data_olympus.auth import PathBlocklist
from data_olympus.git_ops import GitOps
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.tools_write import _validate_base_markers, kb_propose_edit_fn
from data_olympus.worktrees import WorktreeRegistry

if TYPE_CHECKING:
    from pathlib import Path

_POST = "---\nid: mem-x\ntype: memory\nstatus: draft\ntier: meta\n---\nbody\n"
# Credential-shaped, so the value must never be stored.
_CREDENTIAL_SHAPED = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}.items():
        monkeypatch.setenv(k, v)
    # idx=None would otherwise demote every write; this file is about base markers.
    monkeypatch.setenv("KB_GOVERNED_LANE_PROTECTION", "off")
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "seed.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True,
                   capture_output=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    return {
        "head": head,
        "audit_path": tmp_path / "events.log",
        "pending_root": tmp_path / "pending",
        "kwargs": dict(
            postimage=_POST, base_blob_sha=None, target_file_hash=None,
            reason="r", agent_identity="agent", confidence_threshold=0.85,
            worktrees=WorktreeRegistry(git=GitOps(repo), worktree_root=str(tmp_path / "wts")),
            push_queue=PushQueue(queue_root=str(tmp_path / "pq")),
            pending=PendingQueue(pending_root=str(tmp_path / "pending")),
            rate_limiter=SlidingWindowLimiter(max_per_hour=1000),
            blocklist=PathBlocklist([], []), remote_addr="1.2.3.4",
            audit_log=AuditLog(log_path=str(tmp_path / "events.log")), idx=None,
        ),
    }


def _stored_text(pipeline: dict) -> str:
    audit = pipeline["audit_path"]
    text = audit.read_text() if audit.exists() else ""
    root = pipeline["pending_root"]
    if root.exists():
        text += "".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file())
    return text


@pytest.mark.parametrize("confidence", [0.99, 0.10])
@pytest.mark.parametrize("value", [
    _CREDENTIAL_SHAPED,
    "origin/main",
    "HEAD~1",
    "ABCDEF1",          # upper-case hex
    "abc12",            # too short
    "a" * 65,           # too long
    " " + "a" * 40,     # surrounding whitespace
])
def test_malformed_base_commit_is_rejected_without_being_stored(
    pipeline: dict, value: str, confidence: float,
) -> None:
    resp = kb_propose_edit_fn(
        target_path="memory/a.md", base_commit=value, source_session="s1",
        confidence=confidence, **pipeline["kwargs"],
    )
    assert resp.status == "rejected_invalid_base"
    assert "base_commit" in (resp.reason or "")
    assert value not in (resp.reason or "")
    assert value.strip() not in _stored_text(pipeline)
    assert pipeline["kwargs"]["pending"].size() == 0
    assert pipeline["kwargs"]["push_queue"].size() == 0


def test_non_string_base_commit_is_rejected() -> None:
    assert "base_commit" in (_validate_base_markers(None, None, base_commit=123) or "")


@pytest.mark.parametrize("value", [
    None, "", "HEAD", "head", "abc1234", "a" * 40, "0123456789abcdef" * 4,
])
def test_accepted_base_commit_forms(value: str | None) -> None:
    assert _validate_base_markers(None, None, base_commit=value) is None


@pytest.mark.parametrize("confidence,expected", [
    (0.99, "committed"), (0.10, "pending_confirmation"),
])
@pytest.mark.parametrize("form", ["HEAD", "full", "short"])
def test_head_and_commit_ids_still_propose(
    pipeline: dict, confidence: float, expected: str, form: str,
) -> None:
    base = {"HEAD": "HEAD", "full": pipeline["head"], "short": pipeline["head"][:7]}[form]
    resp = kb_propose_edit_fn(
        target_path="memory/b.md", base_commit=base, source_session=f"s-{form}",
        confidence=confidence, **pipeline["kwargs"],
    )
    assert resp.status == expected, resp
