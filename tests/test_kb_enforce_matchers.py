"""Anchored-matcher tests for the kb enforce installer (WP0c).

Un-anchored alternations substring-match unrelated tools (e.g. "Bash" matches
"BashOutput"). The installed PreToolUse/BeforeTool matchers must be anchored so
they gate exactly the intended tool names.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _install(agent: str, settings: Path) -> None:
    r = subprocess.run(
        [sys.executable, str(HELPER), "install", "--agent", agent,
         "--settings", str(settings)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


def _pretool_matcher(settings: Path, event: str) -> str:
    data = json.loads(settings.read_text())
    blocks = data["hooks"][event]
    # The managed block is the one whose command references the dispatcher/plugin.
    for b in blocks:
        cmds = " ".join(h.get("command", "") for h in b.get("hooks", []))
        if "kb-enforce-hook" in cmds:
            return b["matcher"]
    raise AssertionError(f"no managed block found in {event}")


def test_claude_pretool_matcher_is_anchored(tmp_path) -> None:
    settings = tmp_path / "s.json"
    settings.write_text("{}")
    _install("claude-code", settings)
    m = _pretool_matcher(settings, "PreToolUse")
    assert m == "^(Edit|Write|MultiEdit|NotebookEdit|Bash)$"
    # Behavior: gates Bash but NOT BashOutput.
    assert re.fullmatch(m, "Bash")
    assert not re.fullmatch(m, "BashOutput")


def test_codex_pretool_matcher_is_anchored(tmp_path) -> None:
    settings = tmp_path / "hooks.json"
    settings.write_text("{}")
    _install("codex", settings)
    m = _pretool_matcher(settings, "PreToolUse")
    assert m == "^(Edit|Write|MultiEdit|Bash)$"
    assert re.fullmatch(m, "Bash")
    assert not re.fullmatch(m, "BashOutput")


def test_gemini_beforetool_matcher_is_anchored(tmp_path) -> None:
    settings = tmp_path / "s.json"
    settings.write_text("{}")
    _install("gemini", settings)
    m = _pretool_matcher(settings, "BeforeTool")
    assert m == "^(write_file|replace|run_shell_command)$"
    assert re.fullmatch(m, "replace")
    assert not re.fullmatch(m, "replace_all")
