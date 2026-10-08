"""Offline recut contracts.

Fixture setup mutates the bare remote with update-ref. Transport tests run the
production ``git push`` only against a temporary local bare repository, with
global and system Git configuration disabled; no network remote exists.
"""
from __future__ import annotations

import json
import os
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


@pytest.mark.usefixtures("isolated_git")
def test_real_offline_push_replaces_release_branch_atomically(repo):
    evidence, plan, pending = _pending_plan(repo)
    # Non-fast-forward replacement: only the per-ref lease makes this push legal.
    assert repo.git("rev-list", "--max-count=1", f"{evidence['squash']}..{pending}") != ""
    recut.apply_plan(repo.path, str(repo.remote), plan)
    assert repo.remote_ref("refs/heads/release/new") == evidence["squash"]
    assert repo.remote_ref(plan["preservation_ref"]) == pending
    assert repo.remote_ref("refs/heads/main") == evidence["squash"]


@pytest.mark.usefixtures("isolated_git")
def test_real_offline_push_deletes_verified_hotfix_branch(repo):
    (repo.path / "feature.txt").write_text("pending feature\n")
    pending = repo.commit("feat: pending normal feature")
    evidence = repo.release(hotfix=True)
    repo.publish_fixture()
    plan = recut.prepare_plan(**repo.options(evidence), acknowledge=[pending])
    recut.apply_plan(repo.path, str(repo.remote), plan)
    refs = _remote_refs(repo)
    assert "refs/heads/hotfix/new" not in refs
    assert refs["refs/heads/release/new"] == evidence["squash"]
    assert refs[plan["preservation_ref"]] == pending


@pytest.mark.usefixtures("isolated_git")
def test_remote_moved_between_check_and_push_changes_nothing(repo):
    evidence, plan, pending = _pending_plan(repo)
    # The racing commit exists only on the remote, as a concurrent push would.
    racer = repo.git("--git-dir", str(repo.remote), "-c", "user.name=Racer",
                     "-c", "user.email=racer@example.invalid", "commit-tree",
                     f"{pending}^{{tree}}", "-p", pending, "-m", "fix: racer")

    def race_then_push(command):
        # A concurrent writer moves release/new after apply_plan's snapshot.
        repo.git("--git-dir", str(repo.remote), "update-ref",
                 "refs/heads/release/new", racer, pending)
        before.update(_remote_refs(repo))
        return subprocess.run(command, cwd=repo.path, text=True, capture_output=True)

    before: dict = {}
    with pytest.raises(recut.RecutError, match="atomic push failed"):
        recut.apply_plan(repo.path, str(repo.remote), plan, run=race_then_push)
    after = _remote_refs(repo)
    assert after == before
    assert after["refs/heads/release/new"] == racer
    assert plan["preservation_ref"] not in after


def test_push_has_one_lease_per_updated_ref_and_never_forces(repo):
    (repo.path / "feature.txt").write_text("pending feature\n")
    pending = repo.commit("feat: pending normal feature")
    evidence = repo.release(hotfix=True)
    repo.publish_fixture()
    plan = recut.prepare_plan(**repo.options(evidence), acknowledge=[pending])
    commands = []
    runner = repo.apply_runner(plan)

    def record(command):
        commands.append(command)
        return runner(command)

    recut.apply_plan(repo.path, str(repo.remote), plan, run=record)
    [command] = commands
    leases = [arg for arg in command if arg.startswith("--force-with-lease")]
    expected = {f"--force-with-lease={ref}:{plan['expected_refs'].get(ref, '')}"
                for ref in plan["updates"]}
    assert len(leases) == len(plan["updates"]) == 3
    assert set(leases) == expected
    assert command[:3] == ["git", "push", "--atomic"]
    assert "--force" not in command and "-f" not in command
    assert not any(arg.startswith("--force") and not arg.startswith("--force-with-lease=")
                   for arg in command)
    refspecs = command[command.index(str(repo.remote)) + 1:]
    assert sorted(refspecs) == sorted(f"{sha or ''}:{ref}" for ref, sha in
                                      plan["updates"].items())
    assert not any(spec.startswith("+") for spec in refspecs)


def test_post_apply_state_is_verified(repo):
    evidence, plan, _ = _pending_plan(repo)

    def silent_success(command):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(recut.RecutError, match="post-apply state uncertain"):
        recut.apply_plan(repo.path, str(repo.remote), plan, run=silent_success)


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
