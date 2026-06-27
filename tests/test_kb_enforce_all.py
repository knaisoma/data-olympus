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


def test_status_with_settings_is_single_agent(tmp_path):
    # An explicit --settings names one file, so status must target a single
    # provider (claude-code) and must NOT fan out across all providers.
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    _run("install", "--agent", "claude-code", "--settings", str(settings))
    r = _run("status", "--settings", str(settings))
    assert r.returncode == 0
    assert "claude-code" in r.stdout
    # single-agent, not the 7-line fan-out
    for other in ("codex", "gemini", "opencode", "copilot-cli", "copilot-ide", "antigravity"):
        assert other not in r.stdout


def test_install_all_skips_unsupported(tmp_path):
    import os
    env = {**os.environ, "KB_ENFORCE_HOME": str(tmp_path)}
    r = _run("install", "--all", env=env)
    assert r.returncode == 0
    assert "antigravity" in r.stdout  # mentioned as skipped/unsupported
    # claude settings written under the re-rooted home
    assert (tmp_path / ".claude" / "settings.json").exists()
