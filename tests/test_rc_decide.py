"""Stage-one builds never publish, including cut and work-branch dry runs."""
from pathlib import Path

import pytest

from scripts import rc_decide
from scripts.sdlc_version import compute_version
from tests.test_sdlc_version import Repo


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


@pytest.mark.parametrize("branch", ["release/new", "hotfix/new"])
@pytest.mark.parametrize("count,dry_run,promotable", [
    (0, False, False), (0, True, False), (1, False, True), (1, True, False),
])
def test_build_decisions(repo, branch, count, dry_run, promotable):
    repo.cut()
    if branch == "hotfix/new":
        repo.git("branch", "-m", branch)
    for _ in range(count):
        repo.commit("fix: repair export")
    version = compute_version(cwd=repo.path, head="HEAD", main="main", branch=branch)
    result = rc_decide.decide(version, dry_run=dry_run)
    assert result["build"] is True
    assert result["publish"] is False
    assert result["promotable"] is promotable
    assert result["dry_run"] is dry_run
    assert result["candidate"] == version["candidate"]


def preflight(repo, branch="release/new", head="HEAD", dry_run=False, event="push"):
    return rc_decide.preflight(
        cwd=repo.path, head=head, main="refs/heads/main", branch=branch,
        branch_ref=f"refs/heads/{branch}", dry_run=dry_run, event=event,
    )


def test_stale_head_refused(repo):
    cut = repo.cut()
    repo.commit("fix: move head")
    with pytest.raises(ValueError, match="branch head"):
        preflight(repo, head=cut)


def test_main_advanced_refused(repo):
    repo.cut()
    repo.commit("fix: candidate")
    repo.git("checkout", "main")
    repo.commit("fix: main advanced")
    with pytest.raises(ValueError, match="recut_required"):
        preflight(repo, head="refs/heads/release/new")


def test_work_branch_requires_dispatch_dry_run(repo):
    repo.cut()
    repo.git("branch", "-m", "feature/test")
    assert preflight(repo, branch="feature/test", dry_run=True,
                     event="workflow_dispatch") == "release/new"
    for event, dry_run in [("push", True), ("workflow_dispatch", False)]:
        with pytest.raises(ValueError, match="dry-run dispatch"):
            preflight(repo, branch="feature/test", dry_run=dry_run, event=event)


def test_adoption_record_at_base_fails_without_ratification(repo):
    repo.git("tag", "v0.11.0")
    repo.write("release/ADOPTION.json", "{}")
    repo.commit("chore: adoption cut")
    repo.git("checkout", "-b", "release/new")
    repo.git("rm", "release/ADOPTION.json")
    repo.commit("chore: retire adoption record")
    with pytest.raises(ValueError, match="Task 9.*ratification"):
        preflight(repo)


def test_invalid_event_refused(repo):
    repo.cut()
    with pytest.raises(ValueError, match="event"):
        preflight(repo, event="pull_request")


def test_engine_refusal_cannot_be_overridden():
    assert rc_decide.decide({"N": 2, "promotable": False}, dry_run=False)["promotable"] is False


def test_stage_one_workflow_has_no_publication_credentials():
    text = Path(".github/workflows/rc-build.yml").read_text()
    assert "contents: read" in text
    assert "secrets." not in text
    assert "id-token:" not in text
    assert "packages:" not in text
    assert "login-action" not in text
    assert "--push" not in text
    assert "type=oci,dest=" in text
    assert "scripts/sdlc_version.py" in text
    assert "--adoption-" not in text
