"""Doctor hardening tests for the kb enforce installer (WP0c).

doctor now verifies the managed marker/version in the live settings file, that
the hook dispatcher exists and is executable, and warns+fails when the dispatcher
resolves inside a worktree checkout (which dangles after pruning and fails open).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_kb_enforce_under_test", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_bin_in_worktree_detects_worktrees_segment() -> None:
    mod = _load_module()
    assert mod._hook_bin_in_worktree("/repo/.worktrees/foo/bin/kb-enforce-hook")
    assert mod._hook_bin_in_worktree("/repo/.claude/worktrees/foo/bin/kb-enforce-hook")
    assert not mod._hook_bin_in_worktree("/repo/bin/kb-enforce-hook")


def test_doctor_fails_when_marker_absent(tmp_path, monkeypatch) -> None:
    """doctor reports the managed hook missing when the live settings file has no
    marker, and fails (non-zero) even if we point HOOK_BIN at a real executable."""
    mod = _load_module()
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    # Point HOOK_BIN at a real executable OUTSIDE any worktree so the only failure
    # is the missing marker (and the down endpoint).
    fake_bin = tmp_path / "kb-enforce-hook"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(mod, "HOOK_BIN", str(fake_bin))
    monkeypatch.setenv("KB_ENDPOINT", "http://127.0.0.1:1")
    prov = mod._claude_provider()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = prov.doctor(settings)
    out = buf.getvalue()
    assert rc != 0
    assert "not installed" in out.lower()


def test_doctor_warns_when_hook_bin_in_worktree(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    # Install a valid marker into settings first so the ONLY structural failure is
    # the worktree-resolved dispatcher.
    prov = mod._claude_provider()
    prov.install(settings)
    wt_bin = tmp_path / ".worktrees" / "b" / "kb-enforce-hook"
    wt_bin.parent.mkdir(parents=True)
    wt_bin.write_text("#!/bin/sh\n")
    wt_bin.chmod(0o755)
    monkeypatch.setattr(mod, "HOOK_BIN", str(wt_bin))
    monkeypatch.setenv("KB_ENDPOINT", "http://127.0.0.1:1")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = prov.doctor(settings)
    out = buf.getvalue()
    assert rc != 0
    assert "worktree" in out.lower()


def test_doctor_reports_missing_hook_bin(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    prov = mod._claude_provider()
    prov.install(settings)
    monkeypatch.setattr(mod, "HOOK_BIN", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("KB_ENDPOINT", "http://127.0.0.1:1")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = prov.doctor(settings)
    out = buf.getvalue()
    assert rc != 0
    assert "missing" in out.lower()


def test_managed_versions_helper_reads_markers(tmp_path) -> None:
    mod = _load_module()
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    mod._claude_provider().install(settings)
    data = json.loads(settings.read_text())
    versions = mod._managed_versions_in_hooks(data)
    assert mod.SHIM_VERSION in versions
