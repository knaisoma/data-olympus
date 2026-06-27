"""Git-hook provider: post-commit warn (default) and pre-commit block (--block)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"
BEGIN = "# >>> data-olympus enforce (managed) >>>"


def _run(*args: str):
    return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True)


def _init(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    return repo


def test_git_install_default_is_post_commit_warn(tmp_path):
    repo = _init(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"
    r = _run("install", "--agent", "git", "--settings", str(hook))
    assert r.returncode == 0, r.stderr
    assert hook.exists() and os.access(hook, os.X_OK)
    body = hook.read_text()
    assert BEGIN in body
    assert "data-olympus report" in body
    assert "warn" in (r.stdout + r.stderr).lower() or "post-commit" in (r.stdout + r.stderr).lower()


def test_git_install_block_is_pre_commit(tmp_path):
    repo = _init(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    r = _run("install", "--agent", "git", "--block", "--settings", str(hook))
    assert r.returncode == 0, r.stderr
    body = hook.read_text()
    assert "--staged" in body and "--fail-on-unverified" in body


def test_git_uninstall_preserves_operator_hook(tmp_path):
    repo = _init(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho operator-hook\n")
    _run("install", "--agent", "git", "--settings", str(hook))
    _run("uninstall", "--agent", "git", "--settings", str(hook))
    body = hook.read_text()
    assert "operator-hook" in body
    assert BEGIN not in body


def test_git_status(tmp_path):
    repo = _init(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"
    _run("install", "--agent", "git", "--settings", str(hook))
    r = _run("status", "--agent", "git", "--settings", str(hook))
    assert "git: installed, tier=detect" in r.stdout
