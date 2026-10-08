"""Tests for the `0.0.0+unreleased` placeholder rule (scripts/placeholder_version.py).

Both directions are covered: the placeholder is accepted on pull requests into
release/new and hotfix/new, and into main only as a squash whose tree equals
origin/release/new or origin/hotfix/new (or when main already declares it
unchanged). It is refused on feature and stacked bases, case variants and an
unknown context; every other local or non X.Y.Z version is refused everywhere;
a concrete version bump keeps its previous behaviour. The last group runs the
real `version-free-guard` shell from ci.yaml, under GitHub's default
`bash -e`, against throwaway git repositories with the registry check stubbed,
so no network is used.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.placeholder_version import (
    MAIN_BRANCH,
    PLACEHOLDER_BRANCHES,
    PLACEHOLDER_VERSION,
    branch_context,
    guard_decision,
    main,
    placeholder_permitted,
    server_json_placeholder_permitted,
)

_ROOT = Path(__file__).resolve().parents[1]
_OUTSIDE = [
    "feature/foo",
    "release/new-old",
    "hotfix/new2",
    "release",
    "Release/New",
    "RELEASE/NEW",
    "hotfix/New",
    "Main",
    "",
]
_T_RELEASE = "a" * 40
_T_HOTFIX = "b" * 40
_T_OTHER = "c" * 40
_TREES = {"release/new": _T_RELEASE, "hotfix/new": _T_HOTFIX}


def test_placeholder_literal_and_branches_are_the_documented_ones() -> None:
    assert PLACEHOLDER_VERSION == "0.0.0+unreleased"
    assert frozenset({"release/new", "hotfix/new"}) == PLACEHOLDER_BRANCHES
    assert MAIN_BRANCH == "main"
    doc = (_ROOT / "docs" / "releases" / "placeholder-version.md").read_text()
    assert PLACEHOLDER_VERSION in doc
    for rule in (".rules/versioning.md", ".rules/release-planning.md"):
        text = (_ROOT / rule).read_text()
        assert PLACEHOLDER_VERSION in text
        assert "docs/releases/placeholder-version.md" in text
    assert "placeholder-version.md" in (_ROOT / "CONTRIBUTING.md").read_text()


@pytest.mark.parametrize("branch", sorted(PLACEHOLDER_BRANCHES))
def test_placeholder_permitted_on_cycle_branches(branch: str) -> None:
    assert placeholder_permitted(branch)
    assert server_json_placeholder_permitted(branch)


def test_server_json_context_also_permits_main_but_guard_context_does_not() -> None:
    assert server_json_placeholder_permitted("main")
    assert not placeholder_permitted("main")


@pytest.mark.parametrize("branch", [*_OUTSIDE, None])
def test_placeholder_refused_elsewhere(branch: str | None) -> None:
    assert not placeholder_permitted(branch)
    assert not server_json_placeholder_permitted(branch)


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
    # Fails closed even when the base already carries the placeholder and the
    # head tree equals a cycle branch: only main has the tree and unchanged rules.
    decision = guard_decision(
        PLACEHOLDER_VERSION, base_version, base_ref, head_tree=_T_RELEASE, cycle_trees=_TREES
    )
    assert decision.action == "fail"


@pytest.mark.parametrize(("head_tree", "branch"), [(_T_RELEASE, "release/new"),
                                                   (_T_HOTFIX, "hotfix/new")])
def test_guard_accepts_tree_equal_cycle_squash_into_main(head_tree: str, branch: str) -> None:
    decision = guard_decision(
        PLACEHOLDER_VERSION, "0.11.0", "main", head_tree=head_tree, cycle_trees=_TREES
    )
    assert decision.action == "skip"
    assert f"origin/{branch}" in decision.reason


@pytest.mark.parametrize(
    ("head_tree", "trees"),
    [
        (_T_OTHER, _TREES),
        (None, _TREES),
        ("", _TREES),
        ("not-a-tree", {"release/new": "not-a-tree"}),
        (_T_RELEASE, {"release/new": None, "hotfix/new": _T_HOTFIX}),
        (_T_RELEASE, {}),
        (None, {"release/new": None, "hotfix/new": None}),
    ],
)
def test_guard_refuses_other_placeholder_into_main(head_tree: str | None, trees: dict) -> None:
    decision = guard_decision(
        PLACEHOLDER_VERSION, "0.11.0", "main", head_tree=head_tree, cycle_trees=trees
    )
    assert decision.action == "fail"


def test_guard_accepts_unchanged_placeholder_on_main() -> None:
    decision = guard_decision(
        PLACEHOLDER_VERSION, PLACEHOLDER_VERSION, "main", head_tree=_T_OTHER, cycle_trees={}
    )
    assert decision.action == "skip"
    assert "unchanged" in decision.reason


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
    kwargs = {"head_tree": _T_RELEASE, "cycle_trees": _TREES}
    assert guard_decision(version, "0.11.0", base_ref, **kwargs).action == "fail"
    assert guard_decision(version, version, base_ref, **kwargs).action == "fail"


@pytest.mark.parametrize("base_ref", ["main", "release/new", "hotfix/new", "feature/foo"])
def test_guard_concrete_versions_keep_previous_behaviour(base_ref: str) -> None:
    assert guard_decision("0.11.0", "0.11.0", base_ref).action == "skip"
    assert guard_decision("0.12.0", "0.11.0", base_ref).action == "check"
    assert guard_decision("0.12.0", PLACEHOLDER_VERSION, base_ref).action == "check"


def test_cli_prints_decision_and_exits_nonzero_on_fail(capsys: pytest.CaptureFixture[str]) -> None:
    args = ["guard", "--head-version", PLACEHOLDER_VERSION, "--base-version", "0.11.0"]
    assert main([*args, "--base-ref", "release/new"]) == 0
    assert capsys.readouterr().out == "skip\n"
    assert main([*args, "--base-ref", "feature/foo"]) == 1
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


def test_guard_step_takes_refs_from_env_and_decides_before_registry() -> None:
    step = _guard_step()
    assert "shell" not in step  # GitHub's default `bash -e {0}`, as the harness runs it
    assert step["env"]["BASE_REF"] == "${{ github.base_ref }}"
    assert step["env"]["PR_HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    script = step["run"]
    assert "${{" not in script
    decide = script.index("scripts/placeholder_version.py guard")
    assert decide < script.index('TAG="v$HEAD_VERSION"')
    assert decide < script.index("scripts/check_version_free.py")
    assert '--base-ref "$BASE_REF"' in script
    assert '--head-commit "$PR_HEAD_SHA"' in script


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
    command = ["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args]
    return subprocess.run(command, cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def _pyproject(repo: Path, version: str) -> None:
    (repo / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _new_repo(tmp_path: Path, base_ref: str, base_version: str,
              decision_stub: str | None = None) -> Path:
    """A fresh `git init` fixture whose origin/<base_ref> declares base_version."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("should_tag.py", "placeholder_version.py"):
        shutil.copy(_ROOT / "scripts" / name, repo / "scripts" / name)
    if decision_stub is not None:
        (repo / "scripts" / "placeholder_version.py").write_text(decision_stub)
    # Registry stub: records the call instead of touching the network.
    (repo / "scripts" / "check_version_free.py").write_text(
        "import sys\nprint('REGISTRY-CHECK', *sys.argv[1:])\n"
    )
    _git(repo, "init", "-q")
    _pyproject(repo, base_version)
    base = _commit(repo, "base")
    _git(repo, "update-ref", f"refs/remotes/origin/{base_ref}", base)
    return repo


def _run(repo: Path, base_ref: str, head_sha: str | None = None
         ) -> subprocess.CompletedProcess[str]:
    """Run the step as GitHub does with no `shell:`: `bash -e {0}` on a script file."""
    script = repo.parent / "step.sh"
    script.write_text(_guard_step()["run"])
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_", "BASH_ENV", "ENV"))}
    env["BASE_REF"] = base_ref
    env["PR_HEAD_SHA"] = head_sha if head_sha is not None else _git(repo, "rev-parse", "HEAD")
    return subprocess.run(["bash", "-e", str(script)], cwd=repo, env=env,
                          capture_output=True, text=True, check=False)


def _run_guard(tmp_path: Path, base_ref: str, base_version: str, head_version: str
               ) -> subprocess.CompletedProcess[str]:
    repo = _new_repo(tmp_path, base_ref, base_version)
    _pyproject(repo, head_version)
    _commit(repo, "head")
    return _run(repo, base_ref)


@pytest.mark.parametrize("base_ref", sorted(PLACEHOLDER_BRANCHES))
def test_shell_accepts_placeholder_into_cycle_branch(tmp_path: Path, base_ref: str) -> None:
    result = _run_guard(tmp_path, base_ref, "0.11.0", PLACEHOLDER_VERSION)
    assert result.returncode == 0, result.stderr
    assert "REGISTRY-CHECK" not in result.stdout
    assert "placeholder" in result.stderr


@pytest.mark.parametrize("base_ref", ["main", "feature/foo", "Release/New"])
def test_shell_refuses_placeholder_elsewhere(tmp_path: Path, base_ref: str) -> None:
    result = _run_guard(tmp_path, base_ref, "0.11.0", PLACEHOLDER_VERSION)
    assert result.returncode != 0
    assert "REGISTRY-CHECK" not in result.stdout


def test_shell_refuses_unchanged_placeholder_on_feature_base(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, "feature/foo", PLACEHOLDER_VERSION, PLACEHOLDER_VERSION)
    assert result.returncode != 0
    assert "only accepted" in result.stderr


def _cycle_fixture(tmp_path: Path) -> tuple[Path, str]:
    """main at 0.11.0; a cycle branch commit carrying the placeholder and a change.

    Returns the repository and the cycle head commit; the caller decides which
    remote cycle refs exist.
    """
    repo = _new_repo(tmp_path, "main", "0.11.0")
    main_sha = _git(repo, "rev-parse", "HEAD")
    _pyproject(repo, PLACEHOLDER_VERSION)
    _commit(repo, "chore: placeholder")
    (repo / "feature.txt").write_text("cycle work\n")
    cycle = _commit(repo, "feat: cycle work")
    _git(repo, "checkout", "-q", "--detach", main_sha)
    return repo, cycle


def _squash_of(repo: Path, cycle: str, extra: bool = False) -> str:
    """A new commit on main whose tree is the cycle tree (a squash), optionally plus a file."""
    _git(repo, "checkout", "-q", cycle, "--", ".")
    if extra:
        (repo / "extra.txt").write_text("not reviewed\n")
    return _commit(repo, "release: 0.12.0")


@pytest.mark.parametrize("branch", ["release/new", "hotfix/new"])
def test_shell_accepts_tree_equal_squash_into_main(tmp_path: Path, branch: str) -> None:
    repo, cycle = _cycle_fixture(tmp_path)
    _git(repo, "update-ref", f"refs/remotes/origin/{branch}", cycle)
    squash = _squash_of(repo, cycle)
    assert squash != cycle  # a different commit with the same tree
    result = _run(repo, "main", squash)
    assert result.returncode == 0, result.stderr
    assert f"origin/{branch}" in result.stderr
    assert "REGISTRY-CHECK" not in result.stdout


def test_shell_accepts_later_hotfix_squash_with_release_new_elsewhere(tmp_path: Path) -> None:
    repo, cycle = _cycle_fixture(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/hotfix/new", cycle)
    other = _git(repo, "rev-parse", "HEAD")  # release/new on a different tree
    _git(repo, "update-ref", "refs/remotes/origin/release/new", other)
    result = _run(repo, "main", _squash_of(repo, cycle))
    assert result.returncode == 0, result.stderr
    assert "origin/hotfix/new" in result.stderr


@pytest.mark.parametrize("branch", ["release/new", "hotfix/new"])
def test_shell_refuses_cycle_tree_plus_extra_file(tmp_path: Path, branch: str) -> None:
    repo, cycle = _cycle_fixture(tmp_path)
    _git(repo, "update-ref", f"refs/remotes/origin/{branch}", cycle)
    result = _run(repo, "main", _squash_of(repo, cycle, extra=True))
    assert result.returncode != 0
    assert "may enter main only" in result.stderr


def test_shell_refuses_other_main_pr_introducing_placeholder(tmp_path: Path) -> None:
    repo, cycle = _cycle_fixture(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/release/new", cycle)
    _pyproject(repo, PLACEHOLDER_VERSION)
    result = _run(repo, "main", _commit(repo, "chore: sneak the placeholder in"))
    assert result.returncode != 0
    assert "may enter main only" in result.stderr


def test_shell_fails_closed_when_cycle_ref_is_missing(tmp_path: Path) -> None:
    repo, cycle = _cycle_fixture(tmp_path)  # no origin/release/new or hotfix/new ref
    result = _run(repo, "main", _squash_of(repo, cycle))
    assert result.returncode != 0
    assert "may enter main only" in result.stderr


def test_shell_fails_closed_without_head_commit(tmp_path: Path) -> None:
    repo, cycle = _cycle_fixture(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/release/new", cycle)
    _squash_of(repo, cycle)
    result = _run(repo, "main", "")
    assert result.returncode != 0


def test_shell_accepts_unchanged_placeholder_on_main(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path, "main", PLACEHOLDER_VERSION)  # main already declares it
    (repo / "docs.txt").write_text("a later change\n")
    result = _run(repo, "main", _commit(repo, "docs: later change"))
    assert result.returncode == 0, result.stderr
    assert "unchanged" in result.stderr
    assert "REGISTRY-CHECK" not in result.stdout


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


@pytest.mark.parametrize("output", ["bogus", "", "skip check"])
def test_shell_fails_closed_on_unexpected_decision(tmp_path: Path, output: str) -> None:
    stub = f"print({output!r})\n"
    repo = _new_repo(tmp_path, "main", "0.11.0", decision_stub=stub)
    _pyproject(repo, "0.12.0")
    result = _run(repo, "main", _commit(repo, "head"))
    assert result.returncode != 0
    assert "unexpected version guard decision" in result.stderr
    assert "REGISTRY-CHECK" not in result.stdout
