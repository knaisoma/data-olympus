"""The maintenance inventory workflow gives a write token only to the job that runs on main."""
from __future__ import annotations

import itertools
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "maintenance-inventory.yml"

DRY_RUN_IF = "github.event_name == 'workflow_dispatch' && inputs.dry_run"
REFUSE_IF = (
    "github.event_name == 'workflow_dispatch' && !inputs.dry_run"
    " && github.ref != 'refs/heads/main'"
)
WRITE_IF = (
    "github.ref == 'refs/heads/main' && (github.event_name == 'schedule'"
    " || (github.event_name == 'workflow_dispatch' && !inputs.dry_run))"
)


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _evaluate(condition: str, event: str, ref: str, dry_run: bool) -> bool:
    """Evaluate the small subset of GitHub expression syntax these conditions use."""
    allowed = re.fullmatch(r"[\w.' ()!=&|/-]+", condition)
    assert allowed, condition
    # GitHub compares strings without case: model it by folding every value and literal.
    python = (condition.lower().replace("&&", " and ").replace("||", " or ")
              .replace("!=", " __NE__ ").replace("!", " not ").replace("__NE__", "!="))
    names = {"github": type("G", (), {"event_name": event.lower(), "ref": ref.lower()}),
             "inputs": type("I", (), {"dry_run": dry_run})}
    return bool(eval(python, {"__builtins__": {}}, names))  # noqa: S307 - fixed, checked input


def test_no_workflow_level_permissions():
    assert _workflow()["permissions"] == {}


def test_job_conditions_are_exactly_the_reviewed_ones():
    jobs = _workflow()["jobs"]
    assert set(jobs) == {"dry-run", "refuse-off-main", "inventory"}
    assert jobs["dry-run"]["if"] == DRY_RUN_IF
    assert jobs["refuse-off-main"]["if"] == REFUSE_IF
    assert jobs["inventory"]["if"] == WRITE_IF


# (event, ref, dry_run) -> the one job that runs; a schedule has no dry_run input (empty, false).
CASES = {
    ("schedule", "refs/heads/main", False): "inventory",
    ("workflow_dispatch", "refs/heads/main", False): "inventory",
    ("workflow_dispatch", "refs/heads/main", True): "dry-run",
    ("workflow_dispatch", "refs/heads/feature", True): "dry-run",
    ("workflow_dispatch", "refs/heads/feature", False): "refuse-off-main",
    ("workflow_dispatch", "refs/heads/release/new", False): "refuse-off-main",
}


@pytest.mark.parametrize(("event", "ref", "dry_run"), list(CASES))
def test_exactly_one_job_runs_for_each_trigger(event, ref, dry_run):
    jobs = _workflow()["jobs"]
    running = {name for name, job in jobs.items() if _evaluate(job["if"], event, ref, dry_run)}
    assert running == {CASES[(event, ref, dry_run)]}


def test_only_the_main_job_can_write_issues():
    jobs = _workflow()["jobs"]
    writers = {
        name for name, job in jobs.items()
        if (job.get("permissions") or {}).get("issues") == "write"
    }
    assert writers == {"inventory"}
    assert jobs["inventory"]["permissions"] == {"contents": "read", "issues": "write"}
    assert "--dry-run" not in jobs["inventory"]["steps"][-1]["run"]


def test_the_dry_run_job_holds_a_read_only_token_from_any_ref():
    job = _workflow()["jobs"]["dry-run"]
    assert job["permissions"] == {"contents": "read", "issues": "read"}
    assert "--dry-run" in job["steps"][-1]["run"]


def test_a_write_dispatch_off_main_fails_without_a_token():
    job = _workflow()["jobs"]["refuse-off-main"]
    assert job["permissions"] == {}
    assert all("uses" not in step for step in job["steps"])
    assert job["steps"][-1]["run"].rstrip().endswith("exit 1")


def _guard(job: dict) -> str:
    """The case-sensitive ref check that must be the write job's first step."""
    first = job["steps"][0]
    assert "uses" not in first
    return first["run"]


def _guard_passes(job: dict, ref: str) -> bool:
    result = subprocess.run(
        ["bash", "-c", _guard(job)], env={"GITHUB_REF": ref, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def test_no_combination_gives_a_write_token_off_main_or_to_a_dry_run():
    job = _workflow()["jobs"]["inventory"]
    for event, ref, dry_run in itertools.product(
            ["schedule", "workflow_dispatch", "push"],
            ["refs/heads/main", "refs/heads/Main", "refs/heads/MAIN", "refs/heads/feature",
             "refs/tags/v1"], [True, False]):
        if _evaluate(job["if"], event, ref, dry_run) and _guard_passes(job, ref):
            assert ref == "refs/heads/main" and not (event == "workflow_dispatch" and dry_run)


@pytest.mark.parametrize("ref", ["refs/heads/Main", "refs/heads/MAIN", "refs/heads/mAin"])
def test_a_case_variant_of_main_passes_the_expression_but_fails_the_guard(ref):
    job = _workflow()["jobs"]["inventory"]
    assert _evaluate(job["if"], "workflow_dispatch", ref, False)  # GitHub ignores case here
    assert not _guard_passes(job, ref)
    assert _guard_passes(job, "refs/heads/main")


def test_the_guard_runs_before_any_checkout():
    steps = _workflow()["jobs"]["inventory"]["steps"]
    assert "uses" not in steps[0] and "GITHUB_REF" in steps[0]["run"]
    assert all("uses" in step or "python" not in step.get("run", "") for step in steps[:2])
