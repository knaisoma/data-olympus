"""--emit-events POSTs a gate_bypass per unverified governed commit."""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class _Handler(http.server.BaseHTTPRequestHandler):
    posted: list = []

    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        body = json.dumps({"events": []}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        _Handler.posted.append(json.loads(self.rfile.read(n) or "{}"))
        body = b'{"recorded": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)


def _commit(repo: Path, path: str, content: str, msg: str) -> None:
    (repo / path).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


def test_emit_events_posts_gate_bypass(tmp_path):
    _Handler.posted = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _commit(repo, "README.md", "x", "init")
    _commit(repo, "pyproject.toml", "[project]\nname='x'\n", "deps")  # governed, no consult
    env = {**os.environ, "KB_ENDPOINT": f"http://127.0.0.1:{port}"}
    r = subprocess.run(
        [sys.executable, "-m", "data_olympus.cli.main", "report",
         "--workspace", "proj", "--range", "HEAD~1..HEAD", "--emit-events"],
        cwd=str(repo), capture_output=True, text=True, env=env)
    srv.shutdown()
    assert r.returncode == 0
    assert any(p.get("event_type") == "gate_bypass" for p in _Handler.posted)


def test_no_emit_by_default(tmp_path):
    _Handler.posted = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _commit(repo, "README.md", "x", "init")
    _commit(repo, "pyproject.toml", "y", "deps")
    env = {**os.environ, "KB_ENDPOINT": f"http://127.0.0.1:{port}"}
    subprocess.run(
        [sys.executable, "-m", "data_olympus.cli.main", "report",
         "--workspace", "proj", "--range", "HEAD~1..HEAD"],
        cwd=str(repo), capture_output=True, text=True, env=env)
    srv.shutdown()
    assert not any(p.get("event_type") == "gate_bypass" for p in _Handler.posted)
