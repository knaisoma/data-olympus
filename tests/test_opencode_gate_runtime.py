"""OpenCode gate runtime test (WP0c), executed under Bun when available.

Skipped when Bun is not on PATH (the case in CI, whose toolchain is Python +
bats). Locally it imports the plugin module and asserts:

- resolveWorkspace returns the main-worktree basename from both the main checkout
  and a linked worktree (worktree-invariant key);
- tool.execute.before sends workspace=basename and a non-empty action_diff for a
  bash tool call, and throws an actionable deny on verdict=consult_required.

The plugin's only non-stdlib import is `import type { Plugin, Hooks } from
"@opencode-ai/plugin"`, which Bun strips at runtime, so the module loads without
the package installed.
"""
from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

BUN = shutil.which("bun")
PLUGIN = (
    Path(__file__).resolve().parents[1] / "bin" / "opencode" / "data-olympus-gate.ts"
)

pytestmark = pytest.mark.skipif(
    BUN is None, reason="bun not installed (CI floor is the source-contract test)"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
    )


def test_resolve_workspace_is_worktree_invariant(tmp_path) -> None:
    main = tmp_path / "mainrepo"
    main.mkdir()
    _git(main, "init", "-q", "--initial-branch=main")
    _git(main, "-c", "user.email=t@e.com", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "-b", "wt", str(linked))

    script = tmp_path / "run.ts"
    script.write_text(textwrap.dedent(f"""
        import {{ resolveWorkspace }} from {str(PLUGIN)!r}
        console.log(resolveWorkspace({str(main)!r}))
        console.log(resolveWorkspace({str(linked)!r}))
    """))
    r = subprocess.run([BUN, "run", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines == ["mainrepo", "mainrepo"], r.stdout


def test_tool_execute_before_sends_basename_and_action_diff(tmp_path) -> None:
    main = tmp_path / "mainrepo"
    main.mkdir()
    _git(main, "init", "-q", "--initial-branch=main")
    _git(main, "-c", "user.email=t@e.com", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init")

    # Drive the plugin's tool.execute.before with a stubbed global fetch that
    # captures the request body and returns verdict=consult_required, asserting the
    # deny throws with an actionable message.
    script = tmp_path / "run.ts"
    script.write_text(textwrap.dedent(f"""
        import {{ DataOlympusGate }} from {str(PLUGIN)!r}
        let captured: any = null
        // @ts-ignore - stub global fetch
        globalThis.fetch = async (_url: string, opts: any) => {{
            captured = JSON.parse(opts.body)
            return {{ ok: true, json: async () => ({{ verdict: "consult_required" }}) }} as any
        }}
        const wt = {str(main)!r}
        const hooks = await DataOlympusGate({{ directory: wt, worktree: wt }} as any)
        let threw = ""
        try {{
            await hooks["tool.execute.before"](
                {{ tool: "bash", sessionID: "sess-1" }} as any,
                {{ args: {{ command: "pip install requests" }} }} as any,
            )
        }} catch (e: any) {{ threw = e.message }}
        console.log(JSON.stringify({{
            workspace: captured.workspace,
            action_diff: captured.action_diff,
            threw,
        }}))
    """))
    r = subprocess.run([BUN, "run", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    import json
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["workspace"] == "mainrepo"
    assert out["action_diff"] == "pip install requests"
    assert "kb_consult(workspace='mainrepo'" in out["threw"]
    assert "source_session='sess-1'" in out["threw"]
