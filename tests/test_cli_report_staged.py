"""--staged classifies the staged diff (for the pre-commit block hook)."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)


def test_staged_governed_change_fails(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    (repo / "README.md").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    # stage a governed file but do not commit
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    _git(repo, "add", "pyproject.toml")
    env = {**os.environ, "KB_ENDPOINT": "http://127.0.0.1:1"}  # audit unreachable
    r = subprocess.run(
        [sys.executable, "-m", "data_olympus.cli.main", "report",
         "--workspace", "proj", "--staged", "--fail-on-unverified"],
        cwd=str(repo), capture_output=True, text=True, env=env)
    assert r.returncode == 3  # staged governed change, no consult -> fail


def test_staged_non_governed_change_passes(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    (repo / "README.md").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    (repo / "notes.txt").write_text("hello")
    _git(repo, "add", "notes.txt")
    env = {**os.environ, "KB_ENDPOINT": "http://127.0.0.1:1"}
    r = subprocess.run(
        [sys.executable, "-m", "data_olympus.cli.main", "report",
         "--workspace", "proj", "--staged", "--fail-on-unverified"],
        cwd=str(repo), capture_output=True, text=True, env=env)
    assert r.returncode == 0  # nothing governed staged
