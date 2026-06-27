"""OpenCode provider (plugin-file install)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True)


def test_opencode_install_drops_plugin(tmp_path):
    plugdir = tmp_path / "plugin"
    r = _run("install", "--agent", "opencode", "--settings", str(plugdir))
    assert r.returncode == 0, r.stderr
    f = plugdir / "data-olympus-gate.ts"
    assert f.exists()
    body = f.read_text()
    assert "tool.execute.before" in body
    assert "data-olympus-enforce (managed)" in body


def test_opencode_status_and_uninstall(tmp_path):
    plugdir = tmp_path / "plugin"
    _run("install", "--agent", "opencode", "--settings", str(plugdir))
    r = _run("status", "--agent", "opencode", "--settings", str(plugdir))
    assert "opencode: installed, tier=hard" in r.stdout
    _run("uninstall", "--agent", "opencode", "--settings", str(plugdir))
    assert not (plugdir / "data-olympus-gate.ts").exists()
    r2 = _run("status", "--agent", "opencode", "--settings", str(plugdir))
    assert "opencode: not installed" in r2.stdout


def test_opencode_uninstall_leaves_foreign_plugin(tmp_path):
    plugdir = tmp_path / "plugin"
    plugdir.mkdir()
    foreign = plugdir / "other.ts"
    foreign.write_text("// operator plugin")
    _run("install", "--agent", "opencode", "--settings", str(plugdir))
    _run("uninstall", "--agent", "opencode", "--settings", str(plugdir))
    assert foreign.exists()  # only our managed file is removed
