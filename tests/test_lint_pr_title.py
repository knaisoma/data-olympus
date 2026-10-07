"""Pure logic for the PR-title Conventional Commit check (STD-U-810 §7.1)."""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from scripts.lint_pr_title import is_valid_title

_REPO = pathlib.Path(__file__).resolve().parents[1]


def test_valid_feat() -> None:
    assert is_valid_title("feat(search): add synonym expansion") is True


def test_valid_breaking() -> None:
    assert is_valid_title("feat(api)!: drop v0 endpoint") is True


def test_invalid_unknown_type() -> None:
    assert is_valid_title("update: stuff") is False


def test_invalid_no_type() -> None:
    assert is_valid_title("Add a new thing") is False


def test_invalid_empty() -> None:
    assert is_valid_title("") is False


def test_script_runs_as_direct_path_valid() -> None:
    r = subprocess.run(
        [sys.executable, "scripts/lint_pr_title.py", "feat: x"],
        cwd=_REPO, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


def test_script_runs_as_direct_path_invalid() -> None:
    r = subprocess.run(
        [sys.executable, "scripts/lint_pr_title.py", "nope: x"],
        cwd=_REPO, capture_output=True, text=True,
    )
    assert r.returncode == 1


@pytest.mark.parametrize("message,impact", [
    ("feat(api): add export", 2), ("fix: repair export", 1),
    ("perf: speed up export", 0), ("custom: lowercase type", 0),
    ("revert: feat!: replace export", 0), ("docs!: remove instructions", 3),
    ("fix: repair\n\nBREAKING CHANGE: remove API", 3),
    ("fix: repair\n\nBREAKING-CHANGE: remove API", 3),
    ("fix: repair\n\nDiscuss BREAKING CHANGE: in prose", 1),
    ("fix: repair\n\nBREAKING CHANGE: ", 1),
])
def test_standard_impact(message: str, impact: int) -> None:
    from scripts.lint_pr_title import commit_impact

    assert commit_impact(message) == impact


@pytest.mark.parametrize("message", [
    "", "Feat: export", "feat: ", "feat:  ", "feat:export",
    "feat(): export", "feat(Bad Scope): export", " feat: export",
    'Revert "feat: export"', "Merge branch 'feature'",
])
def test_malformed_message(message: str) -> None:
    from scripts.lint_pr_title import commit_impact

    with pytest.raises(ValueError, match="Malformed"):
        commit_impact(message)


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def history(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "commit", "--allow-empty", "-m", "chore: base")
    return tmp_path, git(tmp_path, "rev-parse", "HEAD")


def run_range(
    repo: pathlib.Path, base: str, title: str, body: str = "",
) -> subprocess.CompletedProcess[str]:
    import os

    return subprocess.run(
        [sys.executable, str(_REPO / "scripts/lint_pr_title.py"),
         "--base", base, "--head", "HEAD", "--repo", str(repo), "--body", body],
        env={**os.environ, "PR_TITLE": title}, capture_output=True, text=True,
    )


@pytest.mark.parametrize("message,title,valid", [
    ("feat: export", "feat: export", True),
    ("feat: export", "fix: export", False),
    ("fix: export", "docs: export", False),
    ("feat: export", "feat!: export", True),
    ("fix: export\n\nBREAKING CHANGE: remove API", "feat: export", False),
    ("fix: export\n\nBREAKING-CHANGE: remove API", "fix!: export", False),
    ("revert: feat!: export", "chore: revert export", True),
    ("bad subject", "feat: export", False),
    ("Merge branch 'fake'", "feat: export", False),
    ("fix: export", "bad title", False),
    ("fix: export", "fix: export\n\nBREAKING CHANGE: fake title", False),
])
def test_range_cli(history, message: str, title: str, valid: bool) -> None:
    repo, base = history
    git(repo, "commit", "--allow-empty", "-m", message)
    result = run_range(repo, base, title)
    assert (result.returncode == 0) is valid, result.stderr


def test_range_checks_every_commit_and_skips_real_merges(history) -> None:
    repo, base = history
    git(repo, "checkout", "-b", "feature")
    git(repo, "commit", "--allow-empty", "-m", "feat: export")
    git(repo, "checkout", "main")
    git(repo, "commit", "--allow-empty", "-m", "docs: instructions")
    git(repo, "merge", "--no-ff", "feature", "-m", "arbitrary merge subject")
    assert run_range(repo, base, "feat: export").returncode == 0
    assert run_range(repo, base, "fix: export").returncode == 1


def test_empty_commit_message_fails(history) -> None:
    repo, base = history
    git(repo, "commit", "--allow-empty", "--allow-empty-message", "-m", "")
    assert run_range(repo, base, "feat: export").returncode == 1


def test_invalid_range_fails_closed(history) -> None:
    repo, _ = history
    assert run_range(repo, "missing-ref", "feat: export").returncode != 0


def test_older_malformed_commit_is_not_hidden_by_valid_tip(history) -> None:
    repo, base = history
    git(repo, "commit", "--allow-empty", "-m", "malformed")
    bad_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "--allow-empty", "-m", "feat: export")
    result = run_range(repo, base, "feat: export")
    assert result.returncode == 1
    assert bad_sha in result.stderr


def test_revert_does_not_subtract_feature_impact(history) -> None:
    repo, base = history
    git(repo, "commit", "--allow-empty", "-m", "feat: export")
    git(repo, "commit", "--allow-empty", "-m", "revert: feat: export")
    assert run_range(repo, base, "fix: revert export").returncode == 1


@pytest.mark.parametrize("footer", ["BREAKING CHANGE", "BREAKING-CHANGE"])
@pytest.mark.parametrize("body,valid", [
    ("", False), ("BREAKING CHANGE: ", False),
    ("BREAKING-CHANGE: \t", False), ("Discuss BREAKING CHANGE: removal", False),
    ("BREAKING CHANGE: remove API", True),
    ("Notes\n\nBREAKING-CHANGE: migrate to v2", True),
])
@pytest.mark.parametrize("title", ["fix!: export", "fix: export"])
def test_squash_preserves_breaking_footer(history, footer, body, valid, title) -> None:
    repo, base = history
    git(repo, "commit", "--allow-empty", "-m", f"fix: export\n\n{footer}: remove API")
    result = run_range(repo, base, title, body)
    assert (result.returncode == 0) is valid, result.stderr
    if not valid:
        assert "nonempty breaking-change footer" in result.stderr


@pytest.mark.parametrize("subject,impact", [
    ("feat(a0._/-): export", 2), ("custom: export", 0),
    ("fix(scope)!: export", 3), ("perf: export", 0),
    ("revert: feat!: export", 0), ("feat: café", 2),
    ("feat(): export", None), ("feat(A): export", None),
    ("feat(a b): export", None), ("feat(a+b): export", None),
    ("feat(é): export", None), ("Feat: export", None),
    ("feat: ", None), ("feat:export", None),
])
def test_subject_grammar_table(subject, impact) -> None:
    from scripts.lint_pr_title import commit_impact

    if impact is None:
        with pytest.raises(ValueError, match="Malformed"):
            commit_impact(subject)
    else:
        assert commit_impact(subject) == impact


def test_git_log_decode_error_is_clear(history, monkeypatch, capsys) -> None:
    from scripts.lint_pr_title import main

    repo, base = history
    original_run = subprocess.run

    def invalid_log(args, **kwargs):
        if "log" in args:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", invalid_log)
    assert main(["--base", base, "--repo", str(repo)]) == 1
    assert "Cannot decode commit range" in capsys.readouterr().err


def test_workflow_executes_only_base_scripts() -> None:
    import yaml

    workflow = yaml.safe_load((_REPO / ".github/workflows/pr-title-lint.yml").read_text())
    # PyYAML's YAML 1.1 loader represents GitHub's `on` key as True.
    events = workflow[True]
    assert set(events) == {"pull_request"}
    assert set(events["pull_request"]["branches"]) == {"main", "release/new", "hotfix/new"}
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["lint-title"]["steps"]
    checkout = [step for step in steps if step.get("uses", "").startswith("actions/checkout@")]
    assert len(checkout) == 1
    assert checkout[0]["with"] == {
        "ref": "${{ github.event.pull_request.base.sha }}", "path": "trusted",
        "fetch-depth": 0, "persist-credentials": False,
    }
    for step in steps:
        if "run" in step:
            assert step["working-directory"] == "trusted"
            assert "${{" not in step["run"]
    lint = steps[-1]
    assert lint["env"]["PR_TITLE"] == "${{ github.event.pull_request.title }}"
    assert lint["env"]["PR_BODY"] == "${{ github.event.pull_request.body }}"
    assert '--body "$PR_BODY"' in lint["run"]
    assert lint["run"].startswith(".venv/bin/python -I scripts/lint_pr_title.py --base ")
    assert "uv run" not in lint["run"]


def test_ci_includes_integration_branches_without_narrowing_prs() -> None:
    import yaml

    workflow = yaml.safe_load((_REPO / ".github/workflows/ci.yaml").read_text())
    events = workflow[True]
    assert set(events["push"]["branches"]) == {"main", "release/new", "hotfix/new"}
    assert "pull_request" in events
    assert events["pull_request"] is None
