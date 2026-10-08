"""Branch management stays inert until trusted dispatch and bot activation."""
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


PERMISSIONS = {
    "recut-release-branch.yml": {"actions": "read", "contents": "read", "pull-requests": "read"},
    "hotfix-cut.yml": {"actions": "read", "contents": "read"},
}
OLD_PATH = ("tag-release.yml", "rc-publish.yml", "set-channel.yml")
BLOCKED = "blocked: requires W9 bot identity"


@pytest.fixture(params=sorted(PERMISSIONS))
def workflow(request):
    path = WORKFLOWS / request.param
    assert path.exists(), f"missing branch-management workflow: {path.name}"
    document = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    document["__name__"] = request.param
    return document


def _step(job, prefix):
    return next(step for step in job["steps"] if step.get("name", "").startswith(prefix))


def _run(script, **env):
    return subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                          env={**os.environ, **env}, capture_output=True, text=True)


def test_dispatch_snapshots_are_required_and_main_is_trusted(workflow):
    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    for name in ("main_sha", "head_sha", "base_sha", "hotfix_sha"):
        assert inputs[name]["required"] == "true"
        assert inputs[name]["type"] == "string"
    assert set(workflow["jobs"]) == {"plan", "apply"}
    for job in workflow["jobs"].values():
        assert job["permissions"] == PERMISSIONS[workflow["__name__"]]
        checkout = next(step for step in job["steps"] if step.get("uses", "").startswith(
            "actions/checkout@"))
        assert checkout["with"]["ref"] == "${{ github.sha }}"
        assert checkout["with"]["fetch-depth"] == "0"
        assert checkout["with"]["persist-credentials"] == "false"


def test_credential_lives_only_in_the_main_restricted_apply_environment(workflow):
    jobs = workflow["jobs"]
    assert jobs["apply"]["needs"] == "plan"
    assert jobs["apply"]["environment"] == "sdlc-bot"
    assert "environment" not in jobs["plan"]
    assert "secrets." not in str(workflow.get("env", {}))
    assert "SDLC_BOT_TOKEN }}" not in str(workflow), "no static bot token secret"
    for name, job in jobs.items():
        if name != "apply":
            assert "secrets." not in str(job), f"{name} must not reference any secret"
    apply = jobs["apply"]
    assert "secrets." not in str(apply.get("env", {}))
    gate, mint = apply["steps"][0], _step(apply, "Mint ")
    for step in apply["steps"]:
        if step is not gate and step is not mint:
            assert "secrets." not in str(step), step.get("name")


def test_app_token_is_minted_per_run_with_least_privilege(workflow):
    steps = workflow["jobs"]["apply"]["steps"]
    mint, apply = _step(workflow["jobs"]["apply"], "Mint "), _step(
        workflow["jobs"]["apply"], "Apply ")
    assert mint["uses"].startswith("actions/create-github-app-token@")
    assert mint["id"] == "app-token"
    assert mint["with"]["app-id"] == "${{ secrets.SDLC_APP_ID }}"
    assert mint["with"]["private-key"] == "${{ secrets.SDLC_APP_PRIVATE_KEY }}"
    assert mint["with"]["owner"] == "${{ github.repository_owner }}"
    assert mint["with"]["repositories"] == "${{ github.event.repository.name }}"
    permissions = {k: v for k, v in mint["with"].items() if k.startswith("permission-")}
    assert permissions == {"permission-contents": "write"}
    # Minted immediately before the only step that consumes it.
    assert steps.index(apply) == steps.index(mint) + 1 == len(steps) - 1
    assert apply["env"]["SDLC_BOT_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    for step in steps[:steps.index(mint)]:
        assert "app-token" not in str(step)
    assert "app-token" not in str(workflow["jobs"]["plan"])


@pytest.mark.parametrize(("job", "ref", "pipeline", "app_id", "app_key", "allowed"), [
    ("plan", "refs/heads/main", "enabled", "", "", True),
    ("plan", "refs/heads/main", "", "", "", False),
    ("plan", "refs/heads/release/new", "enabled", "", "", False),
    ("plan", "refs/tags/main", "enabled", "", "", False),
    ("apply", "refs/heads/main", "enabled", "true", "true", True),
    ("apply", "refs/heads/main", "", "true", "true", False),
    ("apply", "refs/heads/main", "enabled", "false", "true", False),
    ("apply", "refs/heads/main", "enabled", "true", "false", False),
    ("apply", "refs/heads/main", "enabled", "", "", False),
    ("apply", "refs/heads/release/new", "enabled", "true", "true", False),
])
def test_activation_gate_fails_closed(workflow, job, ref, pipeline, app_id, app_key, allowed):
    step = workflow["jobs"][job]["steps"][0]
    assert step["name"].startswith("Require trusted main")
    assert step["env"]["SDLC_PIPELINE"] == "${{ vars.SDLC_PIPELINE }}"
    if job == "apply":
        assert step["env"]["HAS_APP_ID"] == "${{ secrets.SDLC_APP_ID != '' }}"
        assert step["env"]["HAS_APP_KEY"] == "${{ secrets.SDLC_APP_PRIVATE_KEY != '' }}"
    result = _run(step["run"], GITHUB_REF=ref, SDLC_PIPELINE=pipeline,
                  HAS_APP_ID=app_id, HAS_APP_KEY=app_key)
    assert (result.returncode == 0) == allowed
    if not allowed and ref == "refs/heads/main":
        assert result.stderr.strip() == BLOCKED


def _token_guard(workflow):
    body = _step(workflow["jobs"]["apply"], "Apply ")["run"]
    return body[:body.index("unset ACTIONS_TOKEN")]


@pytest.mark.parametrize(("token", "allowed"), [
    ("ghs_installationtoken", True),
    ("", False),
    ("ghp_personaltoken", False),
    ("github_pat_finegrained", False),
    ("gho_oauthtoken", False),
    ("ghs_same", False),
])
def test_only_a_distinct_installation_token_is_accepted(workflow, token, allowed):
    result = _run(_token_guard(workflow), SDLC_BOT_TOKEN=token, ACTIONS_TOKEN="ghs_same")
    assert (result.returncode == 0) == allowed
    if not allowed:
        assert result.stderr.strip() == BLOCKED
    if token:
        assert token not in result.stdout + result.stderr


def test_unset_token_is_refused(workflow):
    env = {k: v for k, v in os.environ.items() if k != "SDLC_BOT_TOKEN"}
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", _token_guard(workflow)],
                            env={**env, "ACTIONS_TOKEN": "ghs_same"},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stderr.strip() == BLOCKED


def test_push_identity_is_only_the_bot_and_never_the_workflow_token(workflow):
    apply = _step(workflow["jobs"]["apply"], "Apply ")
    body = apply["run"]
    assert set(apply["env"]) == {"SDLC_BOT_TOKEN", "ACTIONS_TOKEN"}
    assert apply["env"]["ACTIONS_TOKEN"] == "${{ github.token }}"
    guard = body.index('"$SDLC_BOT_TOKEN" != ghs_*')
    header = body.index("GIT_CONFIG_VALUE_0=")
    assert guard < body.index("unset ACTIONS_TOKEN") < header
    assert '"$SDLC_BOT_TOKEN" == "$ACTIONS_TOKEN"' in body[:header]
    assert BLOCKED in body[guard:header]
    # The only credential handed to git is derived from the minted App token.
    assert '"$SDLC_BOT_TOKEN" | base64 -w 0' in body[header:]
    assert "credential.helper" in body
    assert body.index("unset SDLC_BOT_TOKEN") < body.index("sdlc_recut.py")
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_TOKEN"):
        assert f"${name}" not in body[header:]
    # No third-party tool (gh, jq, uv) runs while the credential is present.
    commands = "\n".join(line for line in body.splitlines()
                         if not line.lstrip().startswith("#"))
    for tool in ("gh ", "jq ", "uv ", "cat "):
        assert tool not in commands, tool
    assert '"$PYTHON" scripts/sdlc_recut.py' in body


def test_inputs_never_interpolate_shell_and_plan_never_applies(workflow):
    plan = _step(workflow["jobs"]["plan"], "Plan ")
    apply = _step(workflow["jobs"]["apply"], "Apply ")
    assert "--apply" not in plan["run"]
    assert apply["run"].rstrip().endswith("--apply")
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            body = step.get("run", "")
            assert "${{" not in body
            assert "GITHUB_OUTPUT" not in body
            assert "GITHUB_ENV" not in body
            commands = "\n".join(line for line in body.splitlines()
                                 if not line.lstrip().startswith("#"))
            assert "git config" not in commands
            if "sdlc_recut.py" in body:
                for flag in ("--expected-main", "--expected-head", "--expected-base",
                             "--expected-hotfix", "--old-path-runs-json"):
                    assert flag in body


def test_old_path_runs_are_listed_read_only_in_both_jobs(workflow):
    plan = _step(workflow["jobs"]["plan"], "Plan ")
    snapshot = _step(workflow["jobs"]["apply"], "Snapshot ")
    for step in (plan, snapshot):
        assert step["env"] == {"GH_TOKEN": "${{ github.token }}"}
        assert "--method GET" in step["run"]
        for name in OLD_PATH:
            assert name in step["run"]
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            assert status in step["run"]


def test_recut_lists_every_open_pr_read_only(workflow):
    if "tag" not in workflow["on"]["workflow_dispatch"]["inputs"]:
        return
    plan = _step(workflow["jobs"]["plan"], "Plan ")
    snapshot = _step(workflow["jobs"]["apply"], "Snapshot ")
    for step in (plan, snapshot):
        assert "gh api --method GET --paginate --slurp" in step["run"]
        assert "base=release/new" in step["run"]
        assert "state=open" in step["run"]
    assert "--pull-requests-json" in plan["run"]
    assert "--pull-requests-json" in _step(workflow["jobs"]["apply"], "Apply ")["run"]


@pytest.mark.xfail(strict=True, reason="TODO(C6): no verified full commit SHA was available "
                   "offline for actions/checkout, astral-sh/setup-uv and "
                   "actions/create-github-app-token; pin them, then drop this marker")
def test_apply_job_actions_are_pinned_by_full_commit_sha(workflow):
    for step in workflow["jobs"]["apply"]["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
