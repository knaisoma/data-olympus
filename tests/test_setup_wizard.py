"""Headless tests for the `data-olympus setup` wizard.

All network + subprocess effects are injected, so nothing here touches a real
endpoint, a real agent CLI, or the real HOME.
"""

from __future__ import annotations

import io
import json
import subprocess
from typing import TYPE_CHECKING

from data_olympus import setup_wizard as w

if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


def _opener(status: int, body: dict | None = None):
    payload = json.dumps(body or {}).encode()

    def open_fn(url: str, timeout: float):  # noqa: ARG001
        return _FakeResp(status, payload)

    return open_fn


def _raising_opener(exc: Exception):
    def open_fn(url: str, timeout: float):  # noqa: ARG001
        raise exc

    return open_fn


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------- #
# Endpoint probe
# --------------------------------------------------------------------------- #
def test_probe_endpoint_healthy():
    res = w.probe_endpoint(
        "http://localhost:8080/", opener=_opener(200, {"kb_commit": "abc123"})
    )
    assert res.reachable is True
    assert res.kb_commit == "abc123"
    assert res.endpoint == "http://localhost:8080"


def test_probe_endpoint_non_200():
    res = w.probe_endpoint("http://x", opener=_opener(503))
    assert res.reachable is False
    assert "503" in res.detail


def test_probe_endpoint_unreachable_never_raises():
    res = w.probe_endpoint("http://x", opener=_raising_opener(OSError("refused")))
    assert res.reachable is False
    assert "cannot reach" in res.detail


# --------------------------------------------------------------------------- #
# Agent detection
# --------------------------------------------------------------------------- #
def test_detect_agents_via_config_dir(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    agents = {a.key: a for a in w.detect_agents(env={"PATH": ""}, home=tmp_path)}
    assert agents["claude-code"].detected
    assert "config dir" in agents["claude-code"].detected_via
    assert agents["opencode"].detected
    assert not agents["codex"].detected
    assert not agents["gemini"].detected


def test_detect_agents_via_binary_on_path(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "codex"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    agents = {a.key: a for a in w.detect_agents(env={"PATH": str(bindir)}, home=home)}
    assert agents["codex"].detected
    assert "binary 'codex'" in agents["codex"].detected_via
    assert not agents["claude-code"].detected


def test_detect_agents_none(tmp_path: Path):
    agents = w.detect_agents(env={"PATH": ""}, home=tmp_path)
    assert all(not a.detected for a in agents)


# --------------------------------------------------------------------------- #
# Backup + merge idempotency
# --------------------------------------------------------------------------- #
def test_backup_file_creates_timestamped_copy(tmp_path: Path):
    target = tmp_path / "settings.json"
    target.write_text('{"a": 1}')
    backup = w.backup_file(target, now=0)
    assert backup is not None
    assert backup.exists()
    assert backup.read_text() == '{"a": 1}'
    assert w._BACKUP_SUFFIX in backup.name


def test_backup_file_absent_is_noop(tmp_path: Path):
    assert w.backup_file(tmp_path / "nope.json") is None


def test_merge_mcp_json_adds_entry():
    doc, changed = w.merge_mcp_json({}, name="data-olympus", url="http://x/mcp")
    assert changed is True
    assert doc["mcpServers"]["data-olympus"] == {"url": "http://x/mcp"}


def test_merge_mcp_json_preserves_other_servers():
    existing = {"mcpServers": {"other": {"url": "http://other"}}}
    doc, changed = w.merge_mcp_json(existing, name="data-olympus", url="http://x/mcp")
    assert changed is True
    assert doc["mcpServers"]["other"] == {"url": "http://other"}
    assert doc["mcpServers"]["data-olympus"] == {"url": "http://x/mcp"}
    # original untouched (deep copy)
    assert "data-olympus" not in existing["mcpServers"]


def test_merge_mcp_json_idempotent():
    existing = {"mcpServers": {"data-olympus": {"url": "http://x/mcp"}}}
    _doc, changed = w.merge_mcp_json(existing, name="data-olympus", url="http://x/mcp")
    assert changed is False


def test_write_json_registration_backs_up_and_merges(tmp_path: Path):
    target = tmp_path / ".gemini" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"mcpServers": {"keep": {"url": "u"}}}))
    res = w.write_json_registration(
        target, agent="gemini", name="data-olympus", url="http://x/mcp",
        extra={"type": "http"}, now=0,
    )
    assert res.changed is True
    assert res.backup is not None and res.backup.exists()
    doc = json.loads(target.read_text())
    assert doc["mcpServers"]["keep"] == {"url": "u"}
    assert doc["mcpServers"]["data-olympus"] == {"url": "http://x/mcp", "type": "http"}


def test_write_json_registration_idempotent_no_backup(tmp_path: Path):
    target = tmp_path / "c.json"
    target.write_text(json.dumps({"mcpServers": {"data-olympus": {"url": "http://x/mcp"}}}))
    res = w.write_json_registration(
        target, agent="gemini", name="data-olympus", url="http://x/mcp", now=0
    )
    assert res.action == "unchanged"
    assert res.changed is False
    # no backup file was created
    assert list(target.parent.glob("*.do-bak-*")) == []


# --------------------------------------------------------------------------- #
# Per-agent registration
# --------------------------------------------------------------------------- #
def test_register_claude_uses_cli_when_present(tmp_path: Path):
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(argv)
        return _completed(0, stdout="Added stdio MCP server data-olympus")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").write_text("#!/bin/sh\n")
    (bindir / "claude").chmod(0o755)
    res = w.register_claude(
        "http://x", home=tmp_path, env={"PATH": str(bindir)}, runner=runner, now=0
    )
    assert res.action == "cli"
    assert res.changed is True
    assert calls[0] == [
        "claude", "mcp", "add", "--transport", "http", "data-olympus", "http://x/mcp"
    ]


def test_register_claude_manual_when_cli_absent(tmp_path: Path):
    res = w.register_claude("http://x", home=tmp_path, env={"PATH": ""})
    assert res.action == "manual"
    assert "claude mcp add --transport http data-olympus http://x/mcp" in res.detail


def test_register_claude_already_exists_is_unchanged(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").write_text("#!/bin/sh\n")
    (bindir / "claude").chmod(0o755)

    def runner(argv):  # noqa: ARG001
        return _completed(1, stderr="MCP server data-olympus already exists")

    res = w.register_claude(
        "http://x", home=tmp_path, env={"PATH": str(bindir)}, runner=runner, now=0
    )
    assert res.action == "unchanged"


def test_register_codex_uses_cli(tmp_path: Path):
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(argv)
        return _completed(0)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "codex").write_text("#!/bin/sh\n")
    (bindir / "codex").chmod(0o755)
    res = w.register_codex(
        "http://x/", home=tmp_path, env={"PATH": str(bindir)}, runner=runner, now=0
    )
    assert res.action == "cli"
    assert calls[0] == ["codex", "mcp", "add", "data-olympus", "--url", "http://x/mcp"]


def test_register_claude_backs_up_cli_owned_config(tmp_path: Path):
    """The CLI owns the write, but the wizard still backs up ~/.claude.json so the
    'every registration is backed up' guarantee holds for CLI-backed agents."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").write_text("#!/bin/sh\n")
    (bindir / "claude").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('{"mcpServers": {}}')
    res = w.register_claude(
        "http://x", home=home, env={"PATH": str(bindir)},
        runner=lambda argv: _completed(0), now=0,  # noqa: ARG005
    )
    assert res.backup is not None and res.backup.exists()
    assert res.backup.read_text() == '{"mcpServers": {}}'


def test_register_codex_backs_up_cli_owned_config(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "codex").write_text("#!/bin/sh\n")
    (bindir / "codex").chmod(0o755)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text("model = 'o3'\n")
    res = w.register_codex(
        "http://x", home=home, env={"PATH": str(bindir)},
        runner=lambda argv: _completed(0), now=0,  # noqa: ARG005
    )
    assert res.backup is not None and res.backup.exists()


def test_register_gemini_writes_http_type(tmp_path: Path):
    res = w.register_gemini("http://x", home=tmp_path, now=0)
    doc = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert doc["mcpServers"]["data-olympus"] == {"url": "http://x/mcp", "type": "http"}
    assert res.changed is True


def test_register_opencode_writes_local_wrapper(tmp_path: Path):
    res = w.register_opencode("http://x", home=tmp_path, now=0)
    doc = json.loads((tmp_path / ".config" / "opencode" / "opencode.json").read_text())
    entry = doc["mcp"]["data-olympus"]
    assert entry["type"] == "local"
    assert entry["command"] == ["npx", "-y", "mcp-remote", "http://x/mcp", "--allow-http"]
    assert entry["enabled"] is True
    assert res.changed is True


def test_register_opencode_idempotent(tmp_path: Path):
    w.register_opencode("http://x", home=tmp_path, now=0)
    res2 = w.register_opencode("http://x", home=tmp_path, now=0)
    assert res2.action == "unchanged"


# --------------------------------------------------------------------------- #
# Enforcement delegation
# --------------------------------------------------------------------------- #
def test_install_enforcement_invokes_shipped_script(monkeypatch, tmp_path: Path):
    fake_script = tmp_path / "_kb_enforce.py"
    fake_script.write_text("# stub")
    monkeypatch.setattr(w, "enforce_script", lambda: fake_script)
    captured: dict = {}

    def runner(argv, env):
        captured["argv"] = argv
        captured["env"] = env
        return _completed(0, stdout="installed data-olympus enforcement")

    res = w.install_enforcement("claude-code", env={"X": "1"}, runner=runner)
    assert res.changed is True
    assert captured["argv"][1:] == [str(fake_script), "install", "--agent", "claude-code"]


def test_install_enforcement_missing_script_skips(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(w, "enforce_script", lambda: tmp_path / "absent.py")
    res = w.install_enforcement("codex", env={})
    assert res.action == "skipped"


# --------------------------------------------------------------------------- #
# Version check (offline fallback)
# --------------------------------------------------------------------------- #
def test_latest_version_from_pypi():
    def fetch(url, timeout):  # noqa: ARG001
        assert "pypi.org" in url
        return json.dumps({"info": {"version": "9.9.9"}}).encode()

    info = w.latest_version(fetcher=fetch)
    assert info.latest == "9.9.9"
    assert info.source == "pypi"


def test_latest_version_falls_back_to_github():
    def fetch(url, timeout):  # noqa: ARG001
        if "pypi.org" in url:
            raise OSError("no pypi yet")
        return json.dumps({"tag_name": "v1.2.3"}).encode()

    info = w.latest_version(fetcher=fetch)
    assert info.latest == "1.2.3"
    assert info.source == "github"


def test_latest_version_offline():
    def fetch(url, timeout):  # noqa: ARG001
        raise OSError("no network")

    info = w.latest_version(fetcher=fetch)
    assert info.latest is None
    assert info.source == "offline"


def test_compare_versions_newer_available():
    info = w.VersionInfo(installed="0.2.0", latest="0.3.0", source="pypi")
    assert "0.3.0 available" in w.compare_versions(info)


def test_compare_versions_offline():
    info = w.VersionInfo(installed="0.2.0", latest=None, source="offline", detail="no net")
    line = w.compare_versions(info)
    assert "0.2.0 installed" in line
    assert "no net" in line


# --------------------------------------------------------------------------- #
# Doctor (read-only)
# --------------------------------------------------------------------------- #
def test_build_doctor_is_read_only(tmp_path: Path):
    (tmp_path / ".claude").mkdir()

    def enforce_runner(argv, env):  # noqa: ARG001
        return _completed(0, stdout="claude-code: not installed")

    doctor = w.build_doctor(
        "http://x",
        env={"PATH": ""},
        home=tmp_path,
        opener=_opener(200, {"kb_commit": "c"}),
        enforce_runner=enforce_runner,
        check_version=False,
    )
    assert doctor.endpoint.reachable is True
    assert any("Claude Code detected" in line for line in doctor.lines)
    # No files created beyond the pre-existing .claude dir.
    assert set(p.name for p in tmp_path.iterdir()) == {".claude"}


# --------------------------------------------------------------------------- #
# run(): --check is read-only; interactive orchestration
# --------------------------------------------------------------------------- #
def test_run_check_mode_read_only(tmp_path: Path):
    out = io.StringIO()

    def enforce_runner(argv, env):  # noqa: ARG001
        return _completed(0, stdout="claude-code: not installed")

    # Monkeypatch enforcement_status via a wrapper: build_doctor calls the real
    # one, so point enforce_script at a stub-less path by using check_version off.
    rc = w.run(
        check_only=True,
        no_version_check=True,
        env={"PATH": "", "KB_ENDPOINT": "http://x"},
        home=tmp_path,
        stream_out=out,
        opener=_opener(200),
    )
    assert rc == 0
    text = out.getvalue()
    assert "setup --check" in text
    # nothing was written into the fake home
    assert list(tmp_path.iterdir()) == []


def test_run_yes_mode_registers_detected(tmp_path: Path, monkeypatch):
    # Detect gemini via config dir; assume_yes registers it (file-based, no CLI).
    (tmp_path / ".gemini").mkdir()
    monkeypatch.setattr(w, "enforce_script", lambda: tmp_path / "absent.py")
    out = io.StringIO()
    rc = w.run(
        argv_endpoint="http://x",
        assume_yes=True,
        no_version_check=True,
        env={"PATH": ""},
        home=tmp_path,
        stream_in=io.StringIO(""),
        stream_out=out,
        opener=_opener(200),
    )
    assert rc == 0
    # gemini settings.json was written
    doc = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert "data-olympus" in doc["mcpServers"]
