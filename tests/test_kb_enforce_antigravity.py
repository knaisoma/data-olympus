"""Antigravity is a documented-unsupported provider stub."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True)


def test_antigravity_install_reports_unsupported():
    r = _run("install", "--agent", "antigravity", "--settings", "/tmp/x")
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "unsupported" in out
    assert "antigravity" in out


def test_antigravity_status_reports_unsupported():
    r = _run("status", "--agent", "antigravity", "--settings", "/tmp/x")
    assert "unsupported" in (r.stdout + r.stderr).lower()
