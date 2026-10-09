"""Offline recut contracts.

Fixture setup mutates the bare remote with update-ref. Apply tests run the
production ``git push`` commands (backup creation, deletion, recreation) only
against a temporary local bare repository, with global and system Git
configuration disabled; no network remote exists.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import sdlc_recut as recut


class Repository:
    def __init__(self, root: Path):
        self.path = root / "source"
        self.path.mkdir()
        self.remote = root / "remote.git"
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.base = self.commit("release: 1.4.2", "initial\n")
        self.git("tag", "-a", "v1.4.2", "-m", "Previous release")
        self.git("checkout", "-b", "release/new")

    def git(self, *args: str) -> str:
        assert not args or args[0] != "push", "Tests must never execute git push"
        return subprocess.check_output(
            ["git", *args], cwd=self.path, text=True, stderr=subprocess.PIPE,
        ).strip()

    def commit(self, message: str, content: str | None = None) -> str:
        if content is not None:
            (self.path / "content.txt").write_text(content)
        self.git("add", ".")
        self.git("commit", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def publish_fixture(self) -> None:
        self.git("clone", "--bare", str(self.path), str(self.remote))
        # As on GitHub, the remote's default branch is main, so release/new and
        # hotfix/new can be deleted and recreated.
        self.git("--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main")

    def release(self, *, hotfix: bool = False, subject: str = "release: 1.4.3",
                change: str = "fix: reviewed repair", tag: str = "v1.4.3") -> dict:
        branch = "hotfix/new" if hotfix else "release/new"
        if hotfix:
            self.git("checkout", "-b", branch, self.base)
        head = self.commit(change, "released\n")
        self.git("checkout", "main")
        self.git("merge", "--squash", branch)
        squash = self.commit(subject)
        self.git("tag", "-a", tag, "-m", "Verified release")
        return {
            "tag": tag, "squash": squash, "H": head,
            "B": self.base, "M": self.base, "branch": branch,
            "digest": "sha256:" + "a" * 64,
            "publication": {"pypi": True, "ghcr": True, "github": True, "mcp": True},
            "delivery_verified": True, "unpromoted_heads": [],
        }

    def options(self, evidence: dict) -> dict:
        return {
            "cwd": self.path, "remote": str(self.remote), "mode": "recut",
            "expected_main": self.git("rev-parse", "main"),
            "expected_head": self.git("rev-parse", "release/new"),
            "expected_base": self.base,
            "expected_hotfix": evidence["H"] if evidence["branch"] == "hotfix/new"
            else "absent",
            "tag": evidence["tag"], "evidence": evidence,
        }

    def remote_ref(self, ref: str) -> str:
        return self.git("--git-dir", str(self.remote), "rev-parse", ref)

    def push_runner(self, commands: list | None = None, *, refuse=None, after=None,
                    before=None):
        """Run each production push for real against the local bare remote.

        Every push is recorded in ``commands``. ``refuse(command)`` returning
        true simulates a server refusal without running the push; ``before``
        and ``after`` run just before and once the push has completed, standing
        in for a concurrent writer.
        Global and system Git configuration are disabled for the push.
        """
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}

        def execute(command: list[str]):
            assert command[:2] == ["git", "push"]
            if commands is not None:
                commands.append(command)
            if refuse and refuse(command):
                return subprocess.CompletedProcess(command, 1, stdout="",
                                                   stderr="! [remote rejected] (simulated)")
            if before:
                before(command)
            result = subprocess.run(command, cwd=self.path, env=env, text=True,
                                    capture_output=True)
            if after:
                after(command)
            return result
        return execute


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


def test_verified_squash_recut_preserves_release_history(repo):
    evidence = repo.release()
    repo.publish_fixture()
    plan = recut.prepare_plan(**repo.options(evidence))
    assert plan["ready"] is True
    assert plan["pending_commits"] == []
    assert plan["new_base"] == evidence["squash"]
    assert plan["updates"]["refs/heads/release/new"] == evidence["squash"]
    recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.push_runner())
    assert repo.remote_ref("refs/heads/release/new") == evidence["squash"]
    assert repo.remote_ref("refs/heads/main") == evidence["squash"]


@pytest.mark.parametrize("problem", [
    "missing", "unpublished", "undelivered", "unpromoted", "wrong_head", "wrong_base",
    "wrong_squash", "bad_digest", "lightweight",
])
def test_uncertain_release_is_refused(repo, problem):
    evidence = repo.release()
    if problem == "unpublished":
        evidence["publication"]["pypi"] = False
    elif problem == "undelivered":
        evidence["delivery_verified"] = False
    elif problem == "unpromoted":
        evidence["unpromoted_heads"] = [evidence["H"]]
    elif problem == "wrong_head":
        evidence["H"] = repo.base
    elif problem == "wrong_base":
        evidence["B"] = evidence["H"]
    elif problem == "wrong_squash":
        evidence["squash"] = repo.base
    elif problem == "bad_digest":
        evidence["digest"] = "latest"
    elif problem == "lightweight":
        repo.git("tag", "-d", "v1.4.3")
        repo.git("tag", "v1.4.3")
    repo.publish_fixture()
    options = repo.options(evidence)
    if problem == "missing":
        options["evidence"] = None
    with pytest.raises(recut.RecutError):
        recut.prepare_plan(**options)


def test_pending_work_requires_exact_acknowledgement_and_remains_reachable(repo):
    evidence = repo.release()
    repo.git("checkout", "release/new")
    pending = repo.commit("feat: pending work", "pending\n")
    repo.publish_fixture()
    options = repo.options(evidence)
    plan = recut.prepare_plan(**options)
    assert plan["ready"] is False
    assert plan["pending_commits"] == [pending]
    wrong = recut.prepare_plan(**options, acknowledge=[pending, evidence["H"]])
    assert wrong["ready"] is False
    plan = recut.prepare_plan(**options, acknowledge=[pending])
    assert plan["ready"] is True
    assert plan["replay"]["commits"] == [pending]
    assert plan["replay"]["onto"] == evidence["squash"]
    assert plan["preservation_ref"]
    recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.push_runner())
    assert repo.remote_ref(plan["preservation_ref"]) == pending
    assert repo.remote_ref("refs/heads/release/new") == evidence["squash"]


def test_open_pull_requests_are_returned_for_manual_replay(repo):
    evidence = repo.release()
    repo.publish_fixture()
    prs = [{"number": 42, "baseRefName": "release/new", "headRefName": "feature/export",
            "url": "https://github.com/example/project/pull/42"}]
    plan = recut.prepare_plan(**repo.options(evidence), pull_requests=prs)
    assert plan["pull_requests"] == prs


def test_hotfix_cut_uses_current_stable_and_engine_identity(repo):
    repo.publish_fixture()
    plan = recut.prepare_plan(
        cwd=repo.path, remote=str(repo.remote), mode="hotfix", expected_main=repo.base,
        expected_head=repo.base, expected_base=repo.base, expected_hotfix="absent",
    )
    assert plan["ready"] is True
    assert plan["updates"]["refs/heads/hotfix/new"] == repo.base
    assert plan["candidate"]["candidate"] == "1.4.3-hotfix.rc.0"
    assert plan["candidate"]["promotable"] is False
    recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.push_runner())
    assert repo.remote_ref("refs/heads/hotfix/new") == repo.base


@pytest.mark.parametrize("subject", ["feat: feature", "fix!: breaking repair"])
def test_hotfix_candidate_refuses_non_fix_impact(repo, subject):
    head = repo.commit(subject)
    with pytest.raises(recut.RecutError, match="hotfix"):
        recut.validate_candidate(cwd=repo.path, head=head, main=repo.base, branch="hotfix/new")


def test_hotfix_candidate_uses_engine_for_fix_identity(repo):
    head = repo.commit("fix: repair")
    result = recut.validate_candidate(
        cwd=repo.path, head=head, main=repo.base, branch="hotfix/new",
    )
    assert result["candidate"] == "1.4.3-hotfix.rc.1"
    assert result["pypi_version"] == "1.4.3.dev1"


def test_hotfix_cut_refuses_main_ahead_of_stable(repo):
    repo.git("checkout", "main")
    main = repo.commit("chore: unreconciled main advance")
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="hotfix base must be current stable main"):
        recut.prepare_plan(
            cwd=repo.path, remote=str(repo.remote), mode="hotfix", expected_main=main,
            expected_head=repo.base, expected_base=repo.base, expected_hotfix="absent",
        )


@pytest.mark.parametrize("field", ["expected_main", "expected_head", "expected_base"])
def test_plan_refuses_stale_lock_inputs(repo, field):
    evidence = repo.release()
    repo.publish_fixture()
    options = repo.options(evidence)
    options[field] = evidence["H"] if field != "expected_head" else repo.base
    with pytest.raises(recut.RecutError):
        recut.prepare_plan(**options)


@pytest.mark.parametrize("ref", ["refs/heads/release/new", "refs/heads/main"])
def test_stale_apply_refuses_moved_remote_before_runner(repo, ref):
    evidence = repo.release()
    repo.publish_fixture()
    plan = recut.prepare_plan(**repo.options(evidence))
    repo.git("--git-dir", str(repo.remote), "update-ref", ref, repo.base)
    commands = []
    with pytest.raises(recut.RecutError):
        recut.apply_plan(repo.path, str(repo.remote), plan, run=commands.append)
    assert commands == []


def test_stale_apply_refuses_changed_release_tag(repo):
    evidence = repo.release()
    repo.publish_fixture()
    plan = recut.prepare_plan(**repo.options(evidence))
    repo.git("--git-dir", str(repo.remote), "update-ref", "refs/tags/v1.4.3", repo.base)
    commands = []
    with pytest.raises(recut.RecutError):
        recut.apply_plan(repo.path, str(repo.remote), plan, run=commands.append)
    assert commands == []


def test_hotfix_recut_preserves_normal_work_and_deletes_verified_hotfix(repo):
    (repo.path / "feature.txt").write_text("pending feature\n")
    pending = repo.commit("feat: pending normal feature")
    evidence = repo.release(hotfix=True)
    repo.publish_fixture()
    plan = recut.prepare_plan(**repo.options(evidence), acknowledge=[pending])
    assert plan["ready"] is True
    assert plan["pending_commits"] == [pending]
    assert plan["updates"]["refs/heads/hotfix/new"] is None
    recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.push_runner())
    assert repo.remote_ref(plan["preservation_ref"]) == pending
    assert repo.remote_ref("refs/heads/release/new") == evidence["squash"]
    assert not repo.git("--git-dir", str(repo.remote), "for-each-ref", "refs/heads/hotfix/new")


def test_hotfix_recut_refuses_pending_replay_conflict(repo):
    pending = repo.commit("feat: incompatible normal work", "feature side\n")
    evidence = repo.release(hotfix=True)
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="conflict"):
        recut.prepare_plan(**repo.options(evidence), acknowledge=[pending])


def test_cli_plan_reports_pending_work_without_applying(repo):
    evidence = repo.release()
    repo.git("checkout", "release/new")
    pending = repo.commit("fix: pending repair", "pending\n")
    repo.publish_fixture()
    result = subprocess.run(
        [sys.executable, str(Path(recut.__file__).resolve()), "recut",
         "--remote", str(repo.remote), "--expected-main", evidence["squash"],
         "--expected-head", pending, "--expected-base", repo.base,
         "--tag", evidence["tag"], "--evidence-json", json.dumps(evidence)],
        cwd=repo.path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout)["pending_commits"] == [pending]
    assert repo.remote_ref("refs/heads/release/new") == pending


def test_recut_refuses_when_main_advanced_past_release_squash(repo):
    evidence = repo.release()
    repo.commit("chore: unreleased main change", "after release\n")
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="recut_required"):
        recut.prepare_plan(**repo.options(evidence))


def test_recut_plan_reports_successor_candidate(repo):
    evidence = repo.release()
    repo.publish_fixture()
    plan = recut.prepare_plan(**repo.options(evidence))
    assert plan["candidate"]["base"] == "v1.4.3"
    assert plan["candidate"]["N"] == 0
    assert plan["candidate"]["promotable"] is False


def test_merged_pending_pull_request_replays_once(repo):
    evidence = repo.release()
    repo.git("checkout", "-b", "feature/merged", "release/new")
    (repo.path / "feature.txt").write_text("merged feature\n")
    side = repo.commit("feat: merged feature")
    repo.git("checkout", "release/new")
    repo.git("merge", "--no-ff", "-m", "feat: merge feature", "feature/merged")
    merge = repo.git("rev-parse", "HEAD")
    repo.publish_fixture()
    plan = recut.prepare_plan(**repo.options(evidence), acknowledge=[side, merge])
    assert plan["ready"] is True
    assert set(plan["pending_commits"]) == {side, merge}
    assert plan["replay"]["steps"] == [merge]


def test_planning_never_writes_into_the_checkout(repo):
    evidence = repo.release()
    repo.publish_fixture()
    recut.prepare_plan(**repo.options(evidence))
    assert not (repo.path / "to-delete").exists()
    assert repo.git("status", "--porcelain") == ""


def test_first_release_from_adoption_cut_recuts(tmp_path):
    """The first new-model release squashes onto the adoption cut C."""
    repo = Repository.__new__(Repository)
    repo.path = tmp_path / "source"
    repo.path.mkdir()
    repo.remote = tmp_path / "remote.git"
    repo.git("init", "-b", "main")
    repo.git("config", "user.name", "Test")
    repo.git("config", "user.email", "test@example.invalid")
    repo.git("config", "commit.gpgsign", "false")
    repo.commit("chore: initial", "initial\n")
    repo.git("tag", "-a", "v0.11.0", "-m", "Old path release")
    anchor = repo.commit("fix: landed on the old path", "old path\n")
    (repo.path / "release").mkdir()
    (repo.path / "release/ADOPTION.json").write_text(
        json.dumps({"anchor": anchor, "base": "0.11.0"}) + "\n")
    cut = repo.commit("chore(release): record adoption cut")
    repo.base = cut
    repo.git("checkout", "-b", "release/new")
    repo.git("rm", "-q", "release/ADOPTION.json")
    repo.commit("chore(release): retire adoption record")
    head = repo.commit("fix: first new-model repair", "repaired\n")
    repo.git("checkout", "main")
    repo.git("merge", "--squash", "release/new")
    squash = repo.commit("release: 0.11.1")
    repo.git("tag", "-a", "v0.11.1", "-m", "First new-model release")
    repo.publish_fixture()
    evidence = {
        "tag": "v0.11.1", "squash": squash, "H": head, "B": cut, "M": cut,
        "branch": "release/new", "digest": "sha256:" + "b" * 64,
        "publication": {"pypi": True, "ghcr": True, "github": True, "mcp": True},
        "delivery_verified": True, "unpromoted_heads": [],
        "adoption_ratified": "2099-01-01:operator-supplied-and-ignored",
    }
    plan = recut.prepare_plan(**repo.options(evidence))
    assert plan["ready"] is True
    assert plan["new_base"] == squash
    assert plan["candidate"]["base"] == "v0.11.1"


# --- Transport: the production git push against a local bare remote. ---------

@pytest.fixture
def isolated_git(monkeypatch):
    """Real pushes must not pick up user hooks, URL rewrites or credentials."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


def _remote_refs(repo: Repository) -> dict:
    lines = repo.git("--git-dir", str(repo.remote), "for-each-ref",
                     "--format=%(refname) %(objectname)").splitlines()
    return dict(line.split(" ") for line in lines)


def _pending_plan(repo: Repository) -> tuple[dict, dict, str]:
    evidence = repo.release()
    repo.git("checkout", "release/new")
    pending = repo.commit("fix: pending repair", "pending\n")
    repo.publish_fixture()
    plan = recut.prepare_plan(**repo.options(evidence), acknowledge=[pending])
    assert plan["ready"] is True
    return evidence, plan, pending


def _hotfix_recut_plan(repo: Repository) -> tuple[dict, dict, str]:
    (repo.path / "feature.txt").write_text("pending feature\n")
    pending = repo.commit("feat: pending normal feature")
    evidence = repo.release(hotfix=True)
    repo.publish_fixture()
    plan = recut.prepare_plan(**repo.options(evidence), acknowledge=[pending])
    assert plan["ready"] is True
    return evidence, plan, pending


RELEASE = "refs/heads/release/new"


def _is_create(command: list[str], ref: str, sha: str | None = None) -> bool:
    return len(command) == 4 and command[3].endswith(f":{ref}") and (
        sha is None or command[3] == f"{sha}:{ref}")


def _is_delete(command: list[str]) -> bool:
    return command[-1].startswith(":")


def _delete(remote: str, ref: str, old: str) -> list[str]:
    """The only accepted deletion: a compare-and-swap lease on the old head."""
    return ["git", "push", f"--force-with-lease={ref}:{old}", remote, f":{ref}"]


@pytest.mark.usefixtures("isolated_git")
def test_real_offline_push_deletes_and_recreates_release_branch(repo):
    evidence, plan, pending = _pending_plan(repo)
    # Non-fast-forward replacement, done as deletion plus creation, never an update.
    assert repo.git("rev-list", "--max-count=1", f"{evidence['squash']}..{pending}") != ""
    before = _remote_refs(repo)
    recut.apply_plan(repo.path, str(repo.remote), plan)
    after = _remote_refs(repo)
    assert after.pop(plan["preservation_ref"]) == pending
    assert after.pop(RELEASE) == evidence["squash"]
    before.pop(RELEASE)
    assert after == before  # main, tags and everything else untouched


@pytest.mark.usefixtures("isolated_git")
def test_real_offline_push_deletes_verified_hotfix_branch(repo):
    evidence, plan, pending = _hotfix_recut_plan(repo)
    recut.apply_plan(repo.path, str(repo.remote), plan)
    refs = _remote_refs(repo)
    assert "refs/heads/hotfix/new" not in refs
    assert refs[RELEASE] == evidence["squash"]
    assert refs[plan["preservation_ref"]] == pending


def test_apply_backs_up_then_deletes_then_recreates_then_deletes_hotfix(repo):
    evidence, plan, pending = _hotfix_recut_plan(repo)
    remote = str(repo.remote)
    commands: list = []
    recut.apply_plan(repo.path, remote, plan, run=repo.push_runner(commands))
    assert commands == [
        ["git", "push", remote, f"{pending}:{plan['preservation_ref']}"],
        _delete(remote, RELEASE, plan["expected_refs"][RELEASE]),
        ["git", "push", remote, f"{evidence['squash']}:{RELEASE}"],
        _delete(remote, "refs/heads/hotfix/new", evidence["H"]),
    ]
    assert [(op["op"], op["ref"]) for op in plan["operations"]] == [
        ("create", plan["preservation_ref"]), ("delete", RELEASE), ("create", RELEASE),
        ("delete", "refs/heads/hotfix/new")]


def test_deletions_are_leased_and_creations_are_plain(repo):
    """Exact argv: a lease only on deletions, on the deleted ref and old head."""
    evidence, plan, pending = _hotfix_recut_plan(repo)
    remote = str(repo.remote)
    commands: list = []
    recut.apply_plan(repo.path, remote, plan, run=repo.push_runner(commands))
    expected = plan["expected_refs"]
    deletes = [c for c in commands if _is_delete(c)]
    creates = [c for c in commands if not _is_delete(c)]
    assert deletes == [_delete(remote, RELEASE, expected[RELEASE]),
                       _delete(remote, "refs/heads/hotfix/new", expected["refs/heads/hotfix/new"])]
    assert creates == [["git", "push", remote, f"{pending}:{plan['preservation_ref']}"],
                       ["git", "push", remote, f"{evidence['squash']}:{RELEASE}"]]
    for command in commands:
        assert not any(arg in ("--force", "-f", "--atomic", "--mirror") or arg.startswith("+")
                       for arg in command), command


def test_only_the_leased_delete_form_in_any_git_invocation(repo, tmp_path, monkeypatch):
    """A git wrapper on PATH records every invocation of planning and apply."""
    real_git = shutil.which("git")
    assert real_git
    log = tmp_path / "git.log"
    wrapper = tmp_path / "bin" / "git"
    wrapper.parent.mkdir()
    wrapper.write_text(
        "#!/bin/sh\n"
        f"for arg in \"$@\"; do printf '%s\\037' \"$arg\"; done >> '{log}'\n"
        f"printf '\\n' >> '{log}'\n"
        f"exec '{real_git}' \"$@\"\n")
    wrapper.chmod(0o755)
    evidence, plan, pending = _hotfix_recut_plan(repo)
    monkeypatch.setenv("PATH", f"{wrapper.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    plan = recut.prepare_plan(**repo.options(evidence), acknowledge=[pending])
    recut.apply_plan(repo.path, str(repo.remote), plan)
    remote = str(repo.remote)
    invocations = [line.split("\x1f")[:-1] for line in log.read_text().splitlines()]
    pushes = [args for args in invocations if args[:1] == ["push"]]
    expected = plan["expected_refs"]
    allowed_deletes = [_delete(remote, RELEASE, expected[RELEASE])[1:],
                       _delete(remote, "refs/heads/hotfix/new",
                               expected["refs/heads/hotfix/new"])[1:]]
    assert [args for args in pushes if args[-1].startswith(":")] == allowed_deletes
    assert [args for args in pushes if not args[-1].startswith(":")] == [
        ["push", remote, f"{pending}:{plan['preservation_ref']}"],
        ["push", remote, f"{evidence['squash']}:{RELEASE}"]]
    for args in invocations:
        if args in allowed_deletes:
            continue
        assert not any(arg.startswith("--force") or arg == "-f" or "force-with-lease" in arg
                       for arg in args), args
    for args in pushes:
        assert not any(arg.startswith("+") or arg == "--atomic" for arg in args), args
    assert _remote_refs(repo)[RELEASE] == evidence["squash"]


def _racer(repo: Repository, parent: str) -> str:
    # The racing commit exists only on the remote, as a concurrent push would.
    return repo.git("--git-dir", str(repo.remote), "-c", "user.name=Racer",
                    "-c", "user.email=racer@example.invalid", "commit-tree",
                    f"{parent}^{{tree}}", "-p", parent, "-m", "fix: racer")


def _race_before(monkeypatch, step: str, action) -> None:
    """Run ``action`` once, just before the guard read that precedes ``step``."""
    real = recut._require_state

    def guarded(cwd, remote, expected, message):
        if message.endswith(f"moved before {step}") and not fired:
            fired.append(True)
            action()
        return real(cwd, remote, expected, message)

    fired: list = []
    monkeypatch.setattr(recut, "_require_state", guarded)


def test_remote_moved_after_backup_is_refused_before_deletion(repo):
    evidence, plan, pending = _pending_plan(repo)
    racer = _racer(repo, pending)

    def race(_command):
        # A concurrent writer moves release/new as soon as the backup exists.
        repo.git("--git-dir", str(repo.remote), "update-ref", RELEASE, racer, pending)

    commands: list = []
    with pytest.raises(recut.RecutError, match="post-apply state uncertain after creating "
                                               "sdlc-preserve/"):
        recut.apply_plan(repo.path, str(repo.remote), plan,
                         run=repo.push_runner(commands, after=race))
    assert len(commands) == 1 and _is_create(commands[0], plan["preservation_ref"], pending)
    after = _remote_refs(repo)
    assert after[RELEASE] == racer
    assert after[plan["preservation_ref"]] == pending
    assert after["refs/heads/main"] == evidence["squash"]


def test_head_moved_just_before_deletion_is_refused(repo, monkeypatch):
    """The read immediately before the deletion is what refuses a moved head."""
    _, plan, pending = _pending_plan(repo)
    racer = _racer(repo, pending)
    _race_before(monkeypatch, "delete release/new", lambda: repo.git(
        "--git-dir", str(repo.remote), "update-ref", RELEASE, racer, pending))
    commands: list = []
    with pytest.raises(recut.RecutError, match="moved before delete release/new"):
        recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.push_runner(commands))
    assert not any(_is_delete(command) for command in commands)
    assert _remote_refs(repo)[RELEASE] == racer


def test_lease_refuses_a_head_moved_inside_the_delete_push(repo):
    """Past the read before it, the compare-and-swap lease still refuses."""
    _, plan, pending = _pending_plan(repo)
    racer = _racer(repo, pending)

    def race(command):
        if _is_delete(command):
            repo.git("--git-dir", str(repo.remote), "update-ref", RELEASE, racer, pending)

    commands: list = []
    with pytest.raises(recut.RecutError, match=f"refused by its lease: it moved from "
                                               f"{pending} to {racer}; nothing was deleted"):
        recut.apply_plan(repo.path, str(repo.remote), plan,
                         run=repo.push_runner(commands, before=race))
    assert commands[-1] == _delete(str(repo.remote), RELEASE, pending)
    assert not any(_is_create(c, RELEASE) for c in commands)
    refs = _remote_refs(repo)
    assert refs[RELEASE] == racer
    assert refs[plan["preservation_ref"]] == pending


def test_remote_moved_between_plan_and_apply_changes_nothing(repo):
    _, plan, _ = _pending_plan(repo)
    repo.git("--git-dir", str(repo.remote), "update-ref", "refs/heads/main", repo.base)
    before = _remote_refs(repo)
    commands: list = []
    with pytest.raises(recut.RecutError, match="moved after planning"):
        recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.push_runner(commands))
    assert commands == []
    assert _remote_refs(repo) == before


def test_refused_deletion_leaves_branch_and_keeps_the_backup(repo):
    _, plan, pending = _pending_plan(repo)
    commands: list = []
    with pytest.raises(recut.RecutError, match="deletion of release/new refused; it is "
                                               f"unchanged at {pending}"):
        recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.push_runner(
            commands, refuse=_is_delete))
    refs = _remote_refs(repo)
    assert refs[RELEASE] == pending
    assert refs[plan["preservation_ref"]] == pending
    assert len(commands) == 2  # backup, then the refused deletion; no creation


def test_creation_failure_after_deletion_recovers_the_old_head(repo):
    evidence, plan, pending = _pending_plan(repo)
    remote = str(repo.remote)
    commands: list = []
    runner = repo.push_runner(
        commands, refuse=lambda command: _is_create(command, RELEASE, evidence["squash"]))
    with pytest.raises(recut.RecutError) as raised:
        recut.apply_plan(repo.path, remote, plan, run=runner)
    message = str(raised.value)
    assert "release/new was deleted by this run" in message
    assert f"restored release/new at old head {pending}" in message
    assert f"git push {remote} {pending}:{RELEASE}" in message
    recovery = [c for c in commands if _is_create(c, RELEASE, pending)]
    assert recovery == [["git", "push", remote, f"{pending}:{RELEASE}"]]
    assert commands[-1] == recovery[0]
    refs = _remote_refs(repo)
    assert refs[RELEASE] == pending
    assert refs[plan["preservation_ref"]] == pending


def test_failed_recovery_reports_the_absent_branch_and_exact_command(repo):
    _, plan, pending = _pending_plan(repo)
    remote = str(repo.remote)
    commands: list = []
    runner = repo.push_runner(commands, refuse=lambda command: _is_create(command, RELEASE))
    with pytest.raises(recut.RecutError) as raised:
        recut.apply_plan(repo.path, remote, plan, run=runner)
    message = str(raised.value)
    assert "recovery push failed and release/new is ABSENT" in message
    assert f"Old head: {pending}" in message
    assert f"Recover with: git push {remote} {pending}:{RELEASE}" in message
    assert len([c for c in commands if _is_create(c, RELEASE, pending)]) == 1
    assert RELEASE not in _remote_refs(repo)


def test_branch_recreated_right_after_deletion_is_never_overwritten(repo):
    _, plan, pending = _pending_plan(repo)

    def race(command):
        if _is_delete(command):
            repo.git("--git-dir", str(repo.remote), "update-ref", RELEASE, repo.base, "")

    commands: list = []
    with pytest.raises(recut.RecutError, match=f"deletion of release/new not confirmed; "
                                               f"old head {pending}"):
        recut.apply_plan(repo.path, str(repo.remote), plan,
                         run=repo.push_runner(commands, after=race))
    assert not any(_is_create(c, RELEASE) for c in commands)
    assert _remote_refs(repo)[RELEASE] == repo.base


def test_branch_recreated_before_creation_is_never_overwritten(repo, monkeypatch):
    _, plan, pending = _pending_plan(repo)
    _race_before(monkeypatch, "create release/new", lambda: repo.git(
        "--git-dir", str(repo.remote), "update-ref", RELEASE, repo.base, ""))
    commands: list = []
    with pytest.raises(recut.RecutError) as raised:
        recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.push_runner(commands))
    message = str(raised.value)
    assert f"now exists at {repo.base}, so no recovery push was made" in message
    assert f"Old head: {pending}" in message
    assert not any(_is_create(c, RELEASE) for c in commands)
    assert _remote_refs(repo)[RELEASE] == repo.base


def test_post_apply_state_is_verified(repo):
    _, plan, pending = _pending_plan(repo)

    def silent_success(command):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(recut.RecutError, match="post-apply state uncertain"):
        recut.apply_plan(repo.path, str(repo.remote), plan, run=silent_success)
    assert _remote_refs(repo)[RELEASE] == pending


def test_unconfirmed_deletion_is_refused(repo):
    _, plan, pending = _pending_plan(repo)
    runner = repo.push_runner()

    def deletion_noop(command):
        if _is_delete(command):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return runner(command)

    with pytest.raises(recut.RecutError, match="deletion of release/new not confirmed"):
        recut.apply_plan(repo.path, str(repo.remote), plan, run=deletion_noop)
    assert _remote_refs(repo)[RELEASE] == pending


@pytest.mark.usefixtures("isolated_git")
def test_real_offline_hotfix_cut_is_a_plain_creation(repo):
    repo.publish_fixture()
    plan = recut.prepare_plan(
        cwd=repo.path, remote=str(repo.remote), mode="hotfix", expected_main=repo.base,
        expected_head=repo.base, expected_base=repo.base, expected_hotfix="absent",
    )
    assert plan["operations"] == [
        {"op": "create", "ref": "refs/heads/hotfix/new", "sha": repo.base}]
    commands: list = []
    recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.push_runner(commands))
    assert commands == [["git", "push", str(repo.remote), f"{repo.base}:refs/heads/hotfix/new"]]
    assert _remote_refs(repo)["refs/heads/hotfix/new"] == repo.base


def test_plan_never_creates_an_existing_ref():
    plan = {"updates": {"refs/heads/hotfix/new": "a" * 40},
            "expected_refs": {"refs/heads/hotfix/new": "b" * 40}}
    with pytest.raises(recut.RecutError, match="creates an existing ref"):
        recut.apply_operations(plan)


def _recovery_facts(message: str, remote: str, old: str, backup: str) -> None:
    assert f"Old head: {old}" in message
    assert f"Backup ref: {backup}" in message
    assert f"Recover with: git push {remote} {old}:{RELEASE}" in message


def _fail_reads_after(monkeypatch, trigger) -> None:
    """Every remote read fails once ``trigger`` has been set by the runner."""
    real = recut.remote_snapshot

    def snapshot(cwd, remote):
        if trigger:
            raise recut.RecutError("git ls-remote: could not read from remote")
        return real(cwd, remote)

    monkeypatch.setattr(recut, "remote_snapshot", snapshot)


def test_unreadable_remote_after_deletion_still_recovers_once(repo, monkeypatch):
    """Post-delete read fails (network or token): one recovery push, full facts."""
    _, plan, pending = _pending_plan(repo)
    remote = str(repo.remote)
    trigger: list = []

    def after(command):
        if _is_delete(command):
            trigger.append(True)

    _fail_reads_after(monkeypatch, trigger)
    commands: list = []
    with pytest.raises(recut.RecutError) as raised:
        recut.apply_plan(repo.path, remote, plan, run=repo.push_runner(commands, after=after))
    message = str(raised.value)
    assert "may have been deleted by this run" in message
    assert "could not be read to confirm" in message
    _recovery_facts(message, remote, pending, plan["preservation_ref"])
    assert [c for c in commands if _is_create(c, RELEASE)] == [
        ["git", "push", remote, f"{pending}:{RELEASE}"]]
    assert _remote_refs(repo)[RELEASE] == pending


def test_unreadable_remote_after_refused_creation_still_recovers_once(repo, monkeypatch):
    """Creation refused, then every read fails: the recovery push is still made."""
    evidence, plan, pending = _pending_plan(repo)
    remote = str(repo.remote)
    trigger: list = []

    def refuse(command):
        if _is_create(command, RELEASE, evidence["squash"]):
            trigger.append(True)
            return True
        return False

    _fail_reads_after(monkeypatch, trigger)
    commands: list = []
    with pytest.raises(recut.RecutError) as raised:
        recut.apply_plan(repo.path, remote, plan, run=repo.push_runner(commands, refuse=refuse))
    message = str(raised.value)
    assert "release/new was deleted by this run" in message
    _recovery_facts(message, remote, pending, plan["preservation_ref"])
    assert commands[-1] == ["git", "push", remote, f"{pending}:{RELEASE}"]
    assert len([c for c in commands if _is_create(c, RELEASE, pending)]) == 1
    assert _remote_refs(repo)[RELEASE] == pending


def test_ambiguous_deletion_reported_as_failed_is_recovered(repo):
    """The deletion takes effect but the push reports failure."""
    _, plan, pending = _pending_plan(repo)
    remote = str(repo.remote)
    runner = repo.push_runner()
    commands: list = []

    def lying(command):
        commands.append(command)
        result = runner(command)
        if _is_delete(command):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="timeout")
        return result

    with pytest.raises(recut.RecutError) as raised:
        recut.apply_plan(repo.path, remote, plan, run=lying)
    message = str(raised.value)
    assert "may have been deleted by this run" in message
    assert f"restored release/new at old head {pending}" in message
    _recovery_facts(message, remote, pending, plan["preservation_ref"])
    assert [c for c in commands if _is_create(c, RELEASE)] == [
        ["git", "push", remote, f"{pending}:{RELEASE}"]]
    assert _remote_refs(repo)[RELEASE] == pending


def test_main_moved_after_deletion_recovers_the_old_head(repo):
    """The deletion succeeds, then main moves before the confirming read."""
    _, plan, pending = _pending_plan(repo)
    remote = str(repo.remote)

    def move_main(command):
        if _is_delete(command):
            repo.git("--git-dir", remote, "update-ref", "refs/heads/main", repo.base)

    commands: list = []
    with pytest.raises(recut.RecutError) as raised:
        recut.apply_plan(repo.path, remote, plan,
                         run=repo.push_runner(commands, after=move_main))
    message = str(raised.value)
    assert "deletion of release/new not confirmed" in message
    assert f"restored release/new at old head {pending}" in message
    _recovery_facts(message, remote, pending, plan["preservation_ref"])
    assert [c for c in commands if _is_create(c, RELEASE)] == [
        ["git", "push", remote, f"{pending}:{RELEASE}"]]
    refs = _remote_refs(repo)
    assert refs[RELEASE] == pending
    assert refs["refs/heads/main"] == repo.base


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/tags/v1.0.0", "refs/heads/feature/x"])
@pytest.mark.parametrize("sha", [None, "a" * 40])
def test_plan_with_any_other_ref_is_refused(ref, sha):
    plan = {"updates": {ref: sha}, "expected_refs": {ref: "b" * 40}}
    with pytest.raises(recut.RecutError, match=f"unsupported ref in plan: {ref}"):
        recut.apply_operations(plan)
    for op in ("create", "delete"):
        with pytest.raises(recut.RecutError, match=f"unsupported ref in plan: {ref}"):
            recut._apply_command("origin", {"op": op, "ref": ref, "sha": "b" * 40})


def test_apply_refuses_an_unready_plan_before_any_read_or_push(repo, monkeypatch):
    evidence = repo.release()
    repo.git("checkout", "release/new")
    repo.commit("fix: pending repair", "pending\n")
    repo.publish_fixture()
    plan = recut.prepare_plan(**repo.options(evidence))
    assert plan["ready"] is False
    monkeypatch.setattr(recut, "remote_snapshot", lambda *_: pytest.fail("remote read"))
    commands: list = []
    with pytest.raises(recut.RecutError, match="acknowledge the exact listed pending SHAs"):
        recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.push_runner(commands))
    assert commands == []


# --- Pending-work and hotfix guards. -----------------------------------------

def test_duplicate_acknowledgement_is_not_ready(repo):
    evidence = repo.release()
    repo.git("checkout", "release/new")
    pending = repo.commit("fix: pending repair", "pending\n")
    repo.publish_fixture()
    plan = recut.prepare_plan(**repo.options(evidence), acknowledge=[pending, pending])
    assert plan["ready"] is False


@pytest.mark.parametrize("value", [5, "abc", {"sha": "x"}, [5], None])
def test_acknowledge_must_be_a_list_of_strings(repo, value):
    evidence = repo.release()
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="list of SHA strings"):
        recut.prepare_plan(**repo.options(evidence), acknowledge=value)


def test_cli_rejects_non_list_acknowledge_without_traceback(repo):
    evidence = repo.release()
    repo.publish_fixture()
    result = subprocess.run(
        [sys.executable, str(Path(recut.__file__).resolve()), "recut",
         "--remote", str(repo.remote), "--expected-main", evidence["squash"],
         "--expected-head", evidence["H"], "--expected-base", repo.base,
         "--tag", evidence["tag"], "--evidence-json", json.dumps(evidence),
         "--acknowledge", "5"],
        cwd=repo.path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "blocked: acknowledge must be a JSON list of SHA strings" in result.stderr


def test_hotfix_with_unreleased_commits_is_not_deleted(repo):
    evidence = repo.release(hotfix=True)
    repo.git("checkout", "hotfix/new")
    unreleased = repo.commit("fix: unreleased repair", "unreleased\n")
    repo.publish_fixture()
    options = repo.options(evidence)
    options["expected_hotfix"] = unreleased
    with pytest.raises(recut.RecutError, match="unreleased commits"):
        recut.prepare_plan(**options)


def test_hotfix_cut_refuses_existing_hotfix_branch(repo):
    repo.git("branch", "hotfix/new", repo.base)
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="hotfix/new already exists"):
        recut.prepare_plan(
            cwd=repo.path, remote=str(repo.remote), mode="hotfix", expected_main=repo.base,
            expected_head=repo.base, expected_base=repo.base, expected_hotfix=repo.base,
        )


def test_released_hotfix_candidate_is_checked_fixes_only(repo):
    """A feature on hotfix/new tagged as a minor release must not reconcile."""
    evidence = repo.release(hotfix=True, change="feat: not a repair",
                            subject="release: 1.5.0", tag="v1.5.0")
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="hotfix/new accepts fixes only"):
        recut.prepare_plan(**repo.options(evidence))


# --- Tag-target checks: each fixture trips exactly the named guard. ----------

def test_unpromotable_released_candidate_is_refused(repo):
    """A release whose reviewed H equals its cut (N=0) never reconciles."""
    repo.git("checkout", "main")
    squash = repo.commit("release: 1.4.3")
    repo.git("tag", "-a", "v1.4.3", "-m", "Release of an N=0 candidate")
    repo.publish_fixture()
    evidence = {
        "tag": "v1.4.3", "squash": squash, "H": repo.base, "B": repo.base,
        "M": repo.base, "branch": "release/new", "digest": "sha256:" + "c" * 64,
        "publication": {"pypi": True, "ghcr": True, "github": True, "mcp": True},
        "delivery_verified": True, "unpromoted_heads": [],
    }
    with pytest.raises(recut.RecutError, match="promotable candidate"):
        recut.prepare_plan(**repo.options(evidence))


def test_tag_must_be_the_latest_stable_tag_on_main(repo):
    evidence = repo.release()
    newer = repo.commit("release: 1.4.4")
    repo.git("tag", "-a", "v1.4.4", "-m", "Newer stable release", newer)
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="not the current stable release on main"):
        recut.prepare_plan(**repo.options(evidence))


def test_tag_must_name_main_head_or_latest_release_squash(repo):
    evidence = repo.release()
    repo.commit("release: 1.4.4")  # an untagged, unreconciled release squash
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="main head or latest release squash"):
        recut.prepare_plan(**repo.options(evidence))


def test_release_squash_subject_must_match_tag(repo):
    evidence = repo.release(subject="chore: squash release")
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="subject does not match tag"):
        recut.prepare_plan(**repo.options(evidence))


def test_release_parent_must_equal_recorded_base_and_main(repo):
    evidence = repo.release()
    evidence["M"] = evidence["H"]
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="unreconciled release parent/base"):
        recut.prepare_plan(**repo.options(evidence))


def test_release_tree_must_equal_reviewed_head(repo):
    evidence = repo.release()
    evidence["H"] = repo.base
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="squash tree does not equal reviewed H"):
        recut.prepare_plan(**repo.options(evidence))


# --- Unreconciled stable tags (STD-U-821). -----------------------------------

def _hotfix_options(repo: Repository, main: str) -> dict:
    return {"cwd": repo.path, "remote": str(repo.remote), "mode": "hotfix",
            "expected_main": main, "expected_head": repo.git("rev-parse", "release/new"),
            "expected_base": main, "expected_hotfix": "absent"}


@pytest.mark.parametrize("mode", ["recut", "hotfix"])
def test_stray_stable_tag_off_main_is_refused(repo, mode):
    if mode == "recut":
        evidence = repo.release()
        options = repo.options(evidence)
    else:
        options = None
    repo.git("checkout", "release/new")
    stray = repo.commit("fix: never merged", "stray\n")
    repo.git("tag", "-a", "v9.0.0", "-m", "Stray tag", stray)
    repo.git("checkout", "main")
    repo.publish_fixture()
    if options is None:
        options = _hotfix_options(repo, repo.base)
    else:
        options["expected_head"] = stray
    with pytest.raises(recut.RecutError, match="unreconciled stable tag v9.0.0"):
        recut.prepare_plan(**options)


def test_older_stable_tag_outside_main_is_refused(repo):
    side = repo.commit("fix: side work", "side\n")
    repo.git("tag", "-a", "v1.0.0", "-m", "Never merged", side)
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="unreconciled stable tag v1.0.0"):
        recut.prepare_plan(**_hotfix_options(repo, repo.base))


@pytest.mark.parametrize(("side_tag", "allowed"), [("v1.4.1", True), ("v1.5.0", False)])
def test_merged_off_line_stable_tag_is_allowed_only_when_older(tmp_path, side_tag, allowed):
    """Mirrors v0.6.0: a historical release merged into main off the first parent."""
    repo = Repository(tmp_path)
    repo.git("checkout", "-b", "side", repo.base)
    side = repo.commit("fix: merged side release", "side\n")
    repo.git("tag", "-a", side_tag, "-m", "Historical release", side)
    repo.git("checkout", "main")
    repo.git("merge", "--no-ff", "-m", "release: 1.4.3", "side")
    merged = repo.git("rev-parse", "HEAD")
    repo.git("tag", "-a", "v1.4.3", "-m", "Current stable", merged)
    repo.git("branch", "-f", "release/new", merged)
    repo.publish_fixture()
    options = _hotfix_options(repo, merged)
    if allowed:
        plan = recut.prepare_plan(**options)
        assert plan["updates"] == {"refs/heads/hotfix/new": merged}
    else:
        with pytest.raises(recut.RecutError, match=f"unreconciled stable tag {side_tag}"):
            recut.prepare_plan(**options)


@pytest.mark.parametrize("name", ["snapshot-tree", "v0.0.1"])
def test_tag_on_non_commit_is_named(repo, name):
    evidence = repo.release()
    repo.git("tag", name, f"{repo.base}^{{tree}}")
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match=f"refs/tags/{name} does not resolve to a commit"):
        recut.prepare_plan(**repo.options(evidence))


# --- Fetch consistency. -------------------------------------------------------

def _wrap_fetch(monkeypatch, after_fetch):
    real = recut._git

    def wrapped(cwd, *args):
        output = real(cwd, *args)
        if args and args[0] == "fetch":
            after_fetch()
        return output

    monkeypatch.setattr(recut, "_git", wrapped)


def test_remote_change_during_fetch_is_refused(repo, monkeypatch):
    evidence = repo.release()
    repo.publish_fixture()
    _wrap_fetch(monkeypatch, lambda: repo.git(
        "--git-dir", str(repo.remote), "update-ref", "refs/heads/release/new", repo.base))
    with pytest.raises(recut.RecutError, match="remote changed during fetch"):
        recut.prepare_plan(**repo.options(evidence))


def test_fetched_ref_must_match_snapshot(repo, monkeypatch):
    evidence = repo.release()
    repo.publish_fixture()
    _wrap_fetch(monkeypatch, lambda: repo.git(
        "update-ref", "refs/sdlc/heads/release/new", repo.base))
    with pytest.raises(recut.RecutError, match="stale fetched ref: refs/heads/release/new"):
        recut.prepare_plan(**repo.options(evidence))


# --- Old-path workflows outside the promotion lock. --------------------------

@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "requested", "pending"])
@pytest.mark.parametrize("workflow", ["tag-release.yml", "rc-publish.yml", "set-channel.yml"])
def test_active_old_path_run_is_refused(repo, workflow, status):
    evidence = repo.release()
    repo.publish_fixture()
    runs = [{"id": 1, "path": f".github/workflows/{workflow}", "status": status}]
    with pytest.raises(recut.RecutError, match="old-path workflow run active"):
        recut.prepare_plan(**repo.options(evidence), old_path_runs=runs)


def test_completed_or_unrelated_runs_do_not_block(repo):
    evidence = repo.release()
    repo.publish_fixture()
    runs = [{"id": 1, "path": ".github/workflows/tag-release.yml", "status": "completed"},
            {"id": 2, "path": ".github/workflows/ci.yaml", "status": "in_progress"}]
    assert recut.prepare_plan(**repo.options(evidence), old_path_runs=runs)["ready"] is True


@pytest.mark.parametrize("runs", [{"path": "x"}, [1], "[]"])
def test_old_path_runs_must_be_a_list_of_objects(runs):
    with pytest.raises(recut.RecutError, match="list of objects"):
        recut.refuse_old_path_activity(runs)


def test_old_path_run_without_path_is_uncertain():
    with pytest.raises(recut.RecutError, match="state uncertain"):
        recut.refuse_old_path_activity([{"id": 3, "status": "queued"}])


def test_cli_refuses_active_old_path_run(repo):
    evidence = repo.release()
    repo.publish_fixture()
    runs = [{"id": 7, "path": ".github/workflows/rc-publish.yml", "status": "queued"}]
    result = subprocess.run(
        [sys.executable, str(Path(recut.__file__).resolve()), "recut",
         "--remote", str(repo.remote), "--expected-main", evidence["squash"],
         "--expected-head", evidence["H"], "--expected-base", repo.base,
         "--tag", evidence["tag"], "--evidence-json", json.dumps(evidence),
         "--old-path-runs-json", json.dumps(runs)],
        cwd=repo.path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "old-path workflow run active" in result.stderr
    assert result.stdout == ""


# --- The newest stable tag must be a reconciled release in both modes. -------

def test_hotfix_refuses_lightweight_latest_stable_tag(repo):
    repo.git("checkout", "main")
    head = repo.commit("release: 1.5.0")
    repo.git("tag", "v1.5.0", head)
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="release tag must be annotated: v1.5.0"):
        recut.prepare_plan(**_hotfix_options(repo, head))


def test_hotfix_refuses_latest_stable_tag_on_non_release_commit(repo):
    repo.git("checkout", "main")
    head = repo.commit("feat: unreleased work")
    repo.git("tag", "-a", "v1.5.0", "-m", "Ad hoc tag", head)
    repo.publish_fixture()
    with pytest.raises(recut.RecutError, match="subject does not match tag v1.5.0"):
        recut.prepare_plan(**_hotfix_options(repo, head))


def _historical_repo(tmp_path, *, annotated: bool) -> Repository:
    """Pre-model releases (v0.11.0 and older) are tags on ordinary commits."""
    repo = Repository.__new__(Repository)
    repo.path = tmp_path / "source"
    repo.path.mkdir()
    repo.remote = tmp_path / "remote.git"
    repo.git("init", "-b", "main")
    repo.git("config", "user.name", "Test")
    repo.git("config", "user.email", "test@example.invalid")
    repo.git("config", "commit.gpgsign", "false")
    repo.base = repo.commit("fix(config): refuse an unrecognised value", "old path\n")
    repo.git("tag", *(["-a", "-m", "Old path release"] if annotated else []), "v0.11.0")
    repo.git("branch", "release/new")
    repo.publish_fixture()
    return repo


def test_hotfix_accepts_historical_annotated_tag_on_ordinary_commit(tmp_path):
    repo = _historical_repo(tmp_path, annotated=True)
    plan = recut.prepare_plan(**_hotfix_options(repo, repo.base))
    assert plan["updates"] == {"refs/heads/hotfix/new": repo.base}
    assert plan["candidate"]["candidate"] == "0.11.1-hotfix.rc.0"


def test_historical_tag_must_still_be_annotated(tmp_path):
    repo = _historical_repo(tmp_path, annotated=False)
    with pytest.raises(recut.RecutError, match="release tag must be annotated: v0.11.0"):
        recut.prepare_plan(**_hotfix_options(repo, repo.base))


def test_recut_of_a_historical_tag_is_refused(tmp_path):
    """Recut follows a new-model release; a pre-model tag never qualifies."""
    repo = _historical_repo(tmp_path, annotated=True)
    evidence = {
        "tag": "v0.11.0", "squash": repo.base, "H": repo.base, "B": repo.base,
        "M": repo.base, "branch": "release/new", "digest": "sha256:" + "d" * 64,
        "publication": {"pypi": True, "ghcr": True, "github": True, "mcp": True},
        "delivery_verified": True, "unpromoted_heads": [],
    }
    with pytest.raises(recut.RecutError, match="subject does not match tag v0.11.0"):
        recut.prepare_plan(**repo.options(evidence))
