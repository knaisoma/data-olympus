"""All delivered privileged SDLC workflows serialize on one product lock."""
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


@pytest.mark.parametrize("name", [
    "rc-publish-stage.yml", "promote-release.yml",
    "recut-release-branch.yml", "hotfix-cut.yml",
])
def test_privileged_workflows_share_promotion_lock(name):
    path = WORKFLOWS / name
    if not path.exists():
        pytest.skip(f"{name} is delivered by a separate task")
    workflow = yaml.safe_load(path.read_text())
    assert workflow["concurrency"] == {
        "group": "data-olympus-promotion", "cancel-in-progress": False,
    }
    for job in workflow["jobs"].values():
        assert "concurrency" not in job, f"{name} must not override the lock per job"
