"""--mode soft/off, Bash gating, and OpenCode staleness."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str):
    return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True)


def test_mode_soft_omits_pre_tool_gate(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    _run("install", "--agent", "claude-code", "--mode", "soft", "--settings", str(settings))
    data = json.loads(settings.read_text())
    # soft installs SessionStart + UserPromptSubmit but NOT the blocking PreToolUse
    assert "SessionStart" in data["hooks"]
    assert "UserPromptSubmit" in data["hooks"]
    assert "PreToolUse" not in data["hooks"]


def test_mode_hard_is_default_full(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    _run("install", "--agent", "claude-code", "--settings", str(settings))  # default hard
    data = json.loads(settings.read_text())
    assert "PreToolUse" in data["hooks"]
    # Bash is now gated alongside the edit tools
    pre = data["hooks"]["PreToolUse"][0]
    assert "Bash" in pre["matcher"]


def test_mode_off_uninstalls(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    _run("install", "--agent", "claude-code", "--settings", str(settings))
    _run("install", "--agent", "claude-code", "--mode", "off", "--settings", str(settings))
    data = json.loads(settings.read_text())
    assert "hooks" not in data  # off removed the managed hooks


def test_opencode_status_detects_stale(tmp_path):
    plugdir = tmp_path / "plugin"
    plugdir.mkdir()
    # a managed plugin with a different version header
    (plugdir / "data-olympus-gate.ts").write_text(
        "// data-olympus-enforce (managed) v0\nexport const x = 1\n")
    r = _run("status", "--agent", "opencode", "--settings", str(plugdir))
    assert "stale" in r.stdout.lower()
