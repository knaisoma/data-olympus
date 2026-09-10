"""Tests for git_ops module."""
from __future__ import annotations

import os
import pathlib
import subprocess
from typing import TYPE_CHECKING

import pytest

from data_olympus.git_ops import GitOps

if TYPE_CHECKING:
    from pathlib import Path


def test_head_sha_on_fresh_repo(tmp_git_kb: Path) -> None:
    git = GitOps(tmp_git_kb)
    sha = git.head_sha()
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_ff_merge_no_op_when_no_remote_change(tmp_git_kb: Path) -> None:
    git = GitOps(tmp_git_kb)
    before = git.head_sha()
    # No remote configured; ff_merge should be a no-op (or report no fetch source) without raising.
    result = git.ff_merge_origin_main(timeout_sec=10)
    assert result.previous_sha == before
    assert result.current_sha == before
    assert result.changed is False


def test_ff_merge_advances_after_local_remote_commit(tmp_git_kb: Path, tmp_path: Path) -> None:
    """Simulate a remote by setting origin to a sibling clone, adding a commit to remote, ff."""
    git = GitOps(tmp_git_kb)
    remote = tmp_path / "remote.git"
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "GIT_AUTHOR_NAME": "tester", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "tester", "GIT_COMMITTER_EMAIL": "t@example.com"}
    # Bare remote. --initial-branch=main so HEAD is main regardless of the
    # runner's init.defaultBranch (CI defaults to master without this).
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_git_kb), "remote", "add", "origin", str(remote)],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_git_kb), "push", "-u", "origin", "main"],
                   check=True, env=env)

    # Clone, add commit, push
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, env=env)
    (clone / "newfile.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(clone), "add", "newfile.md"], check=True, env=env)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "add new"], check=True, env=env)
    subprocess.run(["git", "-C", str(clone), "push", "origin", "main"], check=True, env=env)

    before = git.head_sha()
    result = git.ff_merge_origin_main(timeout_sec=10)
    assert result.previous_sha == before
    assert result.current_sha != before
    assert result.changed is True


def test_head_sha_missing_repo_raises(tmp_path: Path) -> None:
    git = GitOps(tmp_path / "no_such_dir")
    with pytest.raises(FileNotFoundError):
        git.head_sha()


def _git_env() -> dict[str, str]:
    return {**os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def test_worktree_add_and_remove_round_trip(tmp_path) -> None:
    # Set up a bare-ish repo with one commit.
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=_git_env())
    (repo / "a.md").write_text("x")
    subprocess.run(["git", "add", "a.md"], cwd=repo, check=True, env=_git_env())
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=_git_env())

    git = GitOps(repo)
    wt = tmp_path / "wt" / "session-abc"
    git.worktree_add(str(wt), branch="kb-session/abc")
    assert wt.is_dir()
    assert (wt / "a.md").exists()

    git.worktree_remove(str(wt), force=True)
    assert not wt.exists()


def test_worktree_add_idempotent_existing_wt_returns_existing(tmp_path) -> None:
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=_git_env())
    (repo / "a.md").write_text("x")
    subprocess.run(["git", "add", "a.md"], cwd=repo, check=True, env=_git_env())
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=_git_env())

    git = GitOps(repo)
    wt = tmp_path / "wt" / "session-abc"
    git.worktree_add(str(wt), branch="kb-session/abc")
    # Second call: must not raise; the worktree already exists.
    git.worktree_add(str(wt), branch="kb-session/abc")


def _seed_repo_with_commit(tmp_path):
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=_git_env())
    (repo / "a.md").write_text("x")
    subprocess.run(["git", "add", "a.md"], cwd=repo, check=True, env=_git_env())
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=_git_env())
    return repo


def test_push_times_out_and_raises_timeout_expired(tmp_path, monkeypatch) -> None:
    """A hanging origin must not block forever: push passes a timeout to
    subprocess and TimeoutExpired propagates (drain classifies it retryable)."""
    git = GitOps(_seed_repo_with_commit(tmp_path))
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        git.push("/tmp/whatever", timeout_sec=1)
    assert captured.get("timeout") == 1


def test_delete_branch_is_idempotent_and_removes_branch(tmp_path) -> None:
    repo = _seed_repo_with_commit(tmp_path)
    git = GitOps(repo)
    # Deleting a non-existent branch must not raise.
    git.delete_branch("kb-session/does-not-exist")
    # Create a branch, then delete it via the API.
    subprocess.run(["git", "-C", str(repo), "branch", "kb-session/abc"],
                   check=True, env=_git_env())
    assert git._branch_exists("kb-session/abc")
    git.delete_branch("kb-session/abc")
    assert not git._branch_exists("kb-session/abc")


def test_worktree_add_reuses_existing_branch(tmp_path) -> None:
    """Regression for the coupled GC bug: if the kb-session branch already
    exists (residual from a crashed GC), worktree_add must attach to it rather
    than fail with 'branch already exists'."""
    repo = _seed_repo_with_commit(tmp_path)
    git = GitOps(repo)
    # Pre-create the branch to simulate a leftover.
    subprocess.run(["git", "-C", str(repo), "branch", "kb-session/abc"],
                   check=True, env=_git_env())
    wt = tmp_path / "wt" / "session-abc"
    # Must not raise despite the pre-existing branch.
    git.worktree_add(str(wt), branch="kb-session/abc")
    assert wt.is_dir()


def test_list_unpushed_shas_finds_commits_not_on_origin_main(tmp_path) -> None:
    """A commit reachable from HEAD but not origin/main is listed; empty
    otherwise. Backs push-queue init-recovery."""
    # Bare remote + clone so origin/main exists.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, env=_git_env())
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(remote), str(work)], check=True, env=_git_env())
    (work / "a.md").write_text("x")
    subprocess.run(["git", "-C", str(work), "add", "a.md"], check=True, env=_git_env())
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], check=True, env=_git_env())
    subprocess.run(["git", "-C", str(work), "push", "origin", "main"], check=True, env=_git_env())

    git = GitOps(work)
    # After push, HEAD == origin/main => nothing unpushed.
    subprocess.run(["git", "-C", str(work), "fetch", "origin"], check=True, env=_git_env())
    assert git.list_unpushed_shas(str(work)) == []

    # New local commit, not pushed => it is unpushed.
    (work / "b.md").write_text("y")
    subprocess.run(["git", "-C", str(work), "add", "b.md"], check=True, env=_git_env())
    subprocess.run(["git", "-C", str(work), "commit", "-m", "orphan"], check=True, env=_git_env())
    unpushed = git.list_unpushed_shas(str(work))
    assert len(unpushed) == 1
    head = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True, env=_git_env()).stdout.strip()
    assert unpushed[0] == head


def test_normalize_remote_url_collapses_ssh_https():
    from data_olympus.git_ops import normalize_remote_url
    a = normalize_remote_url("git@github.com:org/repo.git")
    b = normalize_remote_url("https://github.com/org/repo")
    assert a == b


def test_normalize_remote_url_strips_trailing_slash_and_dotgit():
    from data_olympus.git_ops import normalize_remote_url
    a = normalize_remote_url("https://github.com/org/repo/")
    b = normalize_remote_url("https://github.com/org/repo.git")
    assert a == b


def test_get_remote_url_returns_none_if_no_remote(tmp_path):
    import subprocess

    from data_olympus.git_ops import get_remote_url
    repo = tmp_path / "norepo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    assert get_remote_url(str(repo)) is None


def test_find_claim_commit_finds_the_trailer(tmp_path) -> None:  # noqa: ANN001
    """Recovery asks git whether the claim-linked commit exists. Found is the
    only answer that proves the decision committed (issues #253, #254)."""
    import subprocess

    from data_olympus.git_ops import GitOps

    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}

    def run(*args: str) -> str:
        return subprocess.run(list(args), cwd=repo, check=True, env=env,
                              capture_output=True, text=True).stdout.strip()

    run("git", "init", "-q", "--initial-branch=main")
    (repo / "a.md").write_text("a\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "seed")
    before = run("git", "rev-parse", "HEAD")
    (repo / "b.md").write_text("b\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "resolve: b\n\nKB-Pending-Id: " + "c" * 32)

    git = GitOps(str(repo))
    assert git.find_claim_commit(
        ref="main", since_sha=before, pending_id="c" * 32) is True
    assert git.find_claim_commit(
        ref="main", since_sha=before, pending_id="d" * 32) is False


def test_find_claim_commit_cannot_search_returns_none(tmp_path) -> None:  # noqa: ANN001
    """A missing ref, a missing pre-write sha, or an unreadable repository is
    NOT absence. It is 'cannot tell', and the caller must treat it as uncertain
    rather than concluding nothing committed."""
    import subprocess

    from data_olympus.git_ops import GitOps

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    git = GitOps(str(repo))

    assert git.find_claim_commit(ref="", since_sha="x" * 40, pending_id="c" * 32) is None
    assert git.find_claim_commit(ref="main", since_sha="", pending_id="c" * 32) is None
    assert git.find_claim_commit(
        ref="no-such-branch", since_sha="x" * 40, pending_id="c" * 32) is None


def test_refresh_base_defers_while_a_claim_could_lose_its_commit(tmp_path) -> None:  # noqa: ANN001
    """A rebase can drop the trailer-bearing commit an interrupted resolve left
    behind, and then nothing can establish whether that decision committed. The
    rebase defers while such a claim is outstanding (issues #253, #254)."""
    import subprocess

    from data_olympus.git_ops import ClaimEvidenceAtRiskError, GitOps

    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=repo,
                   check=True, env=env)
    (repo / "a.md").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True, env=env)
    git = GitOps(str(repo), claim_guard=lambda _ref: ["a" * 32])

    try:
        git.refresh_base(str(repo))
    except ClaimEvidenceAtRiskError as exc:
        assert "a" * 32 in str(exc)
    else:
        raise AssertionError("the rebase must defer, not run")


def test_delete_branch_defers_while_a_claim_could_lose_its_commit(tmp_path) -> None:  # noqa: ANN001
    import subprocess

    from data_olympus.git_ops import ClaimEvidenceAtRiskError, GitOps

    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=repo,
                   check=True, env=env)
    (repo / "a.md").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "branch", "kb-session/x"], cwd=repo, check=True, env=env)

    git = GitOps(str(repo), claim_guard=lambda _ref: ["b" * 32])
    try:
        git.delete_branch("kb-session/x")
    except ClaimEvidenceAtRiskError:
        pass
    else:
        raise AssertionError("branch deletion must defer, not run")
    # The branch, and with it the evidence, is still there.
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
         "refs/heads/kb-session/x"], check=False, capture_output=True,
    ).returncode == 0


def test_no_claim_guard_means_no_deferral(tmp_path) -> None:  # noqa: ANN001
    """The guard is opt-in: a GitOps built without one behaves exactly as before."""
    import subprocess

    from data_olympus.git_ops import GitOps

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=repo,
                   check=True)
    # No origin remote, so refresh_base is a documented no-op returning "".
    assert GitOps(str(repo)).refresh_base(str(repo)) == ""


def test_find_claim_commit_rejects_forged_evidence(tmp_path) -> None:  # noqa: ANN001
    """Commit message content is agent-controlled, so a substring match is
    forgeable: an unrelated auto-commit whose body or filename contains the
    trailer text would falsely close an operator decision. Only a real trailer
    line, on a commit whose target path matches the claim, is evidence.
    """
    import subprocess

    from data_olympus.git_ops import GitOps

    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}

    def run(*args: str) -> str:
        return subprocess.run(list(args), cwd=repo, check=True, env=env,
                              capture_output=True, text=True).stdout.strip()

    pid = "c" * 32
    run("git", "init", "-q", "--initial-branch=main")
    (repo / "a.md").write_text("a\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "seed")
    before = run("git", "rev-parse", "HEAD")
    git = GitOps(str(repo))

    # Forgery 1: the text inside another trailer's VALUE, no newline needed.
    (repo / "b.md").write_text("b\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm",
        f"memory: note\n\nKB-Agent-Identity: user KB-Pending-Id: {pid}\n"
        f"KB-Target-Path: memory/inbox/n.md\n")
    assert git.find_claim_commit(
        ref="main", since_sha=before, pending_id=pid,
        target_path="operator/notes.md") is False

    # Forgery 2: the text as a path, which git prints in the log body of a
    # commit whose subject names the file.
    forged = repo / f"KB-Pending-Id: {pid}.md"
    forged.write_text("x\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", f"memory: add KB-Pending-Id: {pid}.md")
    assert git.find_claim_commit(
        ref="main", since_sha=before, pending_id=pid,
        target_path="operator/notes.md") is False

    # The genuine article: a real trailer line, on a commit for that target.
    (repo / "c.md").write_text("c\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm",
        f"resolve: operator/notes.md\n\nKB-Target-Path: operator/notes.md\n"
        f"KB-Pending-Id: {pid}\n")
    assert git.find_claim_commit(
        ref="main", since_sha=before, pending_id=pid,
        target_path="operator/notes.md") is True

    # ...but not for a different target: evidence is bound to the claim.
    assert git.find_claim_commit(
        ref="main", since_sha=before, pending_id=pid,
        target_path="operator/other.md") is False


def test_find_claim_commit_rejects_unicode_separator_forgery(tmp_path) -> None:  # noqa: ANN001
    """Python's splitlines() breaks on U+2028, U+2029 and U+0085; git does not,
    and neither does its trailer parser. A target path carrying one of those can
    therefore manufacture an entire trailer block that git never wrote."""
    import subprocess

    from data_olympus.git_ops import GitOps

    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}

    def run(*args: str) -> str:
        return subprocess.run(list(args), cwd=repo, check=True, env=env,
                              capture_output=True, text=True).stdout.strip()

    pid = "e" * 32
    run("git", "init", "-q", "--initial-branch=main")
    (repo / "a.md").write_text("a\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "seed")
    before = run("git", "rev-parse", "HEAD")
    git = GitOps(str(repo))

    for codepoint in (0x2028, 0x2029, 0x0085):
        sep = chr(codepoint)
        (repo / f"n{codepoint}.md").write_text("x\n")
        run("git", "add", "-A")
        run("git", "commit", "-qm",
            "memory: note\n\nKB-Agent-Identity: claude\n"
            f"KB-Target-Path: decisions/innocent{sep}KB-Pending-Id: {pid}"
            f"{sep}KB-Target-Path: decisions/victim.md\n")
        assert git.find_claim_commit(
            ref="main", since_sha=before, pending_id=pid,
            target_path="decisions/victim.md",
        ) is False, f"forged with U+{codepoint:04X}"


def test_a_duplicated_key_is_ambiguous_evidence_and_yields_nothing() -> None:
    """git emits BOTH values for a repeated key, so picking one is this
    product's policy rather than git's answer.

    Ambiguous evidence must not close an operator's decision, so a duplicated
    key drops out entirely. This is stated as policy, not as a claim about what
    git does.
    """
    from data_olympus.git_ops import _parse_trailers

    assert _parse_trailers(
        "s\n\nKB-Target-Path: a.md\nKB-Target-Path: b.md\n"
    ) == {}
    # A single occurrence alongside a duplicate is unaffected.
    assert _parse_trailers(
        "s\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\nKB-Target-Path: b.md\n"
    ) == {"KB-Pending-Id": "abc"}


def test_a_value_is_returned_exactly_as_git_emits_it() -> None:
    """Delegation is worthless if the wrapper reinterprets the answer.

    `text=True` applies universal-newline decoding, so a CR inside a value
    becomes a line break and one trailer reads as two. `str.strip` removes
    Unicode whitespace git preserved, turning a value that does NOT name a claim
    into one that does. Both were plumbing that widened acceptance, so the
    message goes in as bytes and comes back as bytes.
    """
    from data_olympus.git_ops import _parse_trailers

    pid = "f" * 32
    for codepoint in (0x00A0, 0x2007, 0x202F, 0x3000):
        parsed = _parse_trailers(f"s\n\nKB-Pending-Id: {pid}{chr(codepoint)}\n")
        assert parsed.get("KB-Pending-Id") == pid + chr(codepoint), (
            f"U+{codepoint:04X} was trimmed"
        )

    # A CR inside a value is one trailer to git, not two.
    parsed = _parse_trailers(f"s\n\nKB-Pending-Id: {pid}\rKB-Target-Path: a.md\n")
    assert "KB-Target-Path" not in parsed
    assert parsed.get("KB-Pending-Id") != pid


def test_git_configuration_cannot_rename_a_key_into_the_claim_link() -> None:
    """git's own trailer CONFIG can defeat the builder's key ownership.

    A `[trailer "KB-Agent-Identity"] key = KB-Pending-Id` stanza makes git
    rename an ordinary builder-owned key into the claim key, so an ordinary
    write whose agent identity is somebody else's pending id parses as that
    claim's evidence. Reproduced against git 2.50.1. The parse therefore runs
    with global, system and environment-injected configuration disabled and
    repository discovery ceilinged.
    """
    from data_olympus.git_ops import _parse_trailers

    victim = "e" * 32
    import tempfile
    hostile = pathlib.Path(tempfile.mkdtemp())
    (hostile / "gitconfig").write_text(
        '[trailer "KB-Agent-Identity"]\n\tkey = KB-Pending-Id\n', encoding="utf-8",
    )
    message = (
        f"s\n\nKB-Agent-Identity: {victim}\nKB-Target-Path: a.md\n"
    )
    previous = os.environ.get("GIT_CONFIG_GLOBAL")
    os.environ["GIT_CONFIG_GLOBAL"] = str(hostile / "gitconfig")
    try:
        # Sanity: the hostile config really does rewrite the key for a plain
        # invocation, so this test would catch a regression in the isolation.
        import subprocess
        raw = subprocess.run(
            ["git", "interpret-trailers", "--parse"], input=message,
            capture_output=True, text=True, check=True,
        ).stdout
        assert "KB-Pending-Id" in raw, "the hostile config did not apply"

        assert "KB-Pending-Id" not in _parse_trailers(message)
    finally:
        if previous is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = previous


def test_a_hostile_target_path_cannot_forge_a_claim_link() -> None:
    """Where the forgery is actually stopped: the BUILDER owns the key set.

    An ordinary auto-committed write controls values (its target path, its agent
    identity), never keys. It cannot introduce a KB-Pending-Id line at all,
    whatever it puts in a path, because the builder refuses every character that
    could start a new trailer line. That is the invariant the parser rounds kept
    circling, stated once at the place it holds.
    """
    import pytest

    from data_olympus.audit_trailers import build_commit_message
    from data_olympus.git_ops import _parse_trailers

    victim = "e" * 32
    for hostile in (
        f"decisions/x\nKB-Pending-Id: {victim}",
        f"decisions/x\u2028KB-Pending-Id: {victim}",
        f"decisions/x\u0085KB-Pending-Id: {victim}",
        f"decisions/x\rKB-Pending-Id: {victim}",
    ):
        with pytest.raises(ValueError):
            build_commit_message(
                subject="memory: note", source_session="s1",
                agent_identity="claude", confidence_original=0.9,
                operator_confirmed=False, proposal_type="memory",
                target_tier="T1", target_path=hostile,
            )

    # A path that IS accepted cannot carry a claim link either.
    benign = build_commit_message(
        subject="memory: note", source_session="s1", agent_identity="claude",
        confidence_original=0.9, operator_confirmed=False,
        proposal_type="memory", target_tier="T1",
        target_path=f"decisions/KB-Pending-Id: {victim}.md",
    )
    assert "KB-Pending-Id" not in _parse_trailers(benign)


# --- trailer grammar, differentially against real git ------------------------

_GRAMMAR_CASES = {
    "canonical": "s\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\n",
    "no space after colon": "s\n\nKB-Pending-Id:abc\n",
    "after a --- divider": "s\n\n---\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\n",
    "--- with a trailing word": (
        "s\n\n--- patch\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\n"
    ),
    "--- with a tab": (
        "s\n\n---\tpatch\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\n"
    ),
    "--- with a non-breaking space": (
        "s\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\n\n---\u00a0\n\nprose\n"
    ),
    "--- with no separator at all": "s\n\n---patch\n\nKB-Pending-Id: abc\n",
    "--- with a vertical tab": (
        "s\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\n\n---\v\n\nprose\n"
    ),
    "--- with a form feed": (
        "s\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\n\n---\f\n\nprose\n"
    ),
    "--- on the very first line": "---\n\nKB-Pending-Id: abc\n",
    "CRLF line endings": "s\r\n\r\nKB-Pending-Id: abc\r\nKB-Target-Path: a.md\r\n",
    # git requires a trailer token to be ASCII alphanumeric or hyphen, so one
    # invalid key voids the whole block.
    "invalid key in the block": (
        "s\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\n@: x\n"
    ),
    # A line starting with whitespace is a CONTINUATION in git's grammar: git
    # folds it into the previous value, so its KB-Target-Path is "a.md foo: x",
    # not "a.md". Reading it as its own trailer silently changes the value the
    # target binding compares.
    "tab-indented continuation": (
        "s\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\n\tfoo: x\n"
    ),
    # git's scissors cutoff: everything below it is excluded when locating
    # trailers. The fifth distinct message-boundary rule this parser had to
    # know, which is why it no longer tries to know any of them.
    "below a scissors line": (
        "s\n\n# ------------------------ >8 ------------------------\n\n"
        "KB-Pending-Id: abc\nKB-Target-Path: a.md\n"
    ),
    "legitimate trailers above a scissors line": (
        "s\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\n\n"
        "# ------------------------ >8 ------------------------\n\ndiff stuff\n"
    ),
    "--- immediately before": "s\n\nbody\n---\nKB-Pending-Id: abc\n",
    "body line in the block": "s\n\nprose here\nKB-Pending-Id: abc\n",
    "trailing blank line": "s\n\nKB-Pending-Id: abc\n\n",
    "leading whitespace line": "s\n\n KB-Pending-Id: abc\n",
    "continuation line": "s\n\nKB-Pending-Id: abc\n  more\n",
    "duplicate key": "s\n\nKB-Target-Path: a.md\nKB-Target-Path: b.md\n",
    "subject only": "KB-Pending-Id: abc\n",
    "empty value": "s\n\nKB-Pending-Id: \n",
    "comment line": "s\n\n# note\nKB-Pending-Id: abc\n",
    "two paragraphs of trailers": (
        "s\n\nKB-Target-Path: a.md\n\nKB-Pending-Id: abc\n"
    ),
}


def _git_trailers(message: str) -> dict[str, str]:
    """What real git parses, as key/value, not merely which keys are present."""
    import subprocess

    out = subprocess.run(
        ["git", "interpret-trailers", "--parse"], input=message,
        capture_output=True, text=True, check=True,
    ).stdout
    parsed: dict[str, str] = {}
    for line in out.split("\n"):
        key, sep, value = line.partition(":")
        if sep:
            parsed[key.strip()] = value.strip()
    return parsed


def _git_sees_pending_id(message: str) -> bool:
    return "KB-Pending-Id" in _git_trailers(message)


def test_parser_never_sees_a_trailer_git_does_not() -> None:
    """The security invariant for the whole recovery path.

    This parser decides whether an approved write counts as committed, and
    commit text is agent-controlled. Being STRICTER than git costs nothing,
    because our own builder writes one fixed shape. Being LOOSER is a forgery
    surface: any construction git does not call a trailer, but we do, is content
    an ordinary write could carry to close somebody else's decision.

    So the assertion is one-directional. Ours must imply git's, never the
    reverse.
    """
    from data_olympus.git_ops import _parse_trailers

    looser = []
    for name, message in _GRAMMAR_CASES.items():
        ours = _parse_trailers(message)
        theirs = _git_trailers(message)
        # Both fields recovery actually uses, compared by VALUE: a key we agree
        # exists but read differently is the same defect as one git never saw.
        for field in ("KB-Pending-Id", "KB-Target-Path"):
            if field in ours and ours[field] != theirs.get(field):
                looser.append(f"{name}:{field}")
    assert not looser, f"parser is looser than git for: {looser}"


def test_the_supported_trailer_grammar_is_what_the_builder_writes() -> None:
    """What this parser accepts, stated positively rather than by exclusion."""
    from data_olympus.git_ops import _parse_trailers

    # A trailing paragraph whose every line is `Key: value`, keys unique and
    # free of spaces, before any `---` divider.
    assert _parse_trailers("s\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\n") == {
        "KB-Pending-Id": "abc", "KB-Target-Path": "a.md",
    }
    # Not a trailer block, per git: prose mixed in, or a patch divider before it.
    assert _parse_trailers("s\n\nprose\nKB-Pending-Id: abc\n") == {}
    assert _parse_trailers("s\n\n---\n\nKB-Pending-Id: abc\n") == {}


def test_a_real_commit_from_the_builder_round_trips() -> None:
    """The grammar is only useful if our own commits satisfy it."""
    from data_olympus.audit_trailers import build_commit_message
    from data_olympus.git_ops import _parse_trailers

    msg = build_commit_message(
        subject="resolve: operator/notes.md", source_session="s1",
        agent_identity="claude", confidence_original=0.4,
        operator_confirmed=True, proposal_type="edit", target_tier="T1",
        target_path="operator/notes.md", pending_id="a" * 32,
    )
    parsed = _parse_trailers(msg)

    assert parsed["KB-Pending-Id"] == "a" * 32
    assert parsed["KB-Target-Path"] == "operator/notes.md"
    assert _git_sees_pending_id(msg)


def test_an_unterminated_divider_at_eof_is_not_a_divider() -> None:
    """git requires an actual whitespace BYTE after `---`. At end of input there
    is none, so a message ending in a bare `---` has no patch divider, its final
    paragraph is ordinary text, and the block before it is therefore not the
    trailer block.

    This case cannot be caught by the differential test above, and that is the
    point of asserting it separately: `git interpret-trailers` appends a missing
    final newline before parsing, so the CLI oracle normalises the very
    difference that matters. Production does not go through the CLI; it reads
    raw `%B` bodies out of `git log -z`.
    """
    from data_olympus.git_ops import _parse_trailers

    unterminated = "s\n\nKB-Pending-Id: abc\nKB-Target-Path: a.md\n\n---"
    assert _parse_trailers(unterminated) == {}

    # The same message WITH the terminator DOES have a divider, so parsing stops
    # there and the block before it is the last paragraph, which is the trailer
    # block. Verified against real git: `git interpret-trailers --parse` returns
    # both fields for this spelling. The two spellings differ, which is the
    # whole point.
    assert _parse_trailers(unterminated + "\n") == {
        "KB-Pending-Id": "abc", "KB-Target-Path": "a.md",
    }

    # And a message with no divider at all still parses normally, so the fix
    # does not simply refuse everything.
    assert _parse_trailers("s\n\nKB-Pending-Id: abc\n")["KB-Pending-Id"] == "abc"


def test_find_claim_commit_rejects_forgeries_in_real_commits(tmp_path) -> None:  # noqa: ANN001
    """The search, against REAL commits rather than a mocked log.

    Mocking `subprocess.run` at module level no longer works here, and that is
    informative: `_parse_trailers` asks git now, so a test that stubs the
    subprocess layer stops exercising the thing under test. Using real commits
    is what the earlier mocked versions should have done.
    """
    import subprocess

    from data_olympus.git_ops import GitOps

    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}

    def run(*args: str) -> str:
        return subprocess.run(list(args), cwd=repo, check=True, env=env,
                              capture_output=True, text=True).stdout.strip()

    pid = "e" * 32
    run("git", "init", "-q", "--initial-branch=main")
    (repo / "seed.md").write_text("seed\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "seed")
    before = run("git", "rev-parse", "HEAD")
    git = GitOps(str(repo))

    forgeries = {
        "invalid key voids the block":
            f"s\n\nKB-Pending-Id: {pid}\nKB-Target-Path: a.md\n@: x\n",
        "continuation changes the target":
            f"s\n\nKB-Pending-Id: {pid}\nKB-Target-Path: a.md\n\tfoo: x\n",
        "below a scissors line":
            "s\n\n# ------------------------ >8 ------------------------\n\n"
            f"KB-Pending-Id: {pid}\nKB-Target-Path: a.md\n",
        "after a patch divider":
            f"s\n\n---\n\nKB-Pending-Id: {pid}\nKB-Target-Path: a.md\n",
    }
    for i, (name, message) in enumerate(forgeries.items()):
        (repo / f"f{i}.md").write_text("x\n")
        run("git", "add", "-A")
        run("git", "commit", "-qm", message)
        assert git.find_claim_commit(
            ref="main", since_sha=before, pending_id=pid, target_path="a.md",
        ) is False, name

    # The genuine article, written by the builder, IS found.
    from data_olympus.audit_trailers import build_commit_message

    (repo / "real.md").write_text("y\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", build_commit_message(
        subject="resolve: a.md", source_session="s1", agent_identity="claude",
        confidence_original=0.4, operator_confirmed=True, proposal_type="edit",
        target_tier="T1", target_path="a.md", pending_id=pid,
    ))
    assert git.find_claim_commit(
        ref="main", since_sha=before, pending_id=pid, target_path="a.md",
    ) is True
    # ...but not for a different target.
    assert git.find_claim_commit(
        ref="main", since_sha=before, pending_id=pid, target_path="other.md",
    ) is False


def test_the_search_deadline_bounds_the_whole_walk(tmp_path) -> None:  # noqa: ANN001
    """`timeout_sec` bounds the entire search, not each candidate.

    Reconciliation holds the write serializer, so a per-candidate timeout let a
    range with many candidates stall unrelated writes for a multiple of it.
    Exhausting the deadline is uncertainty, which keeps the claim's lock and has
    the sweep retry later.
    """
    from unittest import mock

    from data_olympus.git_ops import GitOps

    pid = "e" * 32
    body = f"s\n\nKB-Pending-Id: {pid}\nKB-Target-Path: a.md\n"
    log = mock.Mock(returncode=0, stdout="\0".join([body] * 5))
    git = GitOps(str(tmp_path))

    # monotonic() is read once to set the deadline and once per candidate; the
    # second reading is already past it.
    with mock.patch("data_olympus.git_ops.subprocess.run", return_value=log), \
         mock.patch("data_olympus.git_ops.time.monotonic",
                    side_effect=[0.0, 100.0]):
        found = git.find_claim_commit(
            ref="main", since_sha="a" * 40, pending_id=pid,
            target_path="a.md", timeout_sec=1.0,
        )

    assert found is None, "an exhausted deadline must be uncertainty"
