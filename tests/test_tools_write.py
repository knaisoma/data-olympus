"""Tests for the 4 write MCP tool functions."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

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
        # A live index so the issue #112 governed-target lookup resolves
        # definitively (fail-closed would otherwise demote to pending before
        # this test's subject -- the symlink containment gate -- is reached).
        idx=_build_index(repo),
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
        # A live index so the issue #112 governed-target lookup resolves
        # definitively: the seeded doc carries no status, so it is NOT in
        # force and the edit auto-commits as before (fail-closed would
        # otherwise demote it as governed_target_unverified).
        idx=_build_index(repo),
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


# ---- issue #173: OKF v0.2 provenance on server-rendered memories ----


def test_render_memory_records_the_tool_as_generator_at_created_at() -> None:
    """`generated.by` is the data-olympus tool actor and `generated.at` is the
    same instant as `created_at`. The memory carries no legacy timestamp."""
    import yaml

    from data_olympus import __version__
    from data_olympus.tools_write import _render_memory
    out = _render_memory(text="body", tags=[], agent_identity="claude")
    fm = yaml.safe_load(out.split("---\n", 2)[1])
    assert isinstance(fm["created_at"], str)
    assert fm["generated"] == {"by": f"data-olympus/{__version__}", "at": fm["created_at"]}
    assert "timestamp" not in fm
    assert fm["created_by"] == "claude"


def test_render_memory_human_identity_never_becomes_the_generator() -> None:
    """A principal named like a human actor stays the proposer; it is never
    recorded as the author of agent-written content."""
    import yaml

    from data_olympus import __version__
    from data_olympus.tools_write import _render_memory
    out = _render_memory(text="body", tags=[], agent_identity="human:alice")
    fm = yaml.safe_load(out.split("---\n", 2)[1])
    assert fm["created_by"] == "human:alice"
    assert fm["generated"]["by"] == f"data-olympus/{__version__}"


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
    live corpus to check duplicate ids against.

    ``status_autofill=False`` pins the pre-#147 in-force semantics these tests
    rely on: several seed a status-LESS doc precisely so it is NOT in force (so a
    governed-target lookup treats it as non-governed and the edit auto-commits).
    Virtual autofill (#147, default on) would otherwise index that doc as
    ``active``/in-force and change the governance decision. These tests exercise
    the write/governance path, not the autofill feature, so the conservative
    lane is the faithful choice here."""
    import tempfile

    from data_olympus.index import Index
    idx = Index(Path(tempfile.mkdtemp()) / "index.db", status_autofill=False)
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
        # Live index: the status-less seeded doc is definitively not in
        # force, so the issue #112 fail-closed lookup does not demote before
        # this test's subject (CAS) is reached.
        idx=_build_index(repo),
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
        # Live index: see test_propose_edit_stale_base_rejected.
        idx=_build_index(repo),
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


def test_propose_edit_new_document_missing_status_rejected(tmp_path, monkeypatch) -> None:
    """Issue #114 write-path migration: a NEW document created via
    kb_propose_edit without `status` is rejected -- status has always been a
    required field (SPEC.md 4.2) and a `kb lint` error, but the write path
    previously let a brand-new status-less document through. Editing an
    EXISTING status-less document (see test_kb_propose_edit_high_conf_commits,
    whose seeded doc has no status/type at all) remains allowed so operators
    can migrate a legacy corpus incrementally."""
    _set_git_env(monkeypatch)
    git, reg, pq, pen, rl, bl = _state(tmp_path)
    idx = _build_index(git._repo)
    new_doc = "---\nid: NEW-1\ntype: standard\ntier: T1\n---\nbody\n"
    resp = kb_propose_edit_fn(
        target_path="universal/foundation/NEW-1.md", postimage=new_doc,
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        reason="", source_session="s", agent_identity="claude", confidence=0.95,
        confidence_threshold=0.85, worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4", idx=idx,
    )
    assert resp.status == "rejected_invalid_document"
    assert "status" in (resp.reason or "").lower()
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
    idx = _build_index(repo)

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
        # Live index: see test_propose_edit_stale_base_rejected.
        idx=idx,
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


def test_list_pending_labels_claimed_entries(tmp_path) -> None:  # noqa: ANN001
    """kb_list_pending must show a claimed entry rather than reading empty.

    Issue #254: after a transport drop mid-resolve the queue read as empty,
    which is indistinguishable from 'the decision was applied'. The operator
    concluded the write had landed; it had not.
    """
    from data_olympus.pending import PendingQueue
    from data_olympus.tools_write import kb_list_pending_fn

    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pending_id = q.enqueue(
        proposal_type="edit", target_path="operator/notes.md", postimage="body",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        meta={"confidence": 0.4},
    )
    assert [e.state for e in kb_list_pending_fn(pending=q).pending] == ["pending"]

    q.claim_for_resolve(pending_id)
    entries = kb_list_pending_fn(pending=q).pending

    assert [e.pending_id for e in entries] == [pending_id]
    assert entries[0].state == "claimed"


def test_a_signal_killed_commit_is_unknown_not_a_failure() -> None:
    """git can update the ref and then be killed before it exits. subprocess
    reports that as CalledProcessError, and treating it as a failed commit would
    record a proven non-commit for a write that may have landed."""
    import subprocess

    from data_olympus.tools_write import _classify_commit_error

    killed = subprocess.CalledProcessError(-9, ["git", "commit"])
    declined = subprocess.CalledProcessError(1, ["git", "commit"])

    assert _classify_commit_error(killed) == "unknown"
    assert _classify_commit_error(declined) == "failed"


# --- reading back your own parked proposal (issue #256) ----------------------


def _parked(tmp_path, *, flagged: bool = False):  # noqa: ANN001, ANN202
    from data_olympus.pending import PendingQueue

    q = PendingQueue(pending_root=str(tmp_path / "p"))
    meta = {"confidence": 0.4, "source_session": "session-A",
            "proposer_principal": "proposer"}
    if flagged:
        meta["secret_scan_flagged"] = True
        meta["matching_pattern"] = "google_api_key"
    pid = q.enqueue(
        proposal_type="edit", target_path="operator/notes.md",
        postimage="the draft body", base_commit="HEAD", base_blob_sha=None,
        target_file_hash=None, meta=meta,
    )
    return q, pid


def test_the_proposing_principal_can_read_its_own_draft(tmp_path) -> None:
    """The gap in issue #256: a session could learn THAT it proposed something
    about a path, never WHAT it proposed, so it could not quote its own draft
    back or check it against a second proposal."""
    from data_olympus.tools_write import kb_get_pending_fn

    q, pid = _parked(tmp_path)
    resp = kb_get_pending_fn(
        pending=q, pending_id=pid, principal_name="proposer",
        can_resolve=False,
    )

    assert resp.status == "ok"
    assert resp.postimage == "the draft body"
    assert resp.target_path == "operator/notes.md"
    # It must say plainly that this content does not govern.
    assert resp.in_force is False
    assert "not in force" in resp.note.lower()


def test_another_principal_cannot_read_the_draft(tmp_path) -> None:
    """Otherwise this is a side channel for reading queued content. Scoping on
    the caller-supplied session did exactly that: the listing publishes
    source_session, so a reader could copy it and ask for the draft."""
    from data_olympus.tools_write import kb_get_pending_fn

    q, pid = _parked(tmp_path)
    resp = kb_get_pending_fn(
        pending=q, pending_id=pid, principal_name="reader",
        can_resolve=False,
    )

    assert resp.status == "forbidden"
    assert resp.postimage is None


def test_a_resolver_can_read_any_draft(tmp_path) -> None:
    """A principal that could approve the entry can already see the content by
    approving it, so withholding it from them protects nothing."""
    from data_olympus.tools_write import kb_get_pending_fn

    q, pid = _parked(tmp_path)
    resp = kb_get_pending_fn(
        pending=q, pending_id=pid, principal_name="reader", can_resolve=True,
    )

    assert resp.status == "ok"
    assert resp.postimage == "the draft body"


def test_a_secret_flagged_draft_is_withheld_from_its_own_proposer(tmp_path) -> None:
    """A postimage the scanner flagged contains credential-shaped content. The
    proposing session already had it, but reading it BACK through the service
    turns the queue into a place to retrieve one, so only a principal that could
    approve it gets it back."""
    from data_olympus.tools_write import kb_get_pending_fn

    q, pid = _parked(tmp_path, flagged=True)

    own = kb_get_pending_fn(
        pending=q, pending_id=pid, principal_name="proposer", can_resolve=False,
    )
    assert own.status == "forbidden_secret_flagged"
    assert own.postimage is None
    assert own.matching_pattern == "google_api_key"

    resolver = kb_get_pending_fn(
        pending=q, pending_id=pid, principal_name="proposer", can_resolve=True,
    )
    assert resolver.status == "ok"
    assert resolver.postimage == "the draft body"


def test_an_unknown_pending_id_is_not_found(tmp_path) -> None:
    from data_olympus.tools_write import kb_get_pending_fn

    q, _pid = _parked(tmp_path)
    resp = kb_get_pending_fn(
        pending=q, pending_id="0" * 32, principal_name="proposer",
        can_resolve=True,
    )
    assert resp.status == "not_found"
    assert resp.postimage is None


def test_a_traversal_shaped_id_is_rejected_without_touching_disk(tmp_path) -> None:
    from data_olympus.tools_write import kb_get_pending_fn

    q, _pid = _parked(tmp_path)
    resp = kb_get_pending_fn(
        pending=q, pending_id="../../etc/passwd", principal_name="proposer",
        can_resolve=True,
    )
    assert resp.status == "not_found"


def test_an_entry_with_no_recorded_proposer_is_resolver_only(tmp_path) -> None:
    """An entry parked before ownership was recorded cannot have its owner
    established, so it is resolver-only rather than readable by anyone who
    happens to send an empty principal name."""
    from data_olympus.pending import PendingQueue
    from data_olympus.tools_write import kb_get_pending_fn

    q = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = q.enqueue(
        proposal_type="edit", target_path="operator/notes.md",
        postimage="legacy draft", base_commit="HEAD", base_blob_sha=None,
        target_file_hash=None, meta={"confidence": 0.4},
    )

    assert kb_get_pending_fn(
        pending=q, pending_id=pid, principal_name="", can_resolve=False,
    ).status == "forbidden"
    assert kb_get_pending_fn(
        pending=q, pending_id=pid, principal_name="anyone", can_resolve=False,
    ).status == "forbidden"
    assert kb_get_pending_fn(
        pending=q, pending_id=pid, principal_name="op", can_resolve=True,
    ).postimage == "legacy draft"


_GOOD_BLOB = "a" * 40
_GOOD_FILE_HASH = "b" * 64


def _propose_edit_with_markers(state, target, *, blob, file_hash, confidence=0.5):
    git, reg, pq, pen, rl, bl = state
    return kb_propose_edit_fn(
        target_path=target, postimage="new body\n", base_commit="HEAD",
        base_blob_sha=blob, target_file_hash=file_hash, reason="markers",
        source_session="s", agent_identity="claude", confidence=confidence,
        confidence_threshold=0.85, worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=rl, blocklist=bl, remote_addr="1.2.3.4",
    )


@pytest.mark.parametrize(("blob", "file_hash", "field"), [
    ("a" * 64, None, "base_blob_sha"),            # sha256-length blob id
    ("A" * 40, None, "base_blob_sha"),            # uppercase
    (" " + "a" * 39, None, "base_blob_sha"),      # whitespace
    ("g" * 40, None, "base_blob_sha"),            # non-hex
    ("a" * 39, None, "base_blob_sha"),            # short
    (None, "a" * 40, "target_file_hash"),         # blob sha in the file-hash field (#263)
    (None, "B" * 64, "target_file_hash"),         # uppercase
    (None, "b" * 63, "target_file_hash"),         # short
    (None, "b" * 64 + "\n", "target_file_hash"),  # trailing newline
])
def test_propose_edit_rejects_malformed_base_markers(tmp_path, blob, file_hash, field) -> None:
    state = _state(tmp_path)
    target, _real_blob = _seed_t1_file(state[0]._repo)

    resp = _propose_edit_with_markers(state, target, blob=blob, file_hash=file_hash)

    assert resp.status == "rejected_invalid_base"
    assert field in (resp.reason or "")
    submitted = blob if field == "base_blob_sha" else file_hash
    assert submitted.strip() not in (resp.reason or "")
    assert state[3].size() == 0, "nothing may be parked"
    assert state[3].locks_held() == 0, "no path lock may be taken"


@pytest.mark.parametrize(("blob", "file_hash"), [(None, None), ("", ""), ("", None)])
def test_propose_edit_accepts_absent_base_markers(tmp_path, blob, file_hash) -> None:
    state = _state(tmp_path)
    target, _real_blob = _seed_t1_file(state[0]._repo)

    resp = _propose_edit_with_markers(state, target, blob=blob, file_hash=file_hash)

    assert resp.status == "pending_confirmation"


def test_validate_base_markers_rejects_non_strings() -> None:
    from data_olympus.tools_write import _validate_base_markers

    assert "base_blob_sha" in (_validate_base_markers(123, None) or "")
    assert "target_file_hash" in (_validate_base_markers(None, ["x"]) or "")
    assert _validate_base_markers(_GOOD_BLOB, _GOOD_FILE_HASH) is None


def test_correct_marker_pair_proposes_and_resolves(tmp_path, monkeypatch) -> None:
    """End to end: the correct blob id and sha256 for the current file park and
    then commit. This is the case #263 could never reach."""
    _set_git_env(monkeypatch)
    state = _state(tmp_path)
    git, reg, pq, pen, rl, bl = state
    repo = git._repo
    target, blob = _seed_t1_file(repo)
    file_hash = hashlib.sha256((repo / target).read_bytes()).hexdigest()

    parked = _propose_edit_with_markers(state, target, blob=blob, file_hash=file_hash)
    assert parked.status == "pending_confirmation"

    resolved = kb_resolve_pending_fn(
        pending_id=parked.pending_id, decision="approve", edited_text=None,
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="operator",
    )
    assert resolved.status == "committed", resolved.reason


def test_propose_edit_rejects_a_new_unresolved_supersedes_target(tmp_path, monkeypatch) -> None:
    """#259 on the auto-commit path: a new document whose supersedes names a
    missing id is refused with the code in the reason and nothing committed."""
    _set_git_env(monkeypatch)
    state = _state(tmp_path)
    git, reg, pq, pen, rl, bl = state
    repo = git._repo
    target = "projects/demo/new-decision.md"
    postimage = ("---\nid: DEMO-NEW\ntype: decision\nstatus: draft\ntier: T3\n"
                 "supersedes: GHOST\n---\n# New\n")

    resp = kb_propose_edit_fn(
        target_path=target, postimage=postimage, base_commit="HEAD",
        base_blob_sha=None, target_file_hash=None, reason="t", source_session="s",
        agent_identity="claude", confidence=0.95, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4", idx=_build_index(repo),
    )

    assert resp.status == "rejected_invalid_document", (resp.status, resp.reason)
    assert "unresolved_supersedes_target" in (resp.reason or "")
    assert "GHOST" in (resp.reason or "")
    assert pq.size() == 0


def test_resolve_refuses_a_pending_entry_introducing_a_dangling_target(
    tmp_path, monkeypatch,
) -> None:
    _set_git_env(monkeypatch)
    state = _state(tmp_path)
    git, reg, pq, pen, rl, bl = state
    target, _blob = _seed_t1_file(git._repo)
    postimage = "---\nid: STD-U-001\ntier: T1\nsupersedes: GHOST\n---\n# T1\nbody\n"
    parked = kb_propose_edit_fn(
        target_path=target, postimage=postimage, base_commit="HEAD",
        base_blob_sha=None, target_file_hash=None, reason="t", source_session="s",
        agent_identity="claude", confidence=0.3, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="1.2.3.4",
    )
    assert parked.status == "pending_confirmation"

    resolved = kb_resolve_pending_fn(
        pending_id=parked.pending_id, decision="approve", edited_text=None,
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="s", agent_identity="operator",
    )

    assert resolved.status == "rejected_invalid_document"
    assert "unresolved_supersedes_target" in (resolved.reason or "")
    assert [e["state"] for e in pen.list()] == ["pending"]
    assert pen.locks_held() == 1


# ---- issue #141: capture provenance envelope on proposed memories ----

_CAP_TEXT = "Prefer the staging cluster for load tests."
_CAP_TEXT_HASH = "sha256:" + hashlib.sha256(_CAP_TEXT.encode("utf-8")).hexdigest()


def _capture(**overrides):  # noqa: ANN003, ANN202
    env = {
        "capture_source": "codex.transcript",
        "capture_event_id": "urn:evt:7f3a",
        "source_event_hash": "sha256:" + "b" * 64,
        "transformation": "automem.distill/1.4.2",
        "raw_retention": "redacted",
        "capture_session": "sess-0042",
        "classification": "decision",
    }
    env.update(overrides)
    return env


def _stored_capture(**overrides):  # noqa: ANN003, ANN202
    return {**_capture(**overrides), "derived_memory_hash": _CAP_TEXT_HASH}


def _propose_capture(state, *, confidence, capture, audit_log=None, **kw):  # noqa: ANN001, ANN003, ANN202
    _git, reg, pq, pen, rl, bl = state
    return kb_propose_memory_fn(
        text=kw.pop("text", _CAP_TEXT), tags=[], source_session="session-cap",
        agent_identity="claude", confidence=confidence, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen, rate_limiter=rl, blocklist=bl,
        remote_addr="10.0.0.1", audit_log=audit_log, capture=capture, **kw,
    )


def _committed_file(git, sha: str) -> tuple[str, str]:  # noqa: ANN001
    repo = str(git._repo)
    names = subprocess.check_output(
        ["git", "-C", repo, "show", "--name-only", "--format=", sha], text=True,
    ).split()
    assert len(names) == 1, names
    body = subprocess.check_output(
        ["git", "-C", repo, "show", f"{sha}:{names[0]}"], text=True,
    )
    return names[0], body


def _frontmatter(text: str) -> dict:
    from data_olympus.format.frontmatter import parse_frontmatter
    fm, _body = parse_frontmatter(text)
    return fm


class _FrozenDateTime:
    @staticmethod
    def now(tz=None):  # noqa: ANN001, ANN205
        import datetime as real
        return real.datetime(2026, 10, 1, 12, 0, 0, tzinfo=tz)


# 1: absent or null capture is byte-identical to a memory without the feature.
@pytest.mark.parametrize("supplied", [False, True], ids=["absent", "null"])
def test_capture_absent_or_null_keeps_the_postimage_byte_identical(
    tmp_path, monkeypatch, supplied,
) -> None:
    import datetime as real
    import types

    from data_olympus import __version__
    from data_olympus import tools_write as tw

    monkeypatch.setattr(tw, "datetime", types.SimpleNamespace(
        datetime=_FrozenDateTime, UTC=real.UTC))
    state = _state(tmp_path)
    _git, reg, pq, pen, rl, bl = state
    kwargs = {"capture": None} if supplied else {}
    resp = kb_propose_memory_fn(
        text="body", tags=[], source_session="s", agent_identity="claude",
        confidence=0.4, confidence_threshold=0.85, worktrees=reg, push_queue=pq,
        pending=pen, rate_limiter=rl, blocklist=bl, remote_addr="10.0.0.1",
        **kwargs,
    )
    assert resp.status == "pending_confirmation"
    entry = pen.get(resp.pending_id)
    stamp = "2026-10-01T12:00:00+00:00"
    assert entry["postimage"] == (
        "---\n"
        "type: memory\n"
        "status: proposed\n"
        "created_by: claude\n"
        f"created_at: '{stamp}'\n"
        "generated:\n"
        f"  by: data-olympus/{__version__}\n"
        f"  at: '{stamp}'\n"
        "---\n\nbody\n"
    )
    assert "capture" not in entry["meta"]
    [listed] = kb_list_pending_fn(pending=pen).pending
    assert listed.capture is None


# 2: a valid envelope at low confidence reaches pending meta and the listing,
# with the server-computed derived_memory_hash.
def test_capture_low_confidence_is_stored_and_listed(tmp_path) -> None:
    state = _state(tmp_path)
    pen = state[3]
    resp = _propose_capture(state, confidence=0.4, capture=_capture())
    assert resp.status == "pending_confirmation"

    entry = pen.get(resp.pending_id)
    assert entry["meta"]["capture"] == _stored_capture()
    assert _frontmatter(entry["postimage"])["capture"] == _stored_capture()
    # Projection #1: PendingQueue.list().
    [projected] = pen.list()
    assert projected["capture"] == _stored_capture()
    # Projection #2: the kb_list_pending model the REST and MCP listings serve.
    [listed] = kb_list_pending_fn(pending=pen).pending
    assert listed.capture is not None
    assert listed.capture.model_dump() == _stored_capture()


def test_capture_does_not_force_pending(tmp_path, monkeypatch) -> None:
    """Operator decision for 0.11.0: the envelope is a label only."""
    _set_git_env(monkeypatch)
    state = _state(tmp_path)
    resp = _propose_capture(state, confidence=0.95, capture=_capture())
    assert resp.status == "committed"
    assert state[3].size() == 0


def test_list_pending_tolerates_a_malformed_stored_capture(tmp_path) -> None:
    """Review item 2: kb_list_pending_fn builds PendingEntry directly, so a
    record whose capture fails the model must read as None there too."""
    pen = PendingQueue(pending_root=str(tmp_path / "p"))
    pen.enqueue(
        proposal_type="memory", target_path="memory/inbox/a.md", postimage="x",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        meta={"capture": {"capture_source": ["not", "a", "string"]}},
    )
    pen.enqueue(
        proposal_type="memory", target_path="memory/inbox/b.md", postimage="y",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        meta={"capture": _stored_capture()},
    )
    listed = {e.target_path: e.capture for e in kb_list_pending_fn(pending=pen).pending}
    assert listed["memory/inbox/a.md"] is None
    assert listed["memory/inbox/b.md"] is not None


def test_list_pending_model_guard_covers_a_projection_it_cannot_trust(
    tmp_path, monkeypatch,
) -> None:
    """Even a projection that hands back a malformed value cannot take the
    listing down: kb_list_pending_fn re-checks before building the model."""
    pen = PendingQueue(pending_root=str(tmp_path / "p"))
    pen.enqueue(
        proposal_type="memory", target_path="memory/inbox/a.md", postimage="x",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None, meta={},
    )
    real_list = pen.list

    def _tampered():  # noqa: ANN202
        return [{**e, "capture": {"capture_source": 5}} for e in real_list()]

    monkeypatch.setattr(pen, "list", _tampered)
    [listed] = kb_list_pending_fn(pending=pen).pending
    assert listed.capture is None


# 4 (tool level) and review item 4: kb_get_pending_fn reads capture back.
def test_get_pending_returns_capture_on_ok_only(tmp_path) -> None:
    from data_olympus.tools_write import kb_get_pending_fn

    pen = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = pen.enqueue(
        proposal_type="memory", target_path="memory/inbox/a.md", postimage="x",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        meta={"proposer_principal": "proposer", "capture": _stored_capture()},
    )
    own = kb_get_pending_fn(pending=pen, pending_id=pid, principal_name="proposer",
                            can_resolve=False)
    assert own.status == "ok"
    assert own.capture is not None
    assert own.capture.model_dump() == _stored_capture()

    other = kb_get_pending_fn(pending=pen, pending_id=pid, principal_name="reader",
                              can_resolve=False)
    assert other.status == "forbidden"
    assert other.capture is None


@pytest.mark.parametrize("stored", [
    "a string", ["a", "list"], 42, {"capture_source": "x"},
    {**_stored_capture(), "unknown": "x"},
    {**_stored_capture(), "capture_event_id": {"nested": "x"}},
], ids=["string", "list", "number", "partial", "unknown_key", "nested"])
def test_get_pending_reads_malformed_legacy_capture_as_absent(tmp_path, stored) -> None:
    from data_olympus.tools_write import kb_get_pending_fn

    pen = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = pen.enqueue(
        proposal_type="memory", target_path="memory/inbox/a.md", postimage="x",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None,
        meta={"proposer_principal": "proposer", "capture": stored},
    )
    resp = kb_get_pending_fn(pending=pen, pending_id=pid, principal_name="proposer",
                             can_resolve=True)
    assert resp.status == "ok"
    assert resp.capture is None


def test_get_pending_survives_a_non_mapping_meta(tmp_path) -> None:
    import json as _json

    from data_olympus.tools_write import kb_get_pending_fn

    pen = PendingQueue(pending_root=str(tmp_path / "p"))
    pid = pen.enqueue(
        proposal_type="memory", target_path="memory/inbox/a.md", postimage="x",
        base_commit="HEAD", base_blob_sha=None, target_file_hash=None, meta={},
    )
    path = tmp_path / "p" / f"{pid}.json"
    record = _json.loads(path.read_text())
    record["meta"] = ["not", "a", "mapping"]
    path.write_text(_json.dumps(record))
    resp = kb_get_pending_fn(pending=pen, pending_id=pid, principal_name="any",
                             can_resolve=True)
    assert resp.status == "ok"
    assert resp.capture is None


# 13: audit carries the envelope on committed and pending events only.
def test_capture_audit_on_committed_and_pending_not_on_rejection(
    tmp_path, monkeypatch,
) -> None:
    import json as _json

    from data_olympus.audit_log import AuditLog

    _set_git_env(monkeypatch)
    state = _state(tmp_path)
    audit = AuditLog(log_path=str(tmp_path / "audit.log"), hmac_key="")
    assert _propose_capture(state, confidence=0.95, capture=_capture(),
                            audit_log=audit).status == "committed"
    assert _propose_capture(state, confidence=0.4, capture=_capture(),
                            audit_log=audit, text="another memory").status \
        == "pending_confirmation"
    assert _propose_capture(state, confidence=0.4,
                            capture=_capture(raw_retention="forever"),
                            audit_log=audit).status == "rejected_invalid_capture"
    assert _propose_capture(state, confidence=0.4, capture=None,
                            audit_log=audit, text="plain").status \
        == "pending_confirmation"

    events = [_json.loads(x) for x in (tmp_path / "audit.log").read_text().splitlines()]
    by_status = [(e["status"], e.get("capture")) for e in events]
    assert by_status[0] == ("committed", _stored_capture())
    assert by_status[1][0] == "pending_confirmation"
    assert by_status[1][1]["derived_memory_hash"] == (
        "sha256:" + hashlib.sha256(b"another memory").hexdigest())
    assert by_status[2] == ("rejected_invalid_capture", None)
    assert "capture" not in events[2]
    assert "forever" not in (tmp_path / "audit.log").read_text()
    # An event without an envelope keeps exactly its old shape.
    assert "capture" not in events[3]
    assert audit.verify() == (True, -1)


# 16: the rendered capture block counts against max_text_bytes.
def test_capture_block_counts_against_max_text_bytes(tmp_path) -> None:
    from data_olympus.capture import validate_capture
    from data_olympus.tools_write import _render_memory

    envelope = validate_capture(_capture(), text=_CAP_TEXT).envelope
    assert envelope is not None
    plain = len(_render_memory(text=_CAP_TEXT, tags=[], agent_identity="claude")
                .encode("utf-8"))
    with_capture = len(_render_memory(text=_CAP_TEXT, tags=[], agent_identity="claude",
                                      capture=envelope).encode("utf-8"))
    assert with_capture > plain
    # A cap that fits the plain memory but not the labelled one rejects it.
    cap = with_capture - 1
    for sub in ("a", "b", "c"):
        (tmp_path / sub).mkdir()
    assert _propose_capture(_state(tmp_path / "a"), confidence=0.4, capture=None,
                            max_text_bytes=cap).status == "pending_confirmation"
    assert _propose_capture(_state(tmp_path / "b"), confidence=0.4, capture=_capture(),
                            max_text_bytes=cap).status == "rejected_payload_too_large"
    # Generous enough for both: the clock-dependent timestamp is fixed width.
    assert _propose_capture(_state(tmp_path / "c"), confidence=0.4, capture=_capture(),
                            max_text_bytes=with_capture + 8).status \
        == "pending_confirmation"


# Review item 1: the label survives resolve, on approve AND on edit.
def _park_capture(tmp_path):  # noqa: ANN001, ANN202
    state = _state(tmp_path)
    resp = _propose_capture(state, confidence=0.4, capture=_capture())
    assert resp.status == "pending_confirmation"
    return state, resp.pending_id


def _resolve(state, pid, edited_text, decision="approve"):  # noqa: ANN001, ANN202
    _git, reg, pq, pen, _rl, _bl = state
    return kb_resolve_pending_fn(
        pending_id=pid, decision=decision, edited_text=edited_text,
        worktrees=reg, push_queue=pq, pending=pen,
        source_session="op", agent_identity="operator",
    )


def test_resolve_approve_commits_the_reviewed_bytes_with_capture(
    tmp_path, monkeypatch,
) -> None:
    _set_git_env(monkeypatch)
    state, pid = _park_capture(tmp_path)
    reviewed = state[3].get(pid)["postimage"]
    resp = _resolve(state, pid, None)
    assert resp.status == "committed"
    _path, committed = _committed_file(state[0], resp.commit_sha)
    assert committed == reviewed
    assert _frontmatter(committed)["capture"] == _stored_capture()


@pytest.mark.parametrize("decision", ["approve", "edit"])
def test_resolve_body_only_edit_reattaches_capture_and_keeps_the_hash(
    tmp_path, monkeypatch, decision,
) -> None:
    """The interactive edit flow hands the operator the body; edited_text then
    replaces the whole document. The stored envelope is re-attached with the
    ORIGINAL derived_memory_hash, so the mismatch with the committed body is
    the record that a human changed it."""
    _set_git_env(monkeypatch)
    state, pid = _park_capture(tmp_path)
    resp = _resolve(state, pid, "An operator-corrected body.", decision=decision)
    assert resp.status == "committed"
    _path, committed = _committed_file(state[0], resp.commit_sha)
    fm = _frontmatter(committed)
    assert fm["capture"] == _stored_capture()
    assert fm["status"] == "proposed"
    assert fm["type"] == "memory"
    assert committed.endswith("---\n\nAn operator-corrected body.\n")
    edited_hash = "sha256:" + hashlib.sha256(
        b"An operator-corrected body.").hexdigest()
    assert fm["capture"]["derived_memory_hash"] == _CAP_TEXT_HASH != edited_hash


def test_resolve_edit_with_frontmatter_cannot_drop_or_rewrite_capture(
    tmp_path, monkeypatch,
) -> None:
    _set_git_env(monkeypatch)
    state, pid = _park_capture(tmp_path)
    edited = ("---\ntype: memory\nstatus: proposed\ntags:\n- reviewed\n"
              "capture:\n  capture_source: rewritten\n---\n\nEdited body.\n")
    resp = _resolve(state, pid, edited, decision="edit")
    assert resp.status == "committed"
    _path, committed = _committed_file(state[0], resp.commit_sha)
    fm = _frontmatter(committed)
    assert fm["capture"] == _stored_capture()
    assert fm["tags"] == ["reviewed"]
    assert committed.endswith("---\n\nEdited body.\n")


def test_resolve_edit_dropping_the_frontmatter_block_still_keeps_capture(
    tmp_path, monkeypatch,
) -> None:
    _set_git_env(monkeypatch)
    state, pid = _park_capture(tmp_path)
    edited = "---\ntype: memory\nstatus: proposed\n---\n\nEdited, label removed.\n"
    resp = _resolve(state, pid, edited, decision="edit")
    assert resp.status == "committed"
    _path, committed = _committed_file(state[0], resp.commit_sha)
    assert _frontmatter(committed)["capture"] == _stored_capture()


def test_resolve_edit_without_capture_is_unchanged(tmp_path, monkeypatch) -> None:
    """No envelope, no re-attachment: edited_text is committed exactly as
    before this feature."""
    _set_git_env(monkeypatch)
    state = _state(tmp_path)
    parked = _propose_capture(state, confidence=0.4, capture=None)
    resp = _resolve(state, parked.pending_id, "edited body\n")
    assert resp.status == "committed"
    _path, committed = _committed_file(state[0], resp.commit_sha)
    assert committed == "edited body\n"


def test_resolve_edit_cap_counts_the_reattached_block(tmp_path, monkeypatch) -> None:
    """The cap and the secret scan judge the bytes that will be committed."""
    _set_git_env(monkeypatch)
    state, pid = _park_capture(tmp_path)
    _git, reg, pq, pen, _rl, _bl = state
    resp = kb_resolve_pending_fn(
        pending_id=pid, decision="approve", edited_text="short body",
        worktrees=reg, push_queue=pq, pending=pen, source_session="op",
        agent_identity="operator", max_postimage_bytes=100,
    )
    assert resp.status == "rejected_edited_text_too_large"
    assert pen.size() == 1
