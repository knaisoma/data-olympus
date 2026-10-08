"""Stage-one builds never publish, including cut and work-branch dry runs."""
from pathlib import Path

import pytest

from scripts import rc_decide
from scripts.sdlc_version import VersionError, compute_version
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
        trusted_dir=repo.path.parent / "trusted",
    ).engine_branch


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


def test_adoption_record_at_base_goes_to_the_engine(repo):
    """The cut is no longer refused outright: the engine validates the record."""
    repo.git("tag", "v0.11.0")
    repo.write("release/ADOPTION.json", "{}")
    repo.commit("chore: adoption cut")
    repo.git("checkout", "-b", "release/new")
    repo.git("rm", "release/ADOPTION.json")
    repo.commit("chore: retire adoption record")
    # main carries no pinned ratification module: refused before any build.
    with pytest.raises(ValueError, match="trusted adoption ratification"):
        preflight(repo)
    # A dispatched dry run reaches the engine, which rejects the record itself.
    with pytest.raises(VersionError, match="bad_adoption"):
        preflight(repo, dry_run=True, event="workflow_dispatch")


def test_invalid_event_refused(repo):
    repo.cut()
    with pytest.raises(ValueError, match="event"):
        preflight(repo, event="pull_request")


def test_adoption_record_only_at_head_is_not_the_cut_record(repo):
    repo.cut()
    repo.write("release/ADOPTION.json", "{}")
    repo.commit("chore: add head-only record")
    assert preflight(repo) == "release/new"


def test_cut_build_is_never_promotable_even_if_the_engine_says_so():
    """R2: N=0 stays unpromotable in decide itself, independent of the engine."""
    result = rc_decide.decide({"N": 0, "promotable": True}, dry_run=False)
    assert result["promotable"] is False
    assert result["publish"] is False


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


def _steps():
    import yaml

    workflow = yaml.safe_load(Path(".github/workflows/rc-build.yml").read_text())
    return {step.get("name"): step.get("run", "") for step in workflow["jobs"]["build"]["steps"]}


def test_stage_one_workflow_takes_adoption_only_from_preflight():
    steps = _steps()
    admission = steps["Fetch current heads and require exact source"]
    assert "--trusted-dir to-delete/rc-trusted" in admission
    engine = steps["Compute content-derived identity and build decision"]
    # Ratification values come only from preflight's env lines (main's blobs).
    assert '--adoption-ratified "$ADOPTION_RATIFIED"' in engine
    assert '--standard-file "$ADOPTION_STANDARD_FILE"' in engine
    assert '"${adoption[@]}"' in engine
    assert "dry-run) adoption=(--adoption-dry-run)" in engine
    assert "exit 1" in engine
    import yaml

    workflow = yaml.safe_load(Path(".github/workflows/rc-build.yml").read_text())
    # A push has no inputs, so it is never a dry run; only a dispatch can set it.
    assert workflow["jobs"]["build"]["env"]["DRY_RUN"] == "${{ inputs.dry_run || false }}"
    text = Path(".github/workflows/rc-build.yml").read_text()
    assert "adoption_ratification.py" not in text
    assert "std-u-821" not in text
    assert text.count("--adoption-dry-run") == 1
    assert "inputs." not in engine and "inputs." not in admission


def test_trusted_stages_never_take_adoption_from_workflows_or_dry_run():
    for workflow in ("rc-publish-stage.yml", "promote-release.yml"):
        assert "--adoption-" not in Path(".github/workflows", workflow).read_text()
    for script in ("scripts/rc_verify_and_publish.py", "scripts/release_record.py"):
        assert "adoption_dry_run" not in Path(script).read_text()
    # Stage 2 runs from main's checkout; promotion's checkout is S (tree of H),
    # so it reads the pinned values from the recorded M's blobs instead.
    assert "ratification.engine_kwargs(Path(ROOT))" in Path(
        "scripts/rc_verify_and_publish.py").read_text()
    promotion = Path("scripts/release_record.py").read_text()
    assert "main_ratification_kwargs" in promotion
    assert "engine_kwargs" not in promotion
