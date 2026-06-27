"""install --all and status-all behaviours."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str, env=None):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True, env=env)


def test_status_all_lists_every_provider(tmp_path):
    import os
    env = {**os.environ, "KB_ENFORCE_HOME": str(tmp_path)}
    r = _run("status", env=env)
    assert r.returncode == 0
    for name in ("claude-code", "codex", "gemini", "opencode",
                 "copilot-cli", "copilot-ide", "antigravity"):
        assert name in r.stdout


def test_install_all_skips_unsupported(tmp_path):
    import os
    env = {**os.environ, "KB_ENFORCE_HOME": str(tmp_path)}
    r = _run("install", "--all", env=env)
    assert r.returncode == 0
    assert "antigravity" in r.stdout  # mentioned as skipped/unsupported
    # claude settings written under the re-rooted home
    assert (tmp_path / ".claude" / "settings.json").exists()
