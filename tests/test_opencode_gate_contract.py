"""OpenCode gate payload-contract tests (WP0c).

There is no TypeScript/JS test rig wired into this repo's CI (the toolchain is
Python + bats), so the CI-portable floor here asserts, by inspecting the plugin
source, that the gate request the plugin sends satisfies the two contract fixes:

1. workspace is resolved to the main git worktree BASENAME (worktree-invariant,
   matching the key every other surface records consults under), not the raw
   absolute directory path;
2. action_diff carries the change content (command/patch/content), so the gate
   classifier sees a real signal for bash/patch (which carry no file path).

A richer runtime test (importing the plugin under Bun and exercising the fetch
payload) lives in tests/test_opencode_gate_runtime.py; it is skipped when Bun is
not installed (the case in CI), so this source-contract test is the guardrail
that always runs.
"""
from __future__ import annotations

from pathlib import Path

PLUGIN = (
    Path(__file__).resolve().parents[1] / "bin" / "opencode" / "data-olympus-gate.ts"
)


def _src() -> str:
    return PLUGIN.read_text()


def test_plugin_resolves_workspace_to_basename_not_raw_path() -> None:
    src = _src()
    # Uses a basename-yielding resolver and calls it on the worktree/directory.
    assert "resolveWorkspace" in src
    assert 'basename(' in src
    assert "resolveWorkspace(worktree ?? directory)" in src
    # The gate body sends the resolved `workspace`, not the raw path.
    assert "worktree list --porcelain" in src or "worktree" in src
    # Regression guard: the old raw-path body must be gone.
    assert "workspace: worktree ?? directory" not in src


def test_plugin_sends_action_diff_for_bash_and_patch() -> None:
    src = _src()
    assert "action_diff" in src
    # The diff is drawn from the command/patch/content args and capped.
    assert ".command" in src
    assert ".patch" in src
    assert "slice(0, 4000)" in src


def test_plugin_deny_message_is_actionable() -> None:
    src = _src()
    # Blocked deny must echo workspace + session id + a copy-pasteable call.
    assert "kb_consult(workspace=" in src
    assert "source_session=" in src
    assert "input.sessionID" in src
