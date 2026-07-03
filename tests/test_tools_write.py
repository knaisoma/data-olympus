"""Tests for the 4 write MCP tool functions."""
from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from data_olympus.auth import PathBlocklist
from data_olympus.git_ops import GitOps
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.tools_write import (
    kb_list_pending_fn,
    kb_propose_edit_fn,
    kb_propose_memory_fn,
    kb_resolve_pending_fn,
)
from data_olympus.worktrees import WorktreeRegistry

if TYPE_CHECKING:
    import pytest


def _env() -> dict[str, str]:
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def _state(tmp_path):
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=_env())
    (repo / "seed.md").write_text("seed")
    subprocess.run(["git", "add", "seed.md"], cwd=repo, check=True, env=_env())
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=_env())
    git = GitOps(repo)
    reg = WorktreeRegistry(git=git, worktree_root=str(tmp_path / "wts"))
    pq = PushQueue(queue_root=str(tmp_path / "push-q"))
    pen = PendingQueue(pending_root=str(tmp_path / "pending"))
    rl = SlidingWindowLimiter(max_per_hour=10)
    bl = PathBlocklist(tier_blocks=[], path_blocks=[])
    return git, reg, pq, pen, rl, bl


def _set_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")


def test_kb_propose_memory_high_confidence_auto_commits(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="test memory body",
        tags=["test"],
        source_session="session-abc",
        agent_identity="claude",
        confidence=0.9,
        confidence_threshold=0.85,
        worktrees=reg,
        push_queue=pq,
        pending=pen,
        rate_limiter=rl,
        blocklist=bl,
        remote_addr="10.0.0.1",
    )
    assert resp.status == "committed"
    assert resp.commit_sha
    assert resp.push_state == "queued"
    # Queue entry exists.
    assert pq.size() == 1


def test_kb_propose_memory_low_confidence_returns_pending(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="lowconf",
        tags=[],
        source_session="session-abc",
        agent_identity="claude",
        confidence=0.4,
        confidence_threshold=0.85,
        worktrees=reg,
        push_queue=pq,
        pending=pen,
        rate_limiter=rl,
        blocklist=bl,
        remote_addr="10.0.0.1",
    )
    assert resp.status == "pending_confirmation"
    assert resp.pending_id
    assert resp.proposal_text == "lowconf"
    assert pen.size() == 1


def test_kb_propose_memory_rejects_rate_limited(tmp_path) -> None:
    git, reg, pq, pen, _, bl = _state(tmp_path)
    rl = SlidingWindowLimiter(max_per_hour=0)
    resp = kb_propose_memory_fn(
        text="x",
        tags=[],
        source_session="session-abc",
        agent_identity="claude",
        confidence=0.9,
        confidence_threshold=0.85,
        worktrees=reg,
        push_queue=pq,
        pending=pen,
        rate_limiter=rl,
        blocklist=bl,
        remote_addr="10.0.0.1",
    )
    assert resp.status == "rejected_rate_limited"


def test_kb_propose_memory_rejects_blocked_tier(tmp_path) -> None:
    git, reg, pq, pen, rl, _ = _state(tmp_path)
    bl = PathBlocklist(tier_blocks=["memory"], path_blocks=[])
    resp = kb_propose_memory_fn(
        text="x",
        tags=[],
        source_session="session-abc",
        agent_identity="claude",
        confidence=0.9,
        confidence_threshold=0.85,
        worktrees=reg,
        push_queue=pq,
        pending=pen,
        rate_limiter=rl,
        blocklist=bl,
        remote_addr="10.0.0.1",
    )
    assert resp.status == "rejected_path_blocked"


def test_kb_propose_memory_rejects_symlink_escape(tmp_path, monkeypatch) -> None:
    """Regression for the Codex blocker: a KB commit that plants the memory inbox
    as a symlink to an outside dir must NOT cause a write outside the worktree."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    evil = tmp_path / "evil"
    evil.mkdir()
    (repo / "memory").mkdir()
    os.symlink(str(evil), str(repo / "memory" / "inbox"))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=_env())
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "plant symlink"],
                   check=True, env=_env())
    resp = kb_propose_memory_fn(
        text="escape attempt", tags=[], source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "rejected_symlink_escape"
    assert list(evil.iterdir()) == []  # nothing written outside the worktree
    assert pq.size() == 0


def test_kb_propose_edit_rejects_symlink_escape(tmp_path, monkeypatch) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    evil = tmp_path / "evil-edit"
    evil.mkdir()
    # Plant universal/ as a symlink to the evil dir, committed into the tree.
    os.symlink(str(evil), str(repo / "universal"))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=_env())
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "plant symlink dir"],
                   check=True, env=_env())
    resp = kb_propose_edit_fn(
        target_path="universal/foundation/STD-U-001.md",
        postimage="pwned\n", base_commit="HEAD", base_blob_sha=None,
        target_file_hash=None, reason="escape", source_session="s",
        agent_identity="claude", confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "rejected_symlink_escape"
    assert list(evil.iterdir()) == []
    assert pq.size() == 0


def _seed_t1_file(repo) -> tuple[str, str]:
    """Seed a T1 file in the repo; return (target_path, base_blob_sha)."""
    p = repo / "universal" / "foundation" / "STD-U-001.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\nid: STD-U-001\ntier: T1\n---\n# T1\nbody\n")
    import subprocess
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=_env())
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed t1"], check=True, env=_env())
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "ls-tree", "HEAD", str(p.relative_to(repo))],
        text=True,
    ).split()[2]
    return "universal/foundation/STD-U-001.md", sha


def test_kb_propose_edit_rejects_traversal(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_edit_fn(
        target_path="projects/foo/../../memory/x.md",
        postimage="x", base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        reason="test", source_session="s", agent_identity="claude", confidence=0.9,
        confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "rejected_path_not_indexable"


def test_kb_propose_edit_high_conf_commits(tmp_path, monkeypatch) -> None:
    # Need git env for the commit inside the function (same pattern as Task 12).
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo  # note: GitOps stores path as self._repo
    target, blob = _seed_t1_file(repo)
    resp = kb_propose_edit_fn(
        target_path=target,
        postimage="new body\n",
        base_commit="HEAD",
        base_blob_sha=blob,
        target_file_hash=None,
        reason="fix",
        source_session="s",
        agent_identity="claude",
        confidence=0.9,
        confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "committed"
    assert pq.size() == 1


def test_kb_propose_edit_low_conf_pending(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    target, blob = _seed_t1_file(repo)
    resp = kb_propose_edit_fn(
        target_path=target,
        postimage="new body\n",
        base_commit="HEAD",
        base_blob_sha=blob,
        target_file_hash=None,
        reason="lowconf",
        source_session="s",
        agent_identity="claude",
        confidence=0.5,
        confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "pending_confirmation"
    assert pen.size() == 1


def test_kb_list_pending_returns_entries(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    kb_propose_memory_fn(
        text="x", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    resp = kb_list_pending_fn(pending=pen)
    assert len(resp.pending) == 1


def test_kb_resolve_pending_approve_commits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@e.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@e.com")
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    m = kb_propose_memory_fn(
        text="memory body", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    resp = kb_resolve_pending_fn(
        pending_id=m.pending_id,
        decision="approve",
        edited_text=None,
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="claude",
    )
    assert resp.status == "committed"
    assert resp.commit_sha
    assert pen.size() == 0


def test_kb_resolve_pending_reject_clears(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    m = kb_propose_memory_fn(
        text="x", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    resp = kb_resolve_pending_fn(
        pending_id=m.pending_id, decision="reject", edited_text=None,
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="claude",
    )
    assert resp.status == "rejected"
    assert pen.size() == 0


# ---- item 3: YAML frontmatter injection + full-postimage cap ----


def test_render_memory_newline_in_agent_identity_cannot_forge_keys() -> None:
    """A newline-laden agent_identity must not inject top-level YAML keys."""
    import yaml

    from data_olympus.tools_write import _render_memory
    payload = "claude\nid: GDEC-001\nstatus: accepted\nsupersedes: GDEC-000"
    out = _render_memory(text="body", tags=[], agent_identity=payload)
    fm_text = out.split("---\n", 2)[1]
    fm = yaml.safe_load(fm_text)
    # The whole payload survives ONLY as the created_by value; no forged keys.
    assert fm["created_by"] == payload
    assert "id" not in fm
    assert "status" not in fm
    assert "supersedes" not in fm


def test_render_memory_bracket_in_tag_cannot_forge_keys() -> None:
    """A tag containing '], key: value' must not break out of the list."""
    import yaml

    from data_olympus.tools_write import _render_memory
    out = _render_memory(
        text="body", tags=["a], id: forged", "normal"], agent_identity="claude"
    )
    fm_text = out.split("---\n", 2)[1]
    fm = yaml.safe_load(fm_text)
    assert "id" not in fm
    assert fm["tags"] == ["a], id: forged", "normal"]


def test_propose_memory_forged_tag_does_not_forge_id(tmp_path, monkeypatch) -> None:
    """End-to-end: a malicious tag through the propose path is stored inertly."""
    import yaml
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="body", tags=["x], id: DEC-999"], source_session="s",
        agent_identity="claude", confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    assert resp.status == "committed"
    # Read the committed file back out of the worktree and confirm no forged id.
    wt = reg.get_or_create(source_session="s", agent_identity="claude")
    import glob
    written = glob.glob(os.path.join(wt.path, "memory", "inbox", "*.md"))
    assert written
    with open(written[0]) as fh:
        content = fh.read()
    fm = yaml.safe_load(content.split("---\n", 2)[1])
    assert "id" not in fm


def test_propose_memory_cap_counts_full_rendered_postimage(
    tmp_path, monkeypatch,
) -> None:
    """item 3: the size cap must count the rendered frontmatter+body, not just
    the body. A large tags list that fits under a body-only check but pushes the
    full postimage over the cap must be rejected."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    # Body is tiny, but many long tags inflate the rendered frontmatter.
    big_tags = ["t" * 100 for _ in range(50)]
    resp = kb_propose_memory_fn(
        text="hi", tags=big_tags, source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        max_text_bytes=200,
    )
    assert resp.status == "rejected_payload_too_large"
    assert pq.size() == 0


# ---- item 4: canonical path (backslash bypass) in propose_edit ----


def test_propose_edit_rejects_backslash_path(tmp_path) -> None:
    """A backslash path must not slip through as a root-level literal file."""
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_edit_fn(
        target_path="decisions\\x.md",
        postimage="body\n", base_commit="HEAD", base_blob_sha=None,
        target_file_hash=None, reason="", source_session="s",
        agent_identity="claude", confidence=0.3, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    # decisions/ is an indexed prefix, so the canonicalized path is accepted and
    # parked as pending — but under the CANONICAL forward-slash path.
    assert resp.status == "pending_confirmation"
    entry = pen.get(resp.pending_id)
    assert entry["target_path"] == "decisions/x.md"
    assert "\\" not in entry["target_path"]


def test_propose_edit_rejects_control_char_path(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_edit_fn(
        target_path="decisions/x\n.md",
        postimage="body\n", base_commit="HEAD", base_blob_sha=None,
        target_file_hash=None, reason="", source_session="s",
        agent_identity="claude", confidence=0.3, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    assert resp.status == "rejected_path_not_indexable"


# ---- item 2: resolve edited_text bypasses the postimage cap ----


def test_resolve_edited_text_over_cap_is_rejected(tmp_path, monkeypatch) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    m = kb_propose_memory_fn(
        text="small", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    resp = kb_resolve_pending_fn(
        pending_id=m.pending_id, decision="approve",
        edited_text="X" * 5000,
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="operator",
        max_postimage_bytes=100,
    )
    assert resp.status == "rejected_edited_text_too_large"
    # The pending entry is left in place (not consumed) so it can be re-edited.
    assert pen.size() == 1
    assert pq.size() == 0


def test_resolve_edited_text_under_cap_commits(tmp_path, monkeypatch) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    m = kb_propose_memory_fn(
        text="small", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    resp = kb_resolve_pending_fn(
        pending_id=m.pending_id, decision="approve",
        edited_text="edited body\n",
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="operator",
        max_postimage_bytes=1_000_000,
    )
    assert resp.status == "committed"


# ---- item 9: unknown/expired pending_id ----


def test_resolve_unknown_pending_id_raises_not_found(tmp_path) -> None:
    from data_olympus.pending import PendingNotFoundError
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    import pytest
    with pytest.raises(PendingNotFoundError):
        kb_resolve_pending_fn(
            pending_id="deadbeefdeadbeefdeadbeefdeadbeef",
            decision="approve", edited_text=None,
            worktrees=reg, push_queue=pq, pending=pen,
            source_session="s", agent_identity="operator",
        )


def test_pending_get_rejects_traversal_id(tmp_path) -> None:
    from data_olympus.pending import PendingNotFoundError
    pen = PendingQueue(pending_root=str(tmp_path / "pending"))
    import pytest
    with pytest.raises(PendingNotFoundError):
        pen.get("../../etc/passwd")
