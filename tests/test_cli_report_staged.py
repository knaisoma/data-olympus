"""--staged classifies the staged diff (for the pre-commit block hook)."""
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

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)


@contextmanager
def _stub_audit(consult_ts: float) -> Iterator[str]:
    """A tiny audit server that always returns one consult at `consult_ts`.

    It ignores the `since` query param on purpose: the recency gate must be the
    CLI's correlation window, not just a server-side filter. So if the staged
    gate still passes on a stale consult, that is a real bypass, not a stub quirk.
    """
    payload = json.dumps({
        "events": [{
            "ts": consult_ts,
            "event_type": "consult",
            "target_path": "proj",
            "agent_identity": "codex",
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
            pass  # keep test output quiet

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


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


def _stage_governed(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    (repo / "README.md").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    _git(repo, "add", "pyproject.toml")
    return repo


def _run_staged(repo: Path, endpoint: str, window: int) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "KB_ENDPOINT": endpoint}
    return subprocess.run(
        [sys.executable, "-m", "data_olympus.cli.main", "report",
         "--workspace", "proj", "--staged", "--fail-on-unverified",
         "--window-sec", str(window)],
        cwd=str(repo), capture_output=True, text=True, env=env)


def test_staged_recent_consult_passes(tmp_path):
    repo = _stage_governed(tmp_path)
    window = 3600
    with _stub_audit(consult_ts=float(int(time.time()))) as endpoint:  # consult = now
        r = _run_staged(repo, endpoint, window)
    assert r.returncode == 0, r.stderr  # recent consult -> verified


def test_staged_stale_consult_fails(tmp_path):
    repo = _stage_governed(tmp_path)
    window = 3600
    stale_ts = float(int(time.time()) - 10 * window)  # far outside the window
    with _stub_audit(consult_ts=stale_ts) as endpoint:
        r = _run_staged(repo, endpoint, window)
    assert r.returncode == 3, r.stderr  # stale consult must NOT verify the gate


def test_report_git_error_is_not_clean(tmp_path):
    # A bad --range must not read as a clean (0-governed) repo: exit 2, not 0.
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    (repo / "README.md").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    env = {**os.environ, "KB_ENDPOINT": "http://127.0.0.1:1"}
    r = subprocess.run(
        [sys.executable, "-m", "data_olympus.cli.main", "report",
         "--workspace", "proj", "--range", "does-not-exist..HEAD"],
        cwd=str(repo), capture_output=True, text=True, env=env)
    assert r.returncode == 2  # git error, not a clean repo
    assert "fatal" in r.stderr.lower() or "unknown revision" in r.stderr.lower()
