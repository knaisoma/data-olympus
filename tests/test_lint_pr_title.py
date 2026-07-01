"""Pure logic for the PR-title Conventional Commit check (STD-U-810 §7.1)."""
from __future__ import annotations

import pathlib
import subprocess
import sys

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
