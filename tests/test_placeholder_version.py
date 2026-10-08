"""Tests for the `0.0.0+unreleased` placeholder rule (scripts/placeholder_version.py).

Both directions are covered: the placeholder is accepted only for the
release/new and hotfix/new cycle, refused on main, feature branches and an
unknown context, every other local or non X.Y.Z version is refused everywhere,
and a concrete version bump keeps its previous behaviour. The last group runs
the real `version-free-guard` shell from ci.yaml against throwaway git
repositories with the registry check stubbed, so no network is used.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.placeholder_version import (
    PLACEHOLDER_BRANCHES,
    PLACEHOLDER_VERSION,
    branch_context,
    guard_decision,
    main,
    placeholder_permitted,
)

_ROOT = Path(__file__).resolve().parents[1]
_OUTSIDE = ["main", "feature/foo", "release/new-old", "hotfix/new2", "release", ""]


def test_placeholder_literal_and_branches_are_the_documented_ones() -> None:
    assert PLACEHOLDER_VERSION == "0.0.0+unreleased"
    assert frozenset({"release/new", "hotfix/new"}) == PLACEHOLDER_BRANCHES
    doc = (_ROOT / "docs" / "releases" / "placeholder-version.md").read_text()
    assert PLACEHOLDER_VERSION in doc
    for rule in (".rules/versioning.md", ".rules/release-planning.md"):
        assert PLACEHOLDER_VERSION in (_ROOT / rule).read_text()


@pytest.mark.parametrize("branch", sorted(PLACEHOLDER_BRANCHES))
def test_placeholder_permitted_on_cycle_branches(branch: str) -> None:
    assert placeholder_permitted(branch)


@pytest.mark.parametrize("branch", [*_OUTSIDE, None])
def test_placeholder_refused_elsewhere(branch: str | None) -> None:
    assert not placeholder_permitted(branch)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"GITHUB_BASE_REF": "release/new", "GITHUB_REF_NAME": "12/merge"}, "release/new"),
        ({"GITHUB_BASE_REF": "main", "GITHUB_REF_NAME": "12/merge"}, "main"),
        ({"GITHUB_BASE_REF": "", "GITHUB_REF_NAME": "hotfix/new"}, "hotfix/new"),
        ({"GITHUB_REF_NAME": "main"}, "main"),
        ({"GITHUB_HEAD_REF": "release/new"}, None),
        ({}, None),
    ],
)
def test_branch_context_prefers_the_pull_request_base(env: dict, expected: str | None) -> None:
    assert branch_context(env) == expected


@pytest.mark.parametrize("base_ref", sorted(PLACEHOLDER_BRANCHES))
@pytest.mark.parametrize("base_version", ["0.11.0", PLACEHOLDER_VERSION])
def test_guard_skips_placeholder_into_cycle_branches(base_ref: str, base_version: str) -> None:
    decision = guard_decision(PLACEHOLDER_VERSION, base_version, base_ref)
    assert decision.action == "skip"
    assert "placeholder" in decision.reason


@pytest.mark.parametrize("base_ref", _OUTSIDE)
@pytest.mark.parametrize("base_version", ["0.11.0", PLACEHOLDER_VERSION])
def test_guard_fails_placeholder_elsewhere(base_ref: str, base_version: str) -> None:
    # Fails closed even when the base already carries the placeholder, so an
    # unchanged placeholder cannot slip into main or a stacked branch.
    assert guard_decision(PLACEHOLDER_VERSION, base_version, base_ref).action == "fail"


@pytest.mark.parametrize("base_ref", ["release/new", "hotfix/new", "main", "feature/foo"])
@pytest.mark.parametrize(
    "version",
    [
        "0.0.0+other",
        "0.12.0+unreleased",
        "0.0.0+UNRELEASED",
        "0.12.0rc1",
        "0.12.0-rc.1",
        "v0.12.0",
        "",
    ],
)
def test_guard_refuses_other_odd_versions_everywhere(base_ref: str, version: str) -> None:
    assert guard_decision(version, "0.11.0", base_ref).action == "fail"
    assert guard_decision(version, version, base_ref).action == "fail"


@pytest.mark.parametrize("base_ref", ["main", "release/new", "hotfix/new", "feature/foo"])
def test_guard_concrete_versions_keep_previous_behaviour(base_ref: str) -> None:
    assert guard_decision("0.11.0", "0.11.0", base_ref).action == "skip"
    assert guard_decision("0.12.0", "0.11.0", base_ref).action == "check"
    assert guard_decision("0.12.0", PLACEHOLDER_VERSION, base_ref).action == "check"


def test_cli_prints_decision_and_exits_nonzero_on_fail(capsys: pytest.CaptureFixture[str]) -> None:
    args = ["guard", "--head-version", PLACEHOLDER_VERSION, "--base-version", "0.11.0"]
    assert main([*args, "--base-ref", "release/new"]) == 0
    assert capsys.readouterr().out == "skip\n"
    assert main([*args, "--base-ref", "main"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "only accepted" in captured.err
    assert main(["guard", "--head-version", "0.12.0", "--base-version", "0.11.0",
                 "--base-ref", "main"]) == 0
    assert capsys.readouterr().out == "check\n"


# --- the real ci.yaml version-free-guard shell, offline -------------------------

def _guard_job() -> dict:
    doc = yaml.safe_load((_ROOT / ".github" / "workflows" / "ci.yaml").read_text())
    job: dict = doc["jobs"]["version-free-guard"]
    return job


def _guard_step() -> dict:
    return next(s for s in _guard_job()["steps"] if "run" in s)


def test_guard_step_takes_base_ref_from_env_and_decides_before_registry() -> None:
    step = _guard_step()
    assert step["env"]["BASE_REF"] == "${{ github.base_ref }}"
    script = step["run"]
    assert "${{" not in script
    decide = script.index("scripts/placeholder_version.py guard")
    assert decide < script.index('TAG="v$HEAD_VERSION"')
    assert decide < script.index("scripts/check_version_free.py")
    assert '--base-ref "$BASE_REF"' in script


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    for key in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        env.pop(key, None)
    command = ["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args]
    return subprocess.run(command, cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout


def _run_guard(tmp_path: Path, base_ref: str, base_version: str, head_version: str
               ) -> subprocess.CompletedProcess[str]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("should_tag.py", "placeholder_version.py"):
        shutil.copy(_ROOT / "scripts" / name, repo / "scripts" / name)
    # Registry stub: records the call instead of touching the network.
    (repo / "scripts" / "check_version_free.py").write_text(
        "import sys\nprint('REGISTRY-CHECK', *sys.argv[1:])\n"
    )
    _git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{base_version}"\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "update-ref", f"refs/remotes/origin/{base_ref}", "HEAD")
    (repo / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{head_version}"\n')
    _git(repo, "commit", "--allow-empty", "-qam", "head")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["BASE_REF"] = base_ref
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _guard_step()["run"]],
        cwd=repo, env=env, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("base_ref", sorted(PLACEHOLDER_BRANCHES))
def test_shell_accepts_placeholder_into_cycle_branch(tmp_path: Path, base_ref: str) -> None:
    result = _run_guard(tmp_path, base_ref, "0.11.0", PLACEHOLDER_VERSION)
    assert result.returncode == 0, result.stderr
    assert "REGISTRY-CHECK" not in result.stdout
    assert "placeholder" in result.stderr


@pytest.mark.parametrize("base_ref", ["main", "feature/foo"])
@pytest.mark.parametrize("base_version", ["0.11.0", PLACEHOLDER_VERSION])
def test_shell_refuses_placeholder_elsewhere(
    tmp_path: Path, base_ref: str, base_version: str
) -> None:
    result = _run_guard(tmp_path, base_ref, base_version, PLACEHOLDER_VERSION)
    assert result.returncode != 0
    assert "REGISTRY-CHECK" not in result.stdout
    assert "only accepted" in result.stderr


@pytest.mark.parametrize("base_ref", ["release/new", "main"])
def test_shell_refuses_other_local_version(tmp_path: Path, base_ref: str) -> None:
    result = _run_guard(tmp_path, base_ref, "0.11.0", "0.0.0+other")
    assert result.returncode != 0
    assert "REGISTRY-CHECK" not in result.stdout


@pytest.mark.parametrize("base_ref", ["main", "release/new"])
def test_shell_concrete_bump_reaches_registry_check(tmp_path: Path, base_ref: str) -> None:
    result = _run_guard(tmp_path, base_ref, "0.11.0", "0.12.0")
    assert result.returncode == 0, result.stderr
    assert "REGISTRY-CHECK --version 0.12.0" in result.stdout


def test_shell_unchanged_concrete_version_skips(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, "main", "0.11.0", "0.11.0")
    assert result.returncode == 0, result.stderr
    assert "REGISTRY-CHECK" not in result.stdout
    assert "unchanged" in result.stderr
