"""Branch management stays inert until trusted dispatch and bot activation."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


PERMISSIONS = {
    "recut-release-branch.yml": {"contents": "read", "pull-requests": "read"},
    "hotfix-cut.yml": {"contents": "read"},
}


@pytest.fixture(params=sorted(PERMISSIONS))
def workflow(request):
    path = WORKFLOWS / request.param
    assert path.exists(), f"missing branch-management workflow: {path.name}"
    document = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    document["__name__"] = request.param
    return document


def test_dispatch_snapshots_are_required_and_main_is_trusted(workflow):
    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    for name in ("main_sha", "head_sha", "base_sha", "hotfix_sha"):
        assert inputs[name]["required"] == "true"
        assert inputs[name]["type"] == "string"
    job = workflow["jobs"]["manage"]
    assert job["permissions"] == PERMISSIONS[workflow["__name__"]]
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith(
        "actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert checkout["with"]["fetch-depth"] == "0"
    assert checkout["with"]["persist-credentials"] == "false"


@pytest.mark.parametrize(("ref", "pipeline", "identity", "allowed"), [
    ("refs/heads/main", "enabled", "true", True),
    ("refs/heads/main", "", "true", False),
    ("refs/heads/main", "enabled", "false", False),
    ("refs/heads/release/new", "enabled", "true", False),
    ("refs/tags/main", "enabled", "true", False),
])
def test_activation_gate_fails_closed(workflow, ref, pipeline, identity, allowed):
    step = workflow["jobs"]["manage"]["steps"][0]
    assert step["name"] == "Require trusted main and W9 bot identity"
    assert step["env"]["SDLC_PIPELINE"] == "${{ vars.SDLC_PIPELINE }}"
    assert step["env"]["HAS_BOT_IDENTITY"] == "${{ secrets.SDLC_BOT_TOKEN != '' }}"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        env={**os.environ, "GITHUB_REF": ref, "SDLC_PIPELINE": pipeline,
             "HAS_BOT_IDENTITY": identity}, capture_output=True, text=True,
    )
    assert (result.returncode == 0) == allowed
    if not allowed and ref == "refs/heads/main":
        assert "blocked: requires W9 bot identity" in result.stderr


def test_inputs_never_interpolate_shell_and_write_token_is_scoped(workflow):
    job = workflow["jobs"]["manage"]
    assert "secrets." not in str(workflow.get("env", {}))
    assert "secrets." not in str(job.get("env", {}))
    steps = job["steps"]
    apply = next(step for step in steps if step["name"].startswith("Apply "))
    plan = next(step for step in steps if step["name"].startswith("Plan "))
    assert steps.index(plan) < steps.index(apply)
    assert apply["env"]["SDLC_BOT_TOKEN"] == "${{ secrets.SDLC_BOT_TOKEN }}"
    assert "--apply" in apply["run"]
    assert "GIT_CONFIG_VALUE_0" in apply["run"]
    assert "credential.helper" in apply["run"]
    assert "unset SDLC_BOT_TOKEN" in apply["run"]
    assert "--apply" not in plan["run"]
    for step in steps:
        body = step.get("run", "")
        assert "${{" not in body
        assert "GITHUB_OUTPUT" not in body
        commands = "\n".join(line for line in body.splitlines()
                             if not line.lstrip().startswith("#"))
        assert "git config" not in commands
        if step is not apply:
            assert "${{ secrets.SDLC_BOT_TOKEN }}" not in str(step)
        if "sdlc_recut.py" in body:
            for flag in ("--expected-main", "--expected-head", "--expected-base",
                         "--expected-hotfix"):
                assert flag in body


def test_recut_lists_every_open_pr_read_only(workflow):
    steps = workflow["jobs"]["manage"]["steps"]
    if "tag" not in workflow["on"]["workflow_dispatch"]["inputs"]:
        return
    for step in steps:
        if "sdlc_recut.py" not in step.get("run", ""):
            continue
        assert "gh api --method GET --paginate --slurp" in step["run"]
        assert "base=release/new" in step["run"]
        assert "state=open" in step["run"]
        assert "--pull-requests-json" in step["run"]
        assert step["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_push_identity_is_only_the_bot_and_never_the_workflow_token(workflow):
    steps = workflow["jobs"]["manage"]["steps"]
    apply = next(step for step in steps if step["name"].startswith("Apply "))
    body = apply["run"]
    assert apply["env"]["ACTIONS_TOKEN"] == "${{ github.token }}"
    guard = body.index('[[ "$SDLC_BOT_TOKEN" == "$ACTIONS_TOKEN" ]]')
    header = body.index("GIT_CONFIG_VALUE_0=")
    assert guard < body.index("unset ACTIONS_TOKEN") < header
    assert "blocked: requires W9 bot identity" in body[guard:header]
    # The only credential handed to git is derived from the bot secret.
    assert '"$SDLC_BOT_TOKEN" | base64 -w 0' in body[header:]
    assert body.index("unset SDLC_BOT_TOKEN") < body.index("sdlc_recut.py")
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_TOKEN"):
        assert f"${name}" not in body[header:]


@pytest.mark.parametrize(("bot", "allowed"), [("bot-secret", True), ("same", False)])
def test_bot_token_equal_to_workflow_token_is_refused(workflow, bot, allowed):
    body = next(step for step in workflow["jobs"]["manage"]["steps"]
                if step["name"].startswith("Apply "))["run"]
    start = body.index('test -n "$SDLC_BOT_TOKEN"')
    snippet = body[start:body.index("unset ACTIONS_TOKEN")]
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", snippet],
        env={**os.environ, "SDLC_BOT_TOKEN": bot, "ACTIONS_TOKEN": "same"},
        capture_output=True, text=True,
    )
    assert (result.returncode == 0) == allowed
    if not allowed:
        assert "blocked: requires W9 bot identity" in result.stderr
