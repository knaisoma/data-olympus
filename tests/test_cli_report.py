"""Integration tests for `data-olympus report` against a temp git repo + stub audit."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                   capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    (repo / "README.md").write_text("# proj\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init readme")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add deps")  # governed
    return repo


def _run_report(repo: Path, *args: str, endpoint: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "KB_ENDPOINT": endpoint}
    return subprocess.run(
        [sys.executable, "-m", "data_olympus.cli.main", "report",
         "--workspace", "proj", "--range", "HEAD~1..HEAD", *args],
        cwd=str(repo), capture_output=True, text=True, env=env,
    )


def test_report_flags_unverified_governed_commit(tmp_path):
    repo = _make_repo(tmp_path)
    # Audit endpoint that returns NO consults -> the governed commit is unverified.
    r = _run_report(repo, "--json", endpoint="http://127.0.0.1:1")  # unreachable audit
    assert r.returncode == 0, r.stderr
    body = json.loads(r.stdout)
    assert body["total_governed"] == 1
    # audit unreachable -> consult state unknown; the commit is reported, not crashed
    assert body["audit_reachable"] is False


def test_report_fail_on_unverified_exit_code(tmp_path):
    repo = _make_repo(tmp_path)
    r = _run_report(repo, "--fail-on-unverified", endpoint="http://127.0.0.1:1")
    assert r.returncode == 3  # unverified governed change present
