"""Offline recut contracts; remote mutations use update-ref, never git push."""
from __future__ import annotations

import json
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
        self.base = self.commit("chore: initial", "initial\n")
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

    def release(self, *, hotfix: bool = False) -> dict:
        branch = "hotfix/new" if hotfix else "release/new"
        if hotfix:
            self.git("checkout", "-b", branch, self.base)
        head = self.commit("fix: reviewed repair", "released\n")
        self.git("checkout", "main")
        self.git("merge", "--squash", branch)
        squash = self.commit("release: 1.4.3")
        self.git("tag", "-a", "v1.4.3", "-m", "Verified release")
        return {
            "tag": "v1.4.3", "squash": squash, "H": head,
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

    def apply_runner(self, plan: dict):
        def execute(command: list[str]):
            assert command[0:2] == ["git", "push"]
            assert "--atomic" in command
            updates = plan["updates"]
            commands = ["start"]
            for ref, sha in updates.items():
                assert f"{sha or ''}:{ref}" in command
                previous = plan["expected_refs"].get(ref)
                previous = previous if previous not in (None, "absent") else "0" * 40
                if sha is None:
                    commands.append(f"delete {ref} {previous}")
                else:
                    commands.append(f"update {ref} {sha} {previous}")
            commands.extend(["prepare", "commit"])
            subprocess.run(
                ["git", "--git-dir", str(self.remote), "update-ref", "--stdin"],
                input="\n".join(commands) + "\n", text=True, check=True,
                capture_output=True,
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
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
    recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.apply_runner(plan))
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
    recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.apply_runner(plan))
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
    recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.apply_runner(plan))
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
    with pytest.raises(recut.RecutError):
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
    recut.apply_plan(repo.path, str(repo.remote), plan, run=repo.apply_runner(plan))
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
