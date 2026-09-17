"""Tests for contest input validation, lock persistence, and read-path projection (issue #241).

Covers:
1. Stage 1 pre-rate-limiter validation:
   - Secret scan precedence over shape and bounds (zero-leak).
   - Rejecting non-object contest.
   - Rejecting unexpected keys in contest.
   - Missing, non-list, empty, >10, >200 chars, duplicate contradicts items.
   - Non-string, >500 chars, empty reason in contest.
2. Stage 2 post-rate-limiter index check:
   - Unavailable index -> rejected_contest_index_unavailable (503).
   - Non-indexed doc in contradicts -> rejected_invalid_contest.
   - Target document self-contradiction -> rejected_invalid_contest.
3. Propose edit flow with valid contest:
   - High confidence demoted to pending_confirmation with demotion_reason="contest_declared".
   - Lock file carries pending_id, intent="contest", contradicts.
   - Pending entry meta carries intent, contradicts, contest_reason.
4. Lock persistence across claim and restore:
   - Path lock remains held and retains intent/contradicts across claim and restore.
5. Dual rationale separation on kb_list_pending:
   - kb_list_pending_fn surfaces intent and contest object alongside operational reason.
6. REST API status code and memory rejection:
   - propose/memory rejects contest with 400 rejected_invalid_contest.
   - _propose_status maps rejected_contest_index_unavailable to 503 and invalid to 400.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, Any

import pytest

import data_olympus.durable as durable
import data_olympus.pending as pending_mod
from data_olympus.auth import PathBlocklist
from data_olympus.git_ops import GitOps
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.rest_api import _propose_status
from data_olympus.tools_write import (
    _check_contest_index,
    _validate_contest,
    kb_list_pending_fn,
    kb_propose_edit_fn,
)
from data_olympus.worktrees import WorktreeRegistry

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _scoped_win_atomic_writes(monkeypatch):
    """Scoped Windows compatibility shim for atomic writes in tests."""
    if sys.platform == "win32":
        def _win_atomic_write_json(path: str, payload: dict[str, Any]) -> None:
            parent = os.path.dirname(path) or "."
            fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp.", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

        def _win_atomic_remove(path: str) -> None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)

        monkeypatch.setattr(pending_mod, "atomic_write_json", _win_atomic_write_json)
        monkeypatch.setattr(pending_mod, "atomic_remove", _win_atomic_remove)
        monkeypatch.setattr(durable, "atomic_write_json", _win_atomic_write_json)
        monkeypatch.setattr(durable, "atomic_remove", _win_atomic_remove)


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


def _apply_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env().items():
        monkeypatch.setenv(k, v)


def _setup_harness(tmp_path: Path):
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=_env())
    (repo / "seed.md").write_text("# Seed\n")
    subprocess.run(["git", "add", "seed.md"], cwd=repo, check=True, env=_env())
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=_env())
    head_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, env=_env()
    ).strip()

    git = GitOps(repo)
    reg = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"))
    pq = PushQueue(queue_root=str(tmp_path / "push-q"))
    pen = PendingQueue(pending_root=str(tmp_path / "pending"))
    rl = SlidingWindowLimiter(max_per_hour=100)
    bl = PathBlocklist(tier_blocks=[], path_blocks=[])
    return repo, head_sha, reg, pq, pen, rl, bl


class _MockIndex:
    def __init__(self, id_map: dict[str, str] | None = None, raise_exc: bool = False):
        self._id_map = id_map or {}
        self._raise_exc = raise_exc

    def id_to_path_map(self) -> dict[str, str]:
        if self._raise_exc:
            raise RuntimeError("Database connection pool exhausted")
        return dict(self._id_map)


# =============================================================================
# 1. Stage 1 In-Memory Validation Tests (_validate_contest)
# =============================================================================

def test_validate_contest_none_returns_none():
    clean, err = _validate_contest(None)
    assert clean is None
    assert err is None


def test_validate_contest_non_mapping_rejected():
    for non_map in ["not-a-dict", 123, ["a"], True]:
        clean, err = _validate_contest(non_map)
        assert clean is None
        assert err is not None
        assert err.status == "rejected_invalid_contest"
        assert "must be an object" in err.reason


def test_validate_contest_secret_scan_precedence_in_contradicts():
    secret_val = "ghp_" + "A" * 36
    clean, err = _validate_contest({"contradicts": [secret_val], "foo": "extra"})
    assert clean is None
    assert err is not None
    assert err.status == "rejected_secret_detected"
    assert "ghp_" not in err.reason


def test_validate_contest_secret_scan_precedence_in_reason():
    secret_val = "AKIA" + "0" * 16
    clean, err = _validate_contest({"contradicts": ["STD-001"], "reason": f"prefix {secret_val}"})
    assert clean is None
    assert err is not None
    assert err.status == "rejected_secret_detected"
    assert "AKIA" not in err.reason


def test_validate_contest_extra_keys_rejected():
    clean, err = _validate_contest({"contradicts": ["STD-001"], "unknown_key": "val"})
    assert clean is None
    assert err is not None
    assert err.status == "rejected_invalid_contest"
    assert "unexpected key" in err.reason


def test_validate_contest_missing_contradicts():
    clean, err = _validate_contest({"reason": "some reason"})
    assert clean is None
    assert err is not None
    assert err.status == "rejected_invalid_contest"
    assert "missing required field 'contradicts'" in err.reason


def test_validate_contest_contradicts_type_and_bounds():
    # Not a list
    _, err = _validate_contest({"contradicts": "STD-001"})
    assert err is not None and "must be a list" in err.reason

    # Empty list
    _, err = _validate_contest({"contradicts": []})
    assert err is not None and "between 1 and 10 items" in err.reason

    # > 10 items
    _, err = _validate_contest({"contradicts": [f"STD-{i:03d}" for i in range(11)]})
    assert err is not None and "between 1 and 10 items" in err.reason

    # Non-string item
    _, err = _validate_contest({"contradicts": [123]})
    assert err is not None and "items must be strings" in err.reason

    # Empty string item
    _, err = _validate_contest({"contradicts": ["   "]})
    assert err is not None and "non-empty strings" in err.reason

    # > 200 chars item
    _, err = _validate_contest({"contradicts": ["A" * 201]})
    assert err is not None and "at most 200 characters" in err.reason

    # Duplicate items
    _, err = _validate_contest({"contradicts": ["STD-001", "STD-001"]})
    assert err is not None and "contains duplicate items" in err.reason


def test_validate_contest_reason_bounds():
    # Non-string reason
    _, err = _validate_contest({"contradicts": ["STD-001"], "reason": 123})
    assert err is not None and "reason must be a string" in err.reason

    # Reason > 500 chars
    _, err = _validate_contest({"contradicts": ["STD-001"], "reason": "A" * 501})
    assert err is not None and "exceeds 500 characters" in err.reason

    # Empty or whitespace-only reason
    _, err = _validate_contest({"contradicts": ["STD-001"], "reason": "   \n  "})
    assert err is not None and "cannot be empty or whitespace-only" in err.reason


def test_validate_contest_clean_valid():
    contest_data = {"contradicts": ["STD-001", "STD-002"], "reason": "Disputes clause 4"}
    clean, err = _validate_contest(contest_data)
    assert err is None
    assert clean == {
        "contradicts": ["STD-001", "STD-002"],
        "reason": "Disputes clause 4",
    }


# =============================================================================
# 2. Stage 2 Post-Rate-Limiter Index Checks (_check_contest_index)
# =============================================================================

def test_check_contest_index_unavailable():
    # None index
    err = _check_contest_index(["STD-001"], "decisions/D-001.md", None)
    assert err is not None
    assert err.status == "rejected_contest_index_unavailable"

    # Exception during id_to_path_map
    mock_idx = _MockIndex(raise_exc=True)
    err = _check_contest_index(["STD-001"], "decisions/D-001.md", mock_idx)  # type: ignore[arg-type]
    assert err is not None
    assert err.status == "rejected_contest_index_unavailable"


def test_check_contest_index_doc_not_found():
    mock_idx = _MockIndex({"STD-001": "universal/STD-001.md"})
    err = _check_contest_index(["STD-999"], "decisions/D-001.md", mock_idx)  # type: ignore[arg-type]
    assert err is not None
    assert err.status == "rejected_invalid_contest"
    assert "not found in index" in err.reason
    assert "STD-999" not in err.reason  # Zero-leak invariant


def test_check_contest_index_self_contradiction():
    mock_idx = _MockIndex({
        "STD-001": "universal/STD-001.md",
        "STD-002": "decisions/D-001.md",
    })
    # Target path matches the contradicted doc's path
    err = _check_contest_index(["STD-002"], "decisions/D-001.md", mock_idx)  # type: ignore[arg-type]
    assert err is not None
    assert err.status == "rejected_invalid_contest"
    assert "cannot contradict itself" in err.reason


def test_check_contest_index_clean():
    mock_idx = _MockIndex({
        "STD-001": "universal/STD-001.md",
        "STD-002": "universal/STD-002.md",
    })
    err = _check_contest_index(["STD-001", "STD-002"], "decisions/D-001.md", mock_idx)  # type: ignore[arg-type]
    assert err is None


# =============================================================================
# 3. End-to-End kb_propose_edit_fn with Contest
# =============================================================================

def test_kb_propose_edit_contest_parks_and_persists_lock(tmp_path, monkeypatch):
    _apply_git_env(monkeypatch)
    repo, head_sha, reg, pq, pen, rl, bl = _setup_harness(tmp_path)
    target = "decisions/D-001.md"
    mock_idx = _MockIndex({"STD-100": "universal/STD-100.md"})

    resp = kb_propose_edit_fn(
        target_path=target,
        postimage="# D-001\nNew contested decision\n",
        base_commit=head_sha,
        base_blob_sha=None,
        target_file_hash=None,
        reason="Routine operational update",
        source_session="sess-contest-1",
        agent_identity="agent-alpha",
        confidence=0.99,  # High confidence: normally auto-commits, but contest MUST park
        confidence_threshold=0.85,
        worktrees=reg,
        push_queue=pq,
        pending=pen,
        rate_limiter=rl,
        blocklist=bl,
        remote_addr="127.0.0.1",
        can_auto_commit=True,
        idx=mock_idx,  # type: ignore[arg-type]
        contest={
            "contradicts": ["STD-100"],
            "reason": "Direct dispute with legacy STD-100 clause",
        },
    )

    assert resp.status == "pending_confirmation"
    assert resp.demotion_reason == "contest_declared"
    assert resp.pending_id is not None
    pid = resp.pending_id

    # Verify lock file on disk
    locks = pen.held_locks()
    assert len(locks) == 1
    assert locks[0]["target_path"] == target
    assert locks[0]["pending_id"] == pid
    assert locks[0]["intent"] == "contest"
    assert locks[0]["contradicts"] == ["STD-100"]

    # Verify pending entry meta on disk
    entry = pen.get(pid)
    assert entry["meta"]["intent"] == "contest"
    assert entry["meta"]["contradicts"] == ["STD-100"]
    assert entry["meta"]["contest_reason"] == "Direct dispute with legacy STD-100 clause"
    # Note operational reason is preserved separately
    assert entry["meta"]["reason"] == "Routine operational update"


# =============================================================================
# 4. Lock Persistence Across Claim and Restore
# =============================================================================

def test_contest_lock_persistence_across_claim_and_restore(tmp_path, monkeypatch):
    _apply_git_env(monkeypatch)
    repo, head_sha, reg, pq, pen, rl, bl = _setup_harness(tmp_path)
    target = "decisions/D-002.md"
    mock_idx = _MockIndex({"STD-100": "universal/STD-100.md"})

    resp = kb_propose_edit_fn(
        target_path=target,
        postimage="# D-002\nAnother proposal\n",
        base_commit=head_sha,
        base_blob_sha=None,
        target_file_hash=None,
        reason="Update standard",
        source_session="sess-claim-1",
        agent_identity="agent-alpha",
        confidence=0.5,
        confidence_threshold=0.85,
        worktrees=reg,
        push_queue=pq,
        pending=pen,
        rate_limiter=rl,
        blocklist=bl,
        remote_addr="127.0.0.1",
        idx=mock_idx,  # type: ignore[arg-type]
        contest={"contradicts": ["STD-100"], "reason": "Disputes standard"},
    )
    pid = resp.pending_id
    assert pid is not None

    # Verify lock before claim
    locks_pre = pen.held_locks()
    assert locks_pre[0]["intent"] == "contest"
    assert locks_pre[0]["contradicts"] == ["STD-100"]

    # 1. Claim for resolve (holding lock)
    resolved = pen.claim_for_resolve(pid)
    assert resolved.pending_id == pid
    assert resolved.meta["intent"] == "contest"

    # Lock must remain held and keep metadata during claim
    locks_during = pen.held_locks()
    assert len(locks_during) == 1
    assert locks_during[0]["intent"] == "contest"
    assert locks_during[0]["contradicts"] == ["STD-100"]

    # 2. Gate rejects -> restore_resolve
    pen.restore_resolve(pid, claim_token=resolved.claim_token)

    # Lock must STILL be held and keep metadata after restore
    locks_post = pen.held_locks()
    assert len(locks_post) == 1
    assert locks_post[0]["intent"] == "contest"
    assert locks_post[0]["contradicts"] == ["STD-100"]


# =============================================================================
# 5. kb_list_pending_fn Projection & Dual Rationale Separation
# =============================================================================

def test_kb_list_pending_surfaces_dual_rationale(tmp_path, monkeypatch):
    _apply_git_env(monkeypatch)
    repo, head_sha, reg, pq, pen, rl, bl = _setup_harness(tmp_path)
    target = "decisions/D-003.md"
    mock_idx = _MockIndex({"STD-200": "universal/STD-200.md"})

    kb_propose_edit_fn(
        target_path=target,
        postimage="# D-003\nDual rationale proposal\n",
        base_commit=head_sha,
        base_blob_sha=None,
        target_file_hash=None,
        reason="Routine refactoring of rule D-003",
        source_session="sess-list-1",
        agent_identity="agent-beta",
        confidence=0.5,
        confidence_threshold=0.85,
        worktrees=reg,
        push_queue=pq,
        pending=pen,
        rate_limiter=rl,
        blocklist=bl,
        remote_addr="127.0.0.1",
        idx=mock_idx,  # type: ignore[arg-type]
        contest={
            "contradicts": ["STD-200"],
            "reason": "Contradicts outdated telemetry specification",
        },
    )

    list_resp = kb_list_pending_fn(pending=pen)
    assert len(list_resp.pending) == 1
    entry = list_resp.pending[0]

    # Operational reason is preserved
    assert entry.reason == "Routine refactoring of rule D-003"
    # Contest rationale is distinctly separated
    assert entry.intent == "contest"
    assert entry.contest is not None
    assert entry.contest.contradicts == ["STD-200"]
    assert entry.contest.contest_reason == "Contradicts outdated telemetry specification"


# =============================================================================
# 6. REST API Status Mapping
# =============================================================================

def test_rest_api_propose_status_mappings():
    assert _propose_status("rejected_contest_index_unavailable") == 503
    assert _propose_status("rejected_invalid_contest") == 400
    assert _propose_status("rejected_secret_detected") == 422
    assert _propose_status("pending_confirmation") == 202
