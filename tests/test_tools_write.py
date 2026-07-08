"""Tests for the 4 write MCP tool functions."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
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
    # `status` IS a real stamped key (issue #109: memory stamping), but it must
    # be the server-stamped "proposed" value, never the payload's forged
    # "accepted" -- i.e. the embedded "status: accepted" did not escape into
    # its own top-level key.
    assert fm["status"] == "proposed"
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


# ---- issue #109: memory stamping (type/status) + evidence rendering ----


def test_render_memory_stamps_type_and_status() -> None:
    """Server-rendered memories are stamped `type: memory`, `status: proposed`
    so the existing status filter / rerank / in-force machinery applies with
    zero new code paths. Promotion out of `proposed` happens at review time."""
    import yaml

    from data_olympus.tools_write import _render_memory
    out = _render_memory(text="body", tags=[], agent_identity="claude")
    fm = yaml.safe_load(out.split("---\n", 2)[1])
    assert fm["type"] == "memory"
    assert fm["status"] == "proposed"


def test_render_memory_includes_evidence_when_supplied() -> None:
    import yaml

    from data_olympus.tools_write import _render_memory
    out = _render_memory(
        text="body", tags=[], agent_identity="claude",
        evidence=["saw it in the logs", "confirmed with operator"],
    )
    fm = yaml.safe_load(out.split("---\n", 2)[1])
    assert fm["evidence"] == ["saw it in the logs", "confirmed with operator"]


def test_render_memory_omits_evidence_key_when_absent() -> None:
    import yaml

    from data_olympus.tools_write import _render_memory
    out = _render_memory(text="body", tags=[], agent_identity="claude")
    fm = yaml.safe_load(out.split("---\n", 2)[1])
    assert "evidence" not in fm


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


# ---- 0.3.0 epic #72: CAS, validation gate, filename collision, double-resolve ----


def _build_index(repo):
    """Build an Index over the current repo HEAD so the validation gate has a
    live corpus to check duplicate ids against."""
    import tempfile

    from data_olympus.index import Index
    idx = Index(Path(tempfile.mkdtemp()) / "index.db")
    idx.build(Path(str(repo)), source_commit="seed")
    return idx


def test_propose_edit_stale_base_rejected(tmp_path, monkeypatch) -> None:
    """CAS (item 3): a base_blob_sha that does not match the current target
    content on the refreshed base is rejected rejected_stale_base without a
    commit."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    target, _real_blob = _seed_t1_file(repo)
    # Supply a WRONG base blob sha -> stale.
    from data_olympus.write_gate import _blob_sha
    wrong = _blob_sha(b"content the caller wrongly believes is there\n")
    resp = kb_propose_edit_fn(
        target_path=target, postimage="new body\n", base_commit="HEAD",
        base_blob_sha=wrong, target_file_hash=None, reason="fix",
        source_session="s", agent_identity="claude", confidence=0.95,
        confidence_threshold=0.85, worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "rejected_stale_base"
    assert pq.size() == 0


def test_propose_edit_correct_base_commits(tmp_path, monkeypatch) -> None:
    """CAS pass-through: the correct base_blob_sha commits normally."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    target, blob = _seed_t1_file(repo)
    # blob from _seed_t1_file is the ls-tree blob of the seeded content, which is
    # what the session worktree's base holds. It must match.
    resp = kb_propose_edit_fn(
        target_path=target, postimage="new body\n", base_commit="HEAD",
        base_blob_sha=blob, target_file_hash=None, reason="fix",
        source_session="s", agent_identity="claude", confidence=0.95,
        confidence_threshold=0.85, worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "committed"
    assert pq.size() == 1


def test_propose_edit_malformed_yaml_rejected(tmp_path, monkeypatch) -> None:
    """Validation gate (item 4): a postimage with unterminated frontmatter is
    rejected rejected_invalid_document, not committed."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    idx = _build_index(git._repo)
    resp = kb_propose_edit_fn(
        target_path="universal/foundation/STD-U-099.md",
        postimage="---\nid: X\n# never closed\nbody\n",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        reason="", source_session="s", agent_identity="claude", confidence=0.95,
        confidence_threshold=0.85, worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4", idx=idx,
    )
    assert resp.status == "rejected_invalid_document"
    assert pq.size() == 0


def test_propose_edit_duplicate_id_rejected(tmp_path, monkeypatch) -> None:
    """Validation gate (item 4): a forged duplicate id (already used at a
    different path) is rejected so the next index rebuild cannot break."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    # Seed STD-U-001 in the repo so the index knows that id -> that path.
    _seed_t1_file(repo)
    idx = _build_index(repo)
    forged = "---\nid: STD-U-001\ntype: standard\nstatus: active\ntier: T1\n---\nforged\n"
    resp = kb_propose_edit_fn(
        target_path="decisions/DEC-forged.md", postimage=forged,
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        reason="", source_session="s", agent_identity="claude", confidence=0.95,
        confidence_threshold=0.85, worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4", idx=idx,
    )
    assert resp.status == "rejected_invalid_document"
    assert "already used" in (resp.reason or "").lower()
    assert pq.size() == 0


def test_memory_filename_collision_distinct_sessions(tmp_path, monkeypatch) -> None:
    """item 6: two same-day, same-slug memories from DIFFERENT sessions get
    distinct filenames (the uniquifier), so neither overwrites the other."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    common = dict(
        text="daily standup note", tags=[], confidence=0.95,
        confidence_threshold=0.85, worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=SlidingWindowLimiter(max_per_hour=100), blocklist=bl,
        remote_addr="1.2.3.4",
    )
    r1 = kb_propose_memory_fn(source_session="session-A", agent_identity="claude",
                              **common)
    r2 = kb_propose_memory_fn(source_session="session-B", agent_identity="claude",
                              **common)
    assert r1.status == "committed"
    assert r2.status == "committed"
    # Both files exist under memory/inbox with DIFFERENT names.
    import glob
    wt_a = reg.get_or_create(source_session="session-A", agent_identity="claude")
    wt_b = reg.get_or_create(source_session="session-B", agent_identity="claude")
    files_a = {os.path.basename(p)
               for p in glob.glob(os.path.join(wt_a.path, "memory", "inbox", "*.md"))}
    files_b = {os.path.basename(p)
               for p in glob.glob(os.path.join(wt_b.path, "memory", "inbox", "*.md"))}
    assert files_a and files_b
    assert files_a != files_b  # distinct filenames, no silent overwrite


def test_double_resolve_second_reports_already_resolved_or_not_found(
    tmp_path, monkeypatch,
) -> None:
    """item 5: after one resolve commits, a second resolve of the same id does
    NOT produce a second commit. The loser surfaces already_resolved (concurrent
    window) or PendingNotFoundError (sequential, entry gone)."""
    _set_git_env(monkeypatch)
    from data_olympus.pending import PendingNotFoundError
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    m = kb_propose_memory_fn(
        text="a memory", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    first = kb_resolve_pending_fn(
        pending_id=m.pending_id, decision="approve", edited_text=None,
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="operator",
    )
    assert first.status == "committed"
    assert pq.size() == 1
    import pytest
    with pytest.raises(PendingNotFoundError):
        kb_resolve_pending_fn(
            pending_id=m.pending_id, decision="approve", edited_text=None,
            worktrees=reg, push_queue=pq, pending=pen,
            source_session="s", agent_identity="operator",
        )
    # Still exactly one commit enqueued.
    assert pq.size() == 1


# ---- Codex round-2 Blocker B: resolve gate rejection restores the pending entry ----


def test_resolve_stale_base_restores_pending_entry(tmp_path, monkeypatch) -> None:
    """If CAS rejects during a resolve, the pending entry is put back (not lost)
    and the path lock stays held, so the operator can re-resolve it."""
    _set_git_env(monkeypatch)
    from data_olympus.write_gate import _blob_sha
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    target, _blob = _seed_t1_file(repo)
    # Park a low-conf edit as pending with a STALE base_blob_sha so the resolve's
    # CAS gate rejects it.
    stale = _blob_sha(b"content the proposer wrongly believed\n")
    m = kb_propose_edit_fn(
        target_path=target, postimage="new body\n", base_commit="HEAD",
        base_blob_sha=stale, target_file_hash=None, reason="lowconf",
        source_session="s", agent_identity="claude", confidence=0.3,
        confidence_threshold=0.85, worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert m.status == "pending_confirmation"
    assert pen.size() == 1
    resp = kb_resolve_pending_fn(
        pending_id=m.pending_id, decision="approve", edited_text=None,
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="operator",
    )
    assert resp.status == "rejected_stale_base"
    assert pq.size() == 0
    # The entry is restored (not consumed) so it can be re-resolved.
    assert pen.size() == 1
    assert pen.locks_held() == 1  # path lock still held by the restored entry


# ---- Codex round-3: truthful push_state on post-commit enqueue failure ----


def test_enqueue_failure_reports_recovery_pending_not_queued(tmp_path, monkeypatch) -> None:
    """If BOTH the enqueue and the in-process recovery re-enqueue fail after a
    successful commit, the response push_state is the truthful
    enqueue_failed_recovery_pending (not 'queued'), and the commit still exists
    (recoverable by init_recovery)."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)

    class FailingPushQueue:
        def enqueue(self, **_kwargs):
            raise OSError("state volume full")

    resp = kb_propose_memory_fn(
        text="a note", tags=[], source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85, worktrees=reg,
        push_queue=FailingPushQueue(), pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    assert resp.status == "committed"
    assert resp.commit_sha  # the commit was made and is durable
    assert resp.push_state == "enqueue_failed_recovery_pending"


def test_enqueue_recovery_retry_succeeds_reports_queued(tmp_path, monkeypatch) -> None:
    """If the first enqueue fails but the in-process recovery retry succeeds, the
    push_state is 'queued' (the entry landed)."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)

    class FlakyPushQueue:
        def __init__(self):
            self.calls = 0
            self.enqueued = []

        def enqueue(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise OSError("transient")
            self.enqueued.append(kwargs["sha"])

    fq = FlakyPushQueue()
    resp = kb_propose_memory_fn(
        text="a note", tags=[], source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85, worktrees=reg,
        push_queue=fq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    assert resp.status == "committed"
    assert resp.push_state == "queued"
    assert fq.enqueued == [resp.commit_sha]


def test_resolve_enqueue_failure_reports_recovery_pending(tmp_path, monkeypatch) -> None:
    """Codex round-4: a resolve whose post-commit enqueue fails surfaces the
    truthful push_state on ResolvePendingResponse (not a bare committed), while
    still consuming the pending entry (the commit is durable)."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    # Park a low-conf memory as pending using the real queue.
    m = kb_propose_memory_fn(
        text="note", tags=[], source_session="s", agent_identity="claude",
        confidence=0.3, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )

    class FailingPushQueue:
        def enqueue(self, **_kwargs):
            raise OSError("state volume full")

    resp = kb_resolve_pending_fn(
        pending_id=m.pending_id, decision="approve", edited_text=None,
        worktrees=reg, push_queue=FailingPushQueue(), pending=pen,
        source_session="s", agent_identity="operator",
    )
    assert resp.status == "committed"
    assert resp.commit_sha
    assert resp.push_state == "enqueue_failed_recovery_pending"
    assert pen.size() == 0  # entry consumed (commit exists; recovery republishes)


def test_cas_marker_with_refresh_failure_rejects_stale_base(tmp_path, monkeypatch) -> None:
    """Codex round-5: when the caller supplied an enforceable base marker but the
    worktree base cannot be refreshed onto origin/main, CAS cannot be verified, so
    the write is rejected rejected_stale_base instead of committing against a
    possibly-stale base."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    target, blob = _seed_t1_file(repo)

    # Force refresh_base to fail (e.g. network/fetch error) for this registry.
    def boom(_wt, **_kw):
        raise RuntimeError("fetch_failed: origin unreachable")
    monkeypatch.setattr(reg.git, "refresh_base", boom)

    resp = kb_propose_edit_fn(
        target_path=target, postimage="new body\n", base_commit="HEAD",
        base_blob_sha=blob,  # enforceable marker -> refresh failure is fatal
        target_file_hash=None, reason="fix", source_session="s",
        agent_identity="claude", confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    assert resp.status == "rejected_stale_base"
    assert "refreshed" in (resp.reason or "")
    assert pq.size() == 0


def test_no_marker_with_refresh_failure_still_commits(tmp_path, monkeypatch) -> None:
    """A refresh failure with NO base marker (CAS is a no-op) stays non-fatal: the
    commit sits on the unrefreshed base and the push path's non-FF recovery
    publishes it."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)

    def boom(_wt, **_kw):
        raise RuntimeError("fetch_failed")
    monkeypatch.setattr(reg.git, "refresh_base", boom)

    resp = kb_propose_memory_fn(
        text="note", tags=[], source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )
    assert resp.status == "committed"
    assert pq.size() == 1


# ---------------------------------------------------------------------------
# issue #109: `evidence` on kb_propose_memory / kb_propose_edit
# ---------------------------------------------------------------------------


def test_propose_memory_evidence_surfaces_via_pending(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="lowconf", tags=[], source_session="session-abc",
        agent_identity="claude", confidence=0.4, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="10.0.0.1",
        evidence=["saw it in the logs", "confirmed with operator"],
    )
    assert resp.status == "pending_confirmation"
    listed = kb_list_pending_fn(pending=pen)
    entry = next(e for e in listed.pending if e.pending_id == resp.pending_id)
    assert entry.evidence == ["saw it in the logs", "confirmed with operator"]
    assert entry.source_session == "session-abc"


def test_propose_memory_evidence_rejects_too_many_items(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="x", tags=[], source_session="s", agent_identity="claude",
        confidence=0.9, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        evidence=[f"item {i}" for i in range(11)],
    )
    assert resp.status == "rejected_invalid_evidence"
    assert pq.size() == 0
    assert pen.size() == 0


def test_propose_memory_evidence_rejects_oversized_item(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="x", tags=[], source_session="s", agent_identity="claude",
        confidence=0.9, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        evidence=["y" * 501],
    )
    assert resp.status == "rejected_invalid_evidence"


def test_propose_memory_evidence_rejects_non_list_outer_type(tmp_path) -> None:
    """Codex review blocker: REST passes raw JSON `evidence` through, and a
    plain string is iterable (each char is a 1-char str), so without an outer
    isinstance check a JSON string of <= 10 chars would silently pass item
    validation. The outer type must be a real list."""
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    for bad in ("abcd", {"a": "b"}, 42):
        resp = kb_propose_memory_fn(
            text="x", tags=[], source_session="s", agent_identity="claude",
            confidence=0.9, confidence_threshold=0.85, worktrees=reg,
            push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
            remote_addr="1.2.3.4",
            evidence=bad,  # type: ignore[arg-type]
        )
        assert resp.status == "rejected_invalid_evidence", bad
    assert pq.size() == 0
    assert pen.size() == 0


def test_propose_memory_evidence_rejects_non_string_item(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="x", tags=[], source_session="s", agent_identity="claude",
        confidence=0.9, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        evidence=["ok", 42],  # type: ignore[list-item]
    )
    assert resp.status == "rejected_invalid_evidence"


def test_propose_memory_evidence_rejects_falsy_non_list_types(tmp_path) -> None:
    """Codex re-review blocker: `evidence = evidence or []` coerced FALSY
    non-list values ('' / {} / False / 0) to [] before validation, silently
    accepting them. Only None (the "not supplied" sentinel) may normalize to
    []; every other non-list value must reject."""
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    for bad in ("", {}, False, 0):
        resp = kb_propose_memory_fn(
            text="x", tags=[], source_session="s", agent_identity="claude",
            confidence=0.9, confidence_threshold=0.85, worktrees=reg,
            push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
            remote_addr="1.2.3.4",
            evidence=bad,  # type: ignore[arg-type]
        )
        assert resp.status == "rejected_invalid_evidence", repr(bad)
    assert pq.size() == 0
    assert pen.size() == 0


def test_propose_edit_evidence_rejects_falsy_non_list_types(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    target, blob = _seed_t1_file(repo)
    for bad in ("", {}, False, 0):
        resp = kb_propose_edit_fn(
            target_path=target, postimage="new body\n", base_commit="HEAD",
            base_blob_sha=blob, target_file_hash=None, reason="x",
            source_session="s", agent_identity="claude",
            confidence=0.9, confidence_threshold=0.85,
            worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl,
            blocklist=bl, remote_addr="1.2.3.4",
            evidence=bad,  # type: ignore[arg-type]
        )
        assert resp.status == "rejected_invalid_evidence", repr(bad)


def test_propose_edit_evidence_rejects_non_list_outer_type(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    target, blob = _seed_t1_file(repo)
    resp = kb_propose_edit_fn(
        target_path=target, postimage="new body\n", base_commit="HEAD",
        base_blob_sha=blob, target_file_hash=None, reason="x",
        source_session="s", agent_identity="claude",
        confidence=0.9, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
        evidence="not-a-list",  # type: ignore[arg-type]
    )
    assert resp.status == "rejected_invalid_evidence"


def test_propose_memory_evidence_within_limits_accepted(tmp_path, monkeypatch) -> None:
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="note", tags=[], source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        evidence=[f"item {i}" for i in range(9)] + ["z" * 500],
    )
    assert resp.status == "committed"


def test_propose_memory_secret_in_evidence_rejected_redacted(tmp_path, monkeypatch) -> None:
    """Evidence is rendered into the frontmatter of the postimage (issue #109),
    so a secret-shaped evidence string passes through the SAME full-postimage
    secret scan the propose path already runs -- no separate scan is needed.
    The rejection must redact (pattern name only), never echo the raw secret."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="note", tags=[], source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
        evidence=["AKIAABCDEFGHIJKLMNOP"],
    )
    assert resp.status == "rejected_secret_detected"
    assert resp.matching_pattern
    assert "AKIAABCDEFGHIJKLMNOP" not in (resp.reason or "")
    assert pq.size() == 0


def test_propose_edit_evidence_surfaces_via_pending(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    target, blob = _seed_t1_file(repo)
    resp = kb_propose_edit_fn(
        target_path=target, postimage="new body\n", base_commit="HEAD",
        base_blob_sha=blob, target_file_hash=None, reason="lowconf",
        source_session="s", agent_identity="claude",
        confidence=0.5, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
        evidence=["checked the runbook"],
    )
    assert resp.status == "pending_confirmation"
    listed = kb_list_pending_fn(pending=pen)
    entry = next(e for e in listed.pending if e.pending_id == resp.pending_id)
    assert entry.evidence == ["checked the runbook"]
    assert entry.reason == "lowconf"


def test_propose_edit_evidence_rejects_too_many_items(tmp_path) -> None:
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    repo = git._repo
    target, blob = _seed_t1_file(repo)
    resp = kb_propose_edit_fn(
        target_path=target, postimage="new body\n", base_commit="HEAD",
        base_blob_sha=blob, target_file_hash=None, reason="x",
        source_session="s", agent_identity="claude",
        confidence=0.9, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
        evidence=[f"item {i}" for i in range(11)],
    )
    assert resp.status == "rejected_invalid_evidence"
