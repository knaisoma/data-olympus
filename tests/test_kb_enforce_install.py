"""Tests for the kb enforce installer (Claude Code provider)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str, settings: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args, "--settings", str(settings)],
        capture_output=True, text=True,
    )


def test_install_writes_managed_hooks(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}))
    r = _run("install", "--agent", "claude-code", settings=settings)
    assert r.returncode == 0, r.stderr
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"  # operator content preserved
    assert "hooks" in data
    blob = json.dumps(data)
    assert "kb-enforce-hook" in blob
    assert "data-olympus-enforce" in blob  # managed marker
    assert (tmp_path / "settings.json.kb-bak").exists() or any(
        p.name.startswith("settings.json.") for p in tmp_path.iterdir()
    )


def test_install_is_idempotent(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    _run("install", "--agent", "claude-code", settings=settings)
    first = settings.read_text()
    _run("install", "--agent", "claude-code", settings=settings)
    second = settings.read_text()
    assert json.loads(first) == json.loads(second)


def test_uninstall_removes_only_managed_block(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}))
    _run("install", "--agent", "claude-code", settings=settings)
    _run("uninstall", "--agent", "claude-code", settings=settings)
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    assert "kb-enforce-hook" not in json.dumps(data)


def test_status_reports_installed(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    _run("install", "--agent", "claude-code", settings=settings)
    r = _run("status", "--agent", "claude-code", settings=settings)
    assert r.returncode == 0
    assert "installed" in r.stdout.lower()


def test_doctor_reports_unreachable_endpoint(tmp_path) -> None:
    """doctor must return non-zero (not crash) when the endpoint is down.

    Earlier hook code treated non-2xx/connection failures as success;
    urllib raises HTTPError (4xx/5xx) or URLError (connection refused),
    both subclasses of URLError, which the broad except must catch.
    """
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    # Port 1 is in the privileged reserved range; nothing listens there.
    r = subprocess.run(
        [sys.executable, str(HELPER), "doctor", "--agent", "claude-code",
         "--settings", str(settings)],
        capture_output=True, text=True,
        env={"KB_ENDPOINT": "http://127.0.0.1:1", "PATH": __import__("os").environ["PATH"]},
    )
    assert r.returncode != 0
    assert "cannot reach" in r.stdout.lower()
