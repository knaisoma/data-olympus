"""All delivered privileged SDLC workflows serialize on one product lock."""
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# Delivered by this task: a missing or renamed file must fail, never skip.
DELIVERED = ("recut-release-branch.yml", "hotfix-cut.yml")
# TODO(N3): move each entry to DELIVERED when its task lands. Until then a
# missing file is skipped; once present it is checked like any other, and
# test_pending_lock_members_are_promoted_when_delivered forces the move.
PENDING = {"rc-publish-stage.yml": "Task 4", "promote-release.yml": "Task 5"}


@pytest.mark.parametrize("name", [*DELIVERED, *PENDING])
def test_privileged_workflows_share_promotion_lock(name):
    path = WORKFLOWS / name
    if name in PENDING and not path.exists():
        pytest.skip(f"{name} is delivered by {PENDING[name]}; strict once it lands")
    assert path.exists(), f"missing privileged workflow: {name}"
    workflow = yaml.safe_load(path.read_text())
    assert workflow["concurrency"] == {
        "group": "data-olympus-promotion", "cancel-in-progress": False,
    }
    for job in workflow["jobs"].values():
        assert "concurrency" not in job, f"{name} must not override the lock per job"


@pytest.mark.parametrize("name", sorted(PENDING))
def test_pending_lock_members_are_promoted_when_delivered(name):
    assert not (WORKFLOWS / name).exists(), (
        f"{name} has landed: move it from PENDING to DELIVERED so a rename fails")
