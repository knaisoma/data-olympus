"""Codex provider for the kb enforce installer."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True)


def test_codex_install_writes_pretooluse_and_merges(tmp_path):
    hooks = tmp_path / "hooks.json"
    # operator already has a protect-files hook
    hooks.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "protect-files.py"}]}
    ]}}))
    r = _run("install", "--agent", "codex", "--settings", str(hooks))
    assert r.returncode == 0, r.stderr
    data = json.loads(hooks.read_text())
    blob = json.dumps(data)
    assert "protect-files.py" in blob  # operator hook preserved
    assert "kb-enforce-hook pre-tool" in blob
    assert "UserPromptSubmit" in data["hooks"]
    assert "trust" in (r.stdout + r.stderr).lower()  # trust note printed


def test_codex_uninstall_keeps_operator_hook(tmp_path):
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "protect-files.py"}]}
    ]}}))
    _run("install", "--agent", "codex", "--settings", str(hooks))
    _run("uninstall", "--agent", "codex", "--settings", str(hooks))
    data = json.loads(hooks.read_text())
    blob = json.dumps(data)
    assert "protect-files.py" in blob
    assert "kb-enforce-hook" not in blob


def test_codex_status(tmp_path):
    hooks = tmp_path / "hooks.json"
    hooks.write_text("{}")
    _run("install", "--agent", "codex", "--settings", str(hooks))
    r = _run("status", "--agent", "codex", "--settings", str(hooks))
    assert "codex: installed, tier=hard" in r.stdout
