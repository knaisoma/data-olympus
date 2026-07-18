#!/usr/bin/env python3
"""Install one wheel in isolation and exercise its public runtime surface."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

_MCP_PROBE = r"""
import asyncio
import sys

from fastmcp import Client

CORE = {
    "kb_consult", "kb_search", "kb_get", "kb_health", "kb_gate_check",
    "kb_record_event", "kb_session_recap",
}
SEARCH = CORE | {"tool_search", "call_tool"}

async def probe() -> None:
    endpoint, mode = sys.argv[1:3]
    async with Client(endpoint + "/mcp", timeout=20) as client:
        names = {tool.name for tool in await client.list_tools()}
        if mode == "search":
            assert names == SEARCH, names
            hidden = await client.call_tool("kb_outline", {})
            assert "tiers" in str(hidden.data), hidden
        else:
            assert names > CORE, names
            assert "kb_outline" in names, names
            assert "tool_search" not in names, names
            assert "call_tool" not in names, names

asyncio.run(probe())
"""

_PACKAGE_PROBE = r"""
from data_olympus import setup_wizard

root = setup_wizard.bin_root()
required = {
    "kb",
    "kb-enforce-hook",
    "_kb_enforce.py",
    "_kb_fallback.py",
    "_kb_detect_workspace.sh",
    "opencode/data-olympus-gate.ts",
}
missing = sorted(name for name in required if not (root / name).is_file())
assert not missing, missing
assert root.name == "_bin", root
"""

_VERSION_PROBE = r"""
from importlib.metadata import version
import sys

actual = version("data-olympus")
expected = sys.argv[1]
if actual != expected:
    raise SystemExit(f"installed version mismatch: expected {expected}, got {actual}")
"""


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stdout}{completed.stderr}"
        )
    return completed.stdout


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_health(endpoint: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 45
    url = endpoint + "/api/v1/health"
    last_error = "server did not answer"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited before health check with {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"server health timeout: {last_error}")


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _git_init(bundle: Path, env: dict[str, str]) -> None:
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.name", "Wheel Smoke"],
        ["git", "config", "user.email", "wheel-smoke@localhost"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "test: initialize wheel smoke bundle"],
    ):
        _run(command, cwd=bundle, env=env)


def _server_env(
    base: dict[str, str], scratch: Path, bundle: Path, port: int, mode: str,
) -> dict[str, str]:
    env = dict(base)
    env.update(
        {
            "KB_MAIN_PATH": str(bundle),
            "KB_INDEX_PATH": str(scratch / f"index-{mode}.db"),
            "KB_REMOTE_URL": "",
            "KB_WORKTREE_ROOT": str(scratch / f"worktrees-{mode}"),
            "KB_PENDING_ROOT": str(scratch / f"pending-{mode}"),
            "KB_PUSH_QUEUE_ROOT": str(scratch / f"push-queue-{mode}"),
            "KB_AUDIT_LOG_PATH": str(scratch / f"audit-{mode}.log"),
            "KB_LEDGER_PATH": str(scratch / f"ledger-{mode}.json"),
            "KB_HTTP_PORT": str(port),
            "KB_TOOL_DISCOVERY_MODE": mode,
            "KB_DISABLE_VERSION_CHECK": "1",
            "KB_SYNC_INTERVAL_SEC": "3600",
        }
    )
    return env


def _probe_server(
    *, python: Path, server: Path, cli: Path, scratch: Path,
    bundle: Path, base_env: dict[str, str], mode: str,
) -> None:
    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}"
    env = _server_env(base_env, scratch, bundle, port, mode)
    log_path = scratch / f"server-{mode}.log"
    with log_path.open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(server)],
            cwd=scratch,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_health(endpoint, process)
            _run([str(python), "-c", _MCP_PROBE, endpoint, mode], cwd=scratch, env=env)
            if mode == "search":
                doctor = _run(
                    [
                        str(cli), "setup", "--check", "--no-version-check",
                        "--endpoint", endpoint,
                    ],
                    cwd=scratch,
                    env=env,
                )
                if "reachable (HTTP 200)" not in doctor:
                    raise RuntimeError("setup doctor did not confirm the live endpoint")
        except Exception as exc:
            log.flush()
            log.seek(0)
            raise RuntimeError(f"{exc}\nserver log:\n{log.read()}") from exc
        finally:
            _stop(process)


def run(artifact: Path, root: Path, expected_version: str) -> None:
    artifact = artifact.resolve()
    is_distribution = artifact.suffix == ".whl" or artifact.name.endswith(".tar.gz")
    if not artifact.is_file() or not is_distribution:
        raise ValueError(f"distribution artifact does not exist: {artifact}")
    scratch = root / "to-delete" / "wheel-smoke"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    try:
        venv = scratch / "venv"
        base_env = dict(os.environ)
        _run(["uv", "venv", "--python", "3.13", str(venv)], cwd=root, env=base_env)
        python = venv / "bin" / "python"
        _run(
            ["uv", "pip", "install", "--python", str(python), str(artifact)],
            cwd=root,
            env=base_env,
        )
        bin_dir = venv / "bin"
        cli = bin_dir / "data-olympus"
        server = bin_dir / "data-olympus-mcp"
        isolated_env = dict(base_env)
        isolated_env["PATH"] = str(bin_dir) + os.pathsep + base_env.get("PATH", "")
        isolated_env["HOME"] = str(scratch / "home")
        _run(
            [str(python), "-c", _VERSION_PROBE, expected_version],
            cwd=scratch,
            env=isolated_env,
        )
        _run([str(cli), "--help"], cwd=scratch, env=isolated_env)
        _run([str(server), "--help"], cwd=scratch, env=isolated_env)
        _run([str(python), "-c", _PACKAGE_PROBE], cwd=scratch, env=isolated_env)

        bundle = scratch / "kb"
        shutil.copytree(root / "example-bundle", bundle)
        _git_init(bundle, isolated_env)
        _probe_server(
            python=python,
            server=server,
            cli=cli,
            scratch=scratch,
            bundle=bundle,
            base_env=isolated_env,
            mode="search",
        )
        _probe_server(
            python=python,
            server=server,
            cli=cli,
            scratch=scratch,
            bundle=bundle,
            base_env=isolated_env,
            mode="all",
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smoke_installed_wheel")
    parser.add_argument("--artifact", "--wheel", dest="artifact", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    run(args.artifact, args.root.resolve(), args.expected_version)
    print("installed artifact smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
