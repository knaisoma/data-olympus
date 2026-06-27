"""Soft Copilot providers (instructions-file managed block)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"
BEGIN = "<!-- >>> data-olympus enforce (managed) >>> -->"
END = "<!-- <<< data-olympus enforce <<< -->"


def _run(*args: str):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True)


def test_copilot_ide_writes_managed_block_preserving_content(tmp_path):
    f = tmp_path / "copilot-instructions.md"
    f.write_text("# My repo\n\nOperator guidance here.\n")
    r = _run("install", "--agent", "copilot-ide", "--settings", str(f))
    assert r.returncode == 0, r.stderr
    body = f.read_text()
    assert "Operator guidance here." in body          # operator content preserved
    assert BEGIN in body and END in body
    assert "kb_consult" in body
    assert "soft" in (r.stdout + r.stderr).lower()


def test_copilot_ide_install_idempotent(tmp_path):
    f = tmp_path / "copilot-instructions.md"
    f.write_text("# repo\n")
    _run("install", "--agent", "copilot-ide", "--settings", str(f))
    first = f.read_text()
    _run("install", "--agent", "copilot-ide", "--settings", str(f))
    assert f.read_text() == first  # exactly one managed block, no duplication


def test_copilot_ide_uninstall_removes_only_block(tmp_path):
    f = tmp_path / "copilot-instructions.md"
    f.write_text("# repo\n\nkeep me\n")
    _run("install", "--agent", "copilot-ide", "--settings", str(f))
    _run("uninstall", "--agent", "copilot-ide", "--settings", str(f))
    body = f.read_text()
    assert "keep me" in body
    assert BEGIN not in body and "kb_consult" not in body


def test_copilot_cli_status(tmp_path):
    f = tmp_path / "copilot-instructions.md"
    f.write_text("")
    _run("install", "--agent", "copilot-cli", "--settings", str(f))
    r = _run("status", "--agent", "copilot-cli", "--settings", str(f))
    assert "copilot-cli: installed, tier=soft" in r.stdout
