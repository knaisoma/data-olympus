"""Provider registry behaviours for the kb enforce installer."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True)


def test_unknown_agent_is_usage_error() -> None:
    r = _run("status", "--agent", "no-such-agent", "--settings", "/tmp/x.json")
    assert r.returncode == 64
    assert "unknown agent" in (r.stdout + r.stderr).lower()


def test_claude_provider_install_still_writes_hooks(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}))
    r = _run("install", "--agent", "claude-code", "--settings", str(settings))
    assert r.returncode == 0, r.stderr
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    assert "kb-enforce-hook" in json.dumps(data)
    assert "data-olympus-enforce" in json.dumps(data)


def test_claude_pretooluse_uses_dialect_claude(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    _run("install", "--agent", "claude-code", "--settings", str(settings))
    blob = settings.read_text()
    # Claude commands keep the slice-1 form: "<hook> <mode>" (dialect claude is default, omitted)
    assert "kb-enforce-hook session-start" in blob
    assert "kb-enforce-hook pre-tool" in blob
