"""The maintenance inventory workflow gives a write token only to the job that runs on main."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "maintenance-inventory.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_no_workflow_level_permissions():
    assert _workflow()["permissions"] == {}


def test_only_the_main_job_can_write_issues():
    jobs = _workflow()["jobs"]
    writers = {
        name for name, job in jobs.items()
        if (job.get("permissions") or {}).get("issues") == "write"
    }
    assert writers == {"inventory"}
    condition = jobs["inventory"]["if"]
    assert condition.startswith("github.ref == 'refs/heads/main' && (")
    assert "!inputs.dry_run" in condition
    run = jobs["inventory"]["steps"][-1]["run"]
    assert "--dry-run" not in run


def test_the_dry_run_job_holds_a_read_only_token_from_any_ref():
    job = _workflow()["jobs"]["dry-run"]
    assert job["permissions"] == {"contents": "read", "issues": "read"}
    assert job["if"] == "github.event_name == 'workflow_dispatch' && inputs.dry_run"
    assert "--dry-run" in job["steps"][-1]["run"]


def test_a_write_dispatch_off_main_fails_without_a_token():
    job = _workflow()["jobs"]["refuse-off-main"]
    assert job["permissions"] == {}
    assert "github.ref != 'refs/heads/main'" in job["if"]
    assert all("uses" not in step for step in job["steps"])
    assert "exit 1" in job["steps"][-1]["run"]
