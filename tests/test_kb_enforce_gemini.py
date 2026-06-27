"""Gemini provider for the kb enforce installer."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True)


def test_gemini_install_preserves_mcp_and_uses_dialect(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(
        {"mcpServers": {"data-olympus": {"url": "http://x/mcp", "type": "http"}}}))
    r = _run("install", "--agent", "gemini", "--settings", str(settings))
    assert r.returncode == 0, r.stderr
    data = json.loads(settings.read_text())
    assert "data-olympus" in data["mcpServers"]            # operator MCP preserved
    assert "BeforeAgent" in data["hooks"]
    assert "BeforeTool" in data["hooks"]
    blob = json.dumps(data)
    assert "--dialect gemini" in blob                       # gemini command form
    assert "--agent gemini" in blob                         # consult audit records gemini
    # BeforeTool gates the mutating tools
    bt = data["hooks"]["BeforeTool"][0]
    assert bt["matcher"] == "write_file|replace|run_shell_command"


def test_gemini_uninstall_surgical(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"mcpServers": {"x": {"url": "u", "type": "http"}}}))
    _run("install", "--agent", "gemini", "--settings", str(settings))
    _run("uninstall", "--agent", "gemini", "--settings", str(settings))
    data = json.loads(settings.read_text())
    assert "mcpServers" in data and "x" in data["mcpServers"]
    assert "kb-enforce-hook" not in json.dumps(data)
