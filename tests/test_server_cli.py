"""The `data-olympus-mcp --help` entry point must print usage and exit 0 without
loading config (regression for the quickstart command that crashed with
NotADirectoryError before any server started)."""
from __future__ import annotations

import subprocess
import sys


def test_server_help_exits_zero_and_prints_usage() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "data_olympus.server", "--help"],
        capture_output=True, text=True, timeout=30,
        # A bogus KB_MAIN_PATH would crash load_config; --help must not reach it.
        env={"KB_MAIN_PATH": "/nonexistent/kb-main", "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage" in proc.stdout.lower()
    assert "data-olympus-mcp" in proc.stdout
