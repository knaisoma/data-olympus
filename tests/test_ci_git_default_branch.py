"""Regression guard for the CI default-branch trap.

GitHub runners leave ``init.defaultBranch`` unset and fall back to ``master``, so
an unpinned ``git init`` followed by ``git push origin main`` fails with
"src refspec main does not match any". The ``_force_git_default_branch_main``
autouse fixture in ``conftest.py`` pins the default to ``main`` for any subprocess
that inherits ``os.environ``. This test fails on such a runner if that guard is
removed.
"""
from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def test_unpinned_git_init_defaults_to_main(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e.com",
    }
    subprocess.run(["git", "init"], cwd=repo, check=True, env=env)
    head = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert head.stdout.strip() == "main"
