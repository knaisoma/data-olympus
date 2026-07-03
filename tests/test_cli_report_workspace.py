"""Default workspace resolution for `data-olympus report` is worktree-invariant.

Regression: the pre-commit gate runs `report --staged` with no `--workspace`, which
defaulted to `Path.cwd().name` -- the *worktree* directory basename. That never
matched the consult recorded for the repo, so a per-session consult could not clear
the commit gate from inside a linked git worktree. The default must resolve to the
main-worktree basename, identical from the main checkout and any linked worktree,
so the same key is used by the pre-tool gate, `kb_consult`, and this report.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

from data_olympus.cli.report_cmd import resolve_default_workspace

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)


def _repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "mainrepo"
    main.mkdir()
    _git(main, "init", "--initial-branch=main")
    (main / "README.md").write_text("x")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "init")
    wt = tmp_path / "linked"  # deliberately a different basename than the repo
    _git(main, "worktree", "add", "-b", "wt", str(wt))
    return main, wt


def test_default_workspace_from_main_checkout(tmp_path: Path) -> None:
    main, _ = _repo_with_worktree(tmp_path)
    assert resolve_default_workspace(str(main)) == "mainrepo"


def test_default_workspace_from_linked_worktree(tmp_path: Path) -> None:
    # The bug returned "linked" (the worktree dir basename). It must return the
    # main repo basename so it matches the consult recorded for the repo.
    _, wt = _repo_with_worktree(tmp_path)
    assert resolve_default_workspace(str(wt)) == "mainrepo"


def test_default_workspace_non_git_falls_back_to_basename(tmp_path: Path) -> None:
    d = tmp_path / "plain"
    d.mkdir()
    assert resolve_default_workspace(str(d)) == "plain"


def test_default_workspace_bare_repo_uses_first_nonbare_worktree(tmp_path: Path) -> None:
    # A worktree attached to a BARE repo: `git worktree list` lists the bare git
    # dir first (marked `bare`), which is not a real checkout. The key must be the
    # actual worktree basename ("work"), not the bare repo name ("origin.git").
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "--initial-branch=main")
    (src / "README.md").write_text("x")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "init")
    _git(tmp_path, "clone", "--bare", "src", "origin.git")
    work = tmp_path / "work"
    _git(tmp_path / "origin.git", "worktree", "add", str(work), "main")
    assert resolve_default_workspace(str(work)) == "work"


@contextmanager
def _stub_audit(consult_ts: float, target_path: str) -> Iterator[str]:
    payload = json.dumps({
        "events": [{
            "ts": consult_ts,
            "event_type": "consult",
            "target_path": target_path,
            "agent_identity": "claude-code",
            "source_session": "s",
        }],
    }).encode()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_staged_gate_clears_from_worktree_with_repo_consult(tmp_path: Path) -> None:
    # End-to-end: a governed change staged inside a linked worktree, with a recent
    # consult recorded for the MAIN repo basename, must verify without --workspace.
    main, wt = _repo_with_worktree(tmp_path)
    (wt / "pyproject.toml").write_text("[project]\nname='x'\n")
    _git(wt, "add", "pyproject.toml")
    with _stub_audit(float(int(time.time())), target_path="mainrepo") as endpoint:
        env = {**os.environ, "KB_ENDPOINT": endpoint}
        r = subprocess.run(
            [sys.executable, "-m", "data_olympus.cli.main", "report",
             "--staged", "--fail-on-unverified"],
            cwd=str(wt), capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr  # consult on the repo key clears the gate
