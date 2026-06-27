# Enforcement slice 2 (provider pack) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `kb enforce` providers for Codex, Gemini, OpenCode (hard), Copilot CLI + IDE (soft), and an Antigravity stub, on top of the slice-1 Claude Code provider.

**Architecture:** Refactor `bin/_kb_enforce.py` into a provider registry. Hard shell-hook agents (Claude, Codex, Gemini) share a `HookFileProvider` that writes a MARKER-tagged managed block into a JSON hooks map, parameterized by file path, event list, and dispatcher dialect. The dispatcher `bin/kb-enforce-hook` gains a `--dialect claude|gemini` flag (claude == codex behavior: exit-2 deny + stdout text; gemini: JSON-stdout deny + `additionalContext`). OpenCode uses a dropped TS plugin file. Copilot uses a managed instructions-file block. Antigravity reports unsupported.

**Tech Stack:** Python 3.13 (stdlib only), bash + bats, TypeScript (OpenCode plugin), pytest.

**Spec:** `docs/superpowers/specs/2026-06-27-enforcement-slice2-provider-pack-design.md` (contains the verified per-agent hook contracts).

**Conventions (verified against the codebase):**
- Installer is `bin/_kb_enforce.py` (Python stdlib only), dispatched from `bin/kb` via `kb enforce`. Tests in `tests/test_kb_enforce_*.py` (pytest) and `tests/test_kb_*.bats` (bats). Run pytest with `uv run --extra dev pytest`, lint with `uv run --extra dev ruff check`, bats with `bats <file>`, shell lint with `shellcheck`.
- Every behaviour-changing task adds a CHANGELOG `[Unreleased]` line is deferred to the final task (Task 9), per slice-1 precedent. Do not edit CHANGELOG mid-slice.
- Each hard provider's managed entries are tagged with `MARKER` + `SHIM_VERSION` so install is idempotent and uninstall is surgical. Always `_backup()` before editing a real file.

**Verified hook contracts (from the spec; do not re-derive):**
- Claude settings.json `hooks`: SessionStart, UserPromptSubmit, PreToolUse(matcher `Edit|Write|MultiEdit|NotebookEdit`), Stop. Deny: exit 2 + stderr.
- Codex `~/.codex/hooks.json` `hooks`: PreToolUse(matcher `Edit|Write|MultiEdit`), UserPromptSubmit, SessionStart, Stop. Deny: exit 2 + stderr. Operator must TRUST the hook (config.toml `[hooks.state]`) or use `--dangerously-bypass-hook-trust`. MERGE into existing hooks.json.
- Gemini `~/.gemini/settings.json` `hooks`: SessionStart, BeforeAgent(per-prompt; field `prompt`), BeforeTool(matcher regex `write_file|replace|run_shell_command`), Stop. Deny: exit 0 + stdout `{"decision":"deny","reason":...}`. Inject: stdout `{"hookSpecificOutput":{"additionalContext":...}}`.
- OpenCode plugin `~/.config/opencode/plugin/data-olympus-gate.ts`: `tool.execute.before` throws to abort; gates `edit|write|patch|multiedit|bash`.
- Copilot: managed block in an instructions file + MCP. Soft.

---

### Task 1: Refactor installer into a provider registry (Claude preserved)

**Files:**
- Modify: `bin/_kb_enforce.py` (restructure; keep Claude behaviour identical)
- Test: `tests/test_kb_enforce_install.py` (existing — must stay green), `tests/test_kb_enforce_registry.py` (new)

**Goal:** Introduce a `Provider` abstraction and a `HookFileProvider` base, reimplement Claude as an instance, with ZERO behaviour change to the Claude provider (the existing slice-1 tests must pass unchanged).

- [ ] **Step 1: Write the failing test (new registry behaviours)**

```python
# tests/test_kb_enforce_registry.py
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --extra dev pytest tests/test_kb_enforce_registry.py -v`
Expected: FAIL (`unknown agent` text not produced; current code says "not implemented in slice 1").

- [ ] **Step 3: Refactor `bin/_kb_enforce.py`**

Replace the whole file with this registry structure. The Claude provider produces byte-identical output to slice 1 (same MARKER, SHIM_VERSION, event list, command form).

```python
#!/usr/bin/env python3
"""kb enforce installer: per-agent providers for the data-olympus enforcement gate.

Each provider idempotently installs/removes MARKER-tagged enforcement wiring for
one coding agent, backs up before editing, and reports status/doctor. Subcommands:
install | uninstall | status | doctor. Select an agent with --agent.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

MARKER = "data-olympus-enforce"
SHIM_VERSION = "1"
HOOK_BIN = str(Path(__file__).resolve().parent / "kb-enforce-hook")


def _backup(target: Path) -> None:
    if target.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(target, target.with_suffix(target.suffix + f".kb-bak-{ts}"))


def _load_json(target: Path) -> dict:
    if target.exists() and target.read_text().strip():
        return json.loads(target.read_text())
    return {}


def _doctor_endpoint() -> tuple[bool, str]:
    endpoint = os.getenv("KB_ENDPOINT", "http://localhost:8080")
    try:
        with urllib.request.urlopen(f"{endpoint}/api/v1/health", timeout=5) as r:
            ok = r.status == 200
    except Exception as exc:  # noqa: BLE001 - report any failure
        return False, f"cannot reach {endpoint}: {exc}"
    return ok, f"endpoint {endpoint} reachable={ok}"


class HookFileProvider:
    """A provider that writes MARKER-tagged hook entries into a JSON hooks map
    inside a target file (the map lives under the top-level 'hooks' key).

    events: list of (event_name, dispatcher_mode, matcher_or_None).
    dialect: passed to kb-enforce-hook as '--dialect <dialect>'; omitted when
    'claude' so Claude's slice-1 command form ('<hook> <mode>') is preserved.
    """

    tier = "hard"

    def __init__(self, name: str, default_target: Path, events: list,
                 dialect: str = "claude", note: str = "") -> None:
        self.name = name
        self._default_target = default_target
        self._events = events
        self._dialect = dialect
        self._note = note

    def default_target(self) -> Path:
        return self._default_target

    def _command(self, mode: str) -> str:
        if self._dialect == "claude":
            return f"{HOOK_BIN} {mode}"
        return f"{HOOK_BIN} {mode} --dialect {self._dialect}"

    def _managed_block(self, mode: str, matcher: str | None) -> dict:
        entry = {"type": "command", "command": self._command(mode), MARKER: SHIM_VERSION}
        block: dict = {"hooks": [entry]}
        if matcher is not None:
            block["matcher"] = matcher
        return block

    @staticmethod
    def _strip_managed(hooks: dict) -> dict:
        out: dict = {}
        for event, blocks in hooks.items():
            kept = []
            for block in blocks:
                kh = [h for h in block.get("hooks", []) if MARKER not in h]
                if kh:
                    nb = dict(block)
                    nb["hooks"] = kh
                    kept.append(nb)
            if kept:
                out[event] = kept
        return out

    def install(self, target: Path) -> int:
        data = _load_json(target)
        _backup(target)
        hooks = self._strip_managed(data.get("hooks", {}))
        for event, mode, matcher in self._events:
            hooks.setdefault(event, []).append(self._managed_block(mode, matcher))
        data["hooks"] = hooks
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2) + "\n")
        print(f"installed data-olympus enforcement (v{SHIM_VERSION}) into {target} [{self.name}, tier={self.tier}]")
        if self._note:
            print(self._note)
        return 0

    def uninstall(self, target: Path) -> int:
        data = _load_json(target)
        if "hooks" not in data:
            print("nothing to uninstall")
            return 0
        _backup(target)
        data["hooks"] = self._strip_managed(data["hooks"])
        if not data["hooks"]:
            del data["hooks"]
        target.write_text(json.dumps(data, indent=2) + "\n")
        print(f"uninstalled data-olympus enforcement from {target} [{self.name}]")
        return 0

    def status(self, target: Path) -> int:
        data = _load_json(target)
        versions = {
            h[MARKER]
            for blocks in data.get("hooks", {}).values()
            for block in blocks
            for h in block.get("hooks", [])
            if MARKER in h
        }
        if not versions:
            print(f"{self.name}: not installed")
            return 0
        stale = " (stale; run `kb enforce install`)" if SHIM_VERSION not in versions else ""
        print(f"{self.name}: installed, tier={self.tier}, versions={sorted(versions)}{stale}")
        return 0

    def doctor(self, _target: Path) -> int:
        ok, msg = _doctor_endpoint()
        print(f"doctor [{self.name}]: {msg}")
        return 0 if ok else 1


def _claude_provider() -> HookFileProvider:
    return HookFileProvider(
        name="claude-code",
        default_target=Path(os.path.expanduser("~/.claude/settings.json")),
        events=[
            ("SessionStart", "session-start", None),
            ("UserPromptSubmit", "user-prompt", None),
            ("PreToolUse", "pre-tool", "Edit|Write|MultiEdit|NotebookEdit"),
            ("Stop", "stop", None),
        ],
        dialect="claude",
    )


def registry() -> dict:
    return {"claude-code": _claude_provider()}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="kb enforce")
    p.add_argument("command", choices=["install", "uninstall", "status", "doctor"])
    p.add_argument("--agent", default="claude-code")
    p.add_argument("--settings", default=None)
    args = p.parse_args(argv)

    reg = registry()
    provider = reg.get(args.agent)
    if provider is None:
        print(f"kb enforce: unknown agent '{args.agent}' (known: {', '.join(sorted(reg))})",
              file=sys.stderr)
        return 64
    target = Path(args.settings) if args.settings else provider.default_target()
    return {
        "install": provider.install, "uninstall": provider.uninstall,
        "status": provider.status, "doctor": provider.doctor,
    }[args.command](target)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run both test files to verify pass + no regression**

Run: `uv run --extra dev pytest tests/test_kb_enforce_install.py tests/test_kb_enforce_registry.py -v`
Expected: all pass (slice-1 tests unchanged + new registry tests). Then `uv run --extra dev ruff check bin/_kb_enforce.py tests/test_kb_enforce_registry.py` clean.

- [ ] **Step 5: Confirm the bats CLI tests still pass**

Run: `bats tests/test_kb_cli_enforce.bats`
Expected: 3/3 (the `kb enforce` dispatch is unchanged).

- [ ] **Step 6: Commit**

```bash
git add bin/_kb_enforce.py tests/test_kb_enforce_registry.py
git commit -m "refactor(enforce): provider registry + HookFileProvider (Claude preserved)"
```

---

### Task 2: Add `--dialect gemini` to the hook dispatcher

**Files:**
- Modify: `bin/kb-enforce-hook` (parse `--dialect`; default `claude` keeps current behaviour; add gemini deny/inject output)
- Modify: `tests/cli-fixtures/enforce-mock-server.py` (no change needed; reuse)
- Test: `tests/test_kb_enforce_hook_gemini.bats` (new)

**Goal:** The dispatcher accepts `kb-enforce-hook <mode> --dialect <claude|gemini>`. claude (default) is byte-for-byte the current behaviour. gemini changes only the OUTPUT contract: pre-tool deny is emitted as `{"decision":"deny","reason":...}` on stdout with exit 0 (instead of exit 2 + stderr); user-prompt / session-start context is emitted as `{"hookSpecificOutput":{"additionalContext":"..."}}` on stdout.

- [ ] **Step 1: Write the failing test**

```bash
# tests/test_kb_enforce_hook_gemini.bats
#!/usr/bin/env bats
# Gemini dialect output contract for bin/kb-enforce-hook.

setup_file() {
  REPO_ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  export REPO_ROOT
  export FIXTURE_DIR="${BATS_TEST_FILENAME%/*}/cli-fixtures"
  export HOOK="${REPO_ROOT}/bin/kb-enforce-hook"
}

setup() {
  PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
  export PORT
  export KB_ENDPOINT="http://127.0.0.1:${PORT}"
  python3 "${FIXTURE_DIR}/enforce-mock-server.py" "$PORT" &
  MOCK_PID=$!
  export MOCK_PID
  for _ in $(seq 1 30); do
    if curl --silent --max-time 0.2 "http://127.0.0.1:${PORT}/api/v1/compliance" >/dev/null 2>&1; then break; fi
    sleep 0.1
  done
}

teardown() { kill "$MOCK_PID" 2>/dev/null || true; wait "$MOCK_PID" 2>/dev/null || true; }

@test "gemini pre-tool deny is JSON on stdout with exit 0 (not exit 2)" {
  run bash -c 'echo "{\"session_id\":\"blockme\",\"cwd\":\"/tmp/p\",\"tool_name\":\"write_file\",\"tool_input\":{\"file_path\":\"/tmp/p/pyproject.toml\"}}" | "'"$HOOK"'" pre-tool --dialect gemini'
  [ "$status" -eq 0 ]
  [[ "$output" == *'"decision"'* ]]
  [[ "$output" == *'"deny"'* ]]
}

@test "gemini pre-tool allow emits no deny" {
  run bash -c 'echo "{\"session_id\":\"allowme\",\"cwd\":\"/tmp/p\",\"tool_name\":\"write_file\",\"tool_input\":{\"file_path\":\"/tmp/p/README.md\"}}" | "'"$HOOK"'" pre-tool --dialect gemini'
  [ "$status" -eq 0 ]
  [[ "$output" != *'"deny"'* ]]
}

@test "gemini user-prompt emits additionalContext JSON" {
  run bash -c 'echo "{\"session_id\":\"s1\",\"cwd\":\"/tmp/p\",\"prompt\":\"add a library\"}" | "'"$HOOK"'" user-prompt --dialect gemini'
  [ "$status" -eq 0 ]
  [[ "$output" == *'"additionalContext"'* ]]
  [[ "$output" == *'STD-U-002'* ]]
}

@test "gemini pre-tool fail-closed on HTTP 500 still blocks via JSON deny" {
  run bash -c 'echo "{\"session_id\":\"x\",\"cwd\":\"/tmp/p\",\"tool_name\":\"write_file\",\"tool_input\":{\"file_path\":\"/tmp/p/boom.toml\"}}" | KB_ENFORCE_FAIL_MODE=closed "'"$HOOK"'" pre-tool --dialect gemini'
  [ "$status" -eq 0 ]
  [[ "$output" == *'"deny"'* ]]
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `bats tests/test_kb_enforce_hook_gemini.bats`
Expected: FAIL (the hook does not parse `--dialect`; gemini JSON not emitted).

- [ ] **Step 3: Modify `bin/kb-enforce-hook`**

Parse an optional `--dialect` after the mode, defaulting to `claude`. Add two helper emitters and branch the deny/inject output on `$DIALECT`. The claude path is unchanged.

After the line `MODE="${1:-}"`, add dialect parsing:

```bash
shift || true
DIALECT="claude"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dialect) DIALECT="${2:-claude}"; shift 2 ;;
    *) shift ;;
  esac
done
```

Add two emitter helpers (place them after `is_2xx()`):

```bash
emit_context() {
  # emit_context <text> ; print injected context per dialect.
  local text="$1"
  if [[ "$DIALECT" == "gemini" ]]; then
    python3 -c 'import json,sys; print(json.dumps({"hookSpecificOutput":{"additionalContext":sys.argv[1]}}))' "$text"
  else
    printf '%s\n' "$text"
  fi
}

emit_deny() {
  # emit_deny <reason> ; signal a blocked tool call per dialect, then exit.
  local reason="$1"
  if [[ "$DIALECT" == "gemini" ]]; then
    python3 -c 'import json,sys; print(json.dumps({"decision":"deny","reason":sys.argv[1]}))' "$reason"
    exit 0
  fi
  echo "$reason" >&2
  exit 2
}

emit_fail_closed() {
  # emit_fail_closed <reason> ; block due to gate-unavailable under fail-mode=closed.
  emit_deny "$1"
}
```

Now route the existing branches through the emitters. In the `user-prompt` branch, replace the `echo "=== GOVERNING RULES..."` block that prints rules with building the text then calling `emit_context`:

```bash
    GOV="$(json_field_from "$RESP_BODY" is_governed_decision)"
    if [[ "$GOV" == "true" ]]; then
      RULES="$(echo "$RESP_BODY" | python3 -c 'import json,sys
d=json.load(sys.stdin)
lines=["=== GOVERNING RULES (data-olympus) ==="]
for r in d.get("rules",[]):
    lines.append(f"- {r.get(\"id\")}: {r.get(\"title\")}")
lines.append("=== consult these before deciding ===")
print("\n".join(lines))')"
      emit_context "$RULES"
    fi
    exit 0
```

(Note: this python is double-quoted via a single-quoted heredoc-style `-c`; ensure the `r.get(...)` keys use escaped double quotes as the existing code does, or switch to single quotes inside an outer single-quoted block as in the current file. Match the current file's working quoting style.)

In the `session-start` branch, replace the bare `echo "[KB] ...active..."` with:

```bash
    emit_context "[KB] data-olympus enforcement active (endpoint: $KB_ENDPOINT)."
    exit 0
```

In the `pre-tool` branch, replace the two fail/deny `exit` points:
- the fail-closed block: replace `echo "[KB] gate unavailable and fail-mode=closed; blocking." >&2; exit 2` with `emit_fail_closed "[KB] gate unavailable and fail-mode=closed; blocking."`
- the consult_required block: replace `echo "[KB] BLOCKED: ..." >&2; exit 2` with `emit_deny "[KB] BLOCKED: this is a governed change. Call kb_consult for '$WS' first, then retry."`

The fail-open warning (`exit 0`) stays as-is for both dialects.

- [ ] **Step 4: Run gemini bats + the existing claude bats (no regression)**

Run: `bats tests/test_kb_enforce_hook_gemini.bats tests/test_kb_enforce_hook.bats`
Expected: all pass (gemini new + the 7 claude tests unchanged). `shellcheck bin/kb-enforce-hook` clean.

- [ ] **Step 5: Commit**

```bash
git add bin/kb-enforce-hook tests/test_kb_enforce_hook_gemini.bats
git commit -m "feat(enforce): add --dialect gemini to the hook dispatcher"
```

---

### Task 3: Codex provider

**Files:**
- Modify: `bin/_kb_enforce.py` (add `_codex_provider()` to the registry)
- Test: `tests/test_kb_enforce_codex.py` (new)

**Goal:** `kb enforce install --agent codex` writes the managed PreToolUse/UserPromptSubmit/SessionStart/Stop hooks into a Codex `hooks.json`, merging with any operator-authored hooks, and prints the trust note. Dialect is `claude` (Codex shares the exit-2 deny contract).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kb_enforce_codex.py
"""Codex provider for the kb enforce installer."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True)


def test_codex_install_writes_pretooluse_and_merges(tmp_path):
    hooks = tmp_path / "hooks.json"
    # operator already has a protect-files hook
    hooks.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "protect-files.py"}]}
    ]}}))
    r = _run("install", "--agent", "codex", "--settings", str(hooks))
    assert r.returncode == 0, r.stderr
    data = json.loads(hooks.read_text())
    blob = json.dumps(data)
    assert "protect-files.py" in blob  # operator hook preserved
    assert "kb-enforce-hook pre-tool" in blob
    assert "UserPromptSubmit" in data["hooks"]
    assert "trust" in (r.stdout + r.stderr).lower()  # trust note printed


def test_codex_uninstall_keeps_operator_hook(tmp_path):
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "protect-files.py"}]}
    ]}}))
    _run("install", "--agent", "codex", "--settings", str(hooks))
    _run("uninstall", "--agent", "codex", "--settings", str(hooks))
    data = json.loads(hooks.read_text())
    blob = json.dumps(data)
    assert "protect-files.py" in blob
    assert "kb-enforce-hook" not in blob


def test_codex_status(tmp_path):
    hooks = tmp_path / "hooks.json"
    hooks.write_text("{}")
    _run("install", "--agent", "codex", "--settings", str(hooks))
    r = _run("status", "--agent", "codex", "--settings", str(hooks))
    assert "codex: installed, tier=hard" in r.stdout
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --extra dev pytest tests/test_kb_enforce_codex.py -v`
Expected: FAIL (`unknown agent 'codex'`).

- [ ] **Step 3: Add the Codex provider**

In `bin/_kb_enforce.py`, add this factory and register it:

```python
CODEX_TRUST_NOTE = (
    "NOTE (codex): Codex requires this hook to be trusted before it runs. On the "
    "next `codex` start you will be prompted to trust it, or run codex with "
    "`--dangerously-bypass-hook-trust` for vetted automation. The trust hash is "
    "persisted under [hooks.state] in ~/.codex/config.toml."
)


def _codex_provider() -> HookFileProvider:
    return HookFileProvider(
        name="codex",
        default_target=Path(os.path.expanduser("~/.codex/hooks.json")),
        events=[
            ("SessionStart", "session-start", None),
            ("UserPromptSubmit", "user-prompt", None),
            ("PreToolUse", "pre-tool", "Edit|Write|MultiEdit"),
            ("Stop", "stop", None),
        ],
        dialect="claude",  # Codex shares Claude's exit-2 deny contract
        note=CODEX_TRUST_NOTE,
    )
```

And update `registry()`:

```python
def registry() -> dict:
    return {
        "claude-code": _claude_provider(),
        "codex": _codex_provider(),
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev pytest tests/test_kb_enforce_codex.py tests/test_kb_enforce_install.py -v`
Expected: all pass. `uv run --extra dev ruff check bin/_kb_enforce.py tests/test_kb_enforce_codex.py` clean.

- [ ] **Step 5: Commit**

```bash
git add bin/_kb_enforce.py tests/test_kb_enforce_codex.py
git commit -m "feat(enforce): add codex provider (hooks.json merge + trust note)"
```

---

### Task 4: Gemini provider

**Files:**
- Modify: `bin/_kb_enforce.py` (add `_gemini_provider()`)
- Test: `tests/test_kb_enforce_gemini.py` (new)

**Goal:** `kb enforce install --agent gemini` writes SessionStart/BeforeAgent/BeforeTool/Stop managed hooks into `~/.gemini/settings.json`, preserving existing `mcpServers` and any operator hooks, using dialect `gemini`. BeforeTool matcher is the mutating-tools regex.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kb_enforce_gemini.py
"""Gemini provider for the kb enforce installer."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True)


def test_gemini_install_preserves_mcp_and_uses_dialect(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"mcpServers": {"data-olympus": {"url": "http://x/mcp", "type": "http"}}}))
    r = _run("install", "--agent", "gemini", "--settings", str(settings))
    assert r.returncode == 0, r.stderr
    data = json.loads(settings.read_text())
    assert "data-olympus" in data["mcpServers"]            # operator MCP preserved
    assert "BeforeAgent" in data["hooks"]
    assert "BeforeTool" in data["hooks"]
    blob = json.dumps(data)
    assert "--dialect gemini" in blob                       # gemini command form
    # BeforeTool gates the mutating tools
    bt = data["hooks"]["BeforeTool"][0]
    assert bt["matcher"] == "write_file|replace|run_shell_command"


def test_gemini_uninstall_surgical(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"mcpServers": {"x": {"url": "u", "type": "http"}}}))
    _run("install", "--agent", "gemini", "--settings", str(settings))
    _run("uninstall", "--agent", "gemini", "--settings", str(settings))
    data = json.loads(settings.read_text())
    assert "mcpServers" in data and "x" in data["mcpServers"]
    assert "kb-enforce-hook" not in json.dumps(data)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --extra dev pytest tests/test_kb_enforce_gemini.py -v`
Expected: FAIL (`unknown agent 'gemini'`).

- [ ] **Step 3: Add the Gemini provider**

In `bin/_kb_enforce.py`, add and register:

```python
def _gemini_provider() -> HookFileProvider:
    return HookFileProvider(
        name="gemini",
        default_target=Path(os.path.expanduser("~/.gemini/settings.json")),
        events=[
            ("SessionStart", "session-start", None),
            ("BeforeAgent", "user-prompt", None),  # BeforeAgent carries `prompt`
            ("BeforeTool", "pre-tool", "write_file|replace|run_shell_command"),
            ("Stop", "stop", None),
        ],
        dialect="gemini",
    )
```

```python
def registry() -> dict:
    return {
        "claude-code": _claude_provider(),
        "codex": _codex_provider(),
        "gemini": _gemini_provider(),
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev pytest tests/test_kb_enforce_gemini.py -v`
Expected: pass. ruff clean.

- [ ] **Step 5: Commit**

```bash
git add bin/_kb_enforce.py tests/test_kb_enforce_gemini.py
git commit -m "feat(enforce): add gemini provider (settings.json, gemini dialect)"
```

---

### Task 5: OpenCode plugin + provider

**Files:**
- Create: `bin/opencode/data-olympus-gate.ts` (the plugin template, managed header)
- Modify: `bin/_kb_enforce.py` (add `OpenCodeProvider`)
- Test: `tests/test_kb_enforce_opencode.py` (new)

**Goal:** `kb enforce install --agent opencode` copies the plugin to `~/.config/opencode/plugin/data-olympus-gate.ts` with a MARKER header; uninstall removes only that managed file; status reports installed/tier=hard.

- [ ] **Step 1: Create the plugin template `bin/opencode/data-olympus-gate.ts`**

```typescript
// data-olympus-enforce (managed) v1 -- installed by `kb enforce install --agent opencode`.
// Gates file-mutating tools through the data-olympus gate. Remove via
// `kb enforce uninstall --agent opencode`.
import type { Plugin, Hooks } from "@opencode-ai/plugin"

const ENDPOINT = process.env.KB_ENDPOINT ?? "http://localhost:8080"
const FAIL_MODE = process.env.KB_ENFORCE_FAIL_MODE ?? "open"
const TOKEN = process.env.KB_AUTH_TOKEN ?? ""
// Gate mutating tools. "bash" is included because shell-driven writes surface as bash.
const GATED = new Set(["edit", "write", "patch", "multiedit", "bash"])

export const DataOlympusGate: Plugin = async ({ directory, worktree }) => {
  return {
    "tool.execute.before": async (input, output) => {
      if (!GATED.has(input.tool)) return
      const headers: Record<string, string> = { "content-type": "application/json" }
      if (TOKEN) headers["authorization"] = `Bearer ${TOKEN}`
      let verdict: string | undefined
      try {
        const res = await fetch(`${ENDPOINT}/api/v1/gate/check`, {
          method: "POST",
          headers,
          body: JSON.stringify({
            workspace: worktree ?? directory,
            session_id: input.sessionID,
            tool_name: input.tool,
            action_path: (output.args && (output.args.filePath ?? output.args.path)) ?? "",
          }),
          signal: AbortSignal.timeout(5000),
        })
        if (!res.ok) {
          if (FAIL_MODE === "closed")
            throw new Error(`data-olympus gate HTTP ${res.status}; blocking (fail-closed)`)
          return
        }
        verdict = ((await res.json()) as { verdict?: string }).verdict
      } catch (err) {
        if (err instanceof Error && err.message.startsWith("data-olympus gate")) throw err
        if (FAIL_MODE === "closed")
          throw new Error(`data-olympus gate unreachable; blocking (fail-closed)`)
        return // fail open
      }
      if (verdict === "consult_required") {
        throw new Error(
          `BLOCKED by data-olympus: '${input.tool}' requires KB consultation. ` +
          `Call the kb_consult MCP tool for this workspace, then retry.`,
        )
      }
    },
  } satisfies Hooks
}
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_kb_enforce_opencode.py
"""OpenCode provider (plugin-file install)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True)


def test_opencode_install_drops_plugin(tmp_path):
    plugdir = tmp_path / "plugin"
    r = _run("install", "--agent", "opencode", "--settings", str(plugdir))
    assert r.returncode == 0, r.stderr
    f = plugdir / "data-olympus-gate.ts"
    assert f.exists()
    body = f.read_text()
    assert "tool.execute.before" in body
    assert "data-olympus-enforce (managed)" in body


def test_opencode_status_and_uninstall(tmp_path):
    plugdir = tmp_path / "plugin"
    _run("install", "--agent", "opencode", "--settings", str(plugdir))
    r = _run("status", "--agent", "opencode", "--settings", str(plugdir))
    assert "opencode: installed, tier=hard" in r.stdout
    _run("uninstall", "--agent", "opencode", "--settings", str(plugdir))
    assert not (plugdir / "data-olympus-gate.ts").exists()
    r2 = _run("status", "--agent", "opencode", "--settings", str(plugdir))
    assert "opencode: not installed" in r2.stdout


def test_opencode_uninstall_leaves_foreign_plugin(tmp_path):
    plugdir = tmp_path / "plugin"
    plugdir.mkdir()
    foreign = plugdir / "other.ts"
    foreign.write_text("// operator plugin")
    _run("install", "--agent", "opencode", "--settings", str(plugdir))
    _run("uninstall", "--agent", "opencode", "--settings", str(plugdir))
    assert foreign.exists()  # only our managed file is removed
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run --extra dev pytest tests/test_kb_enforce_opencode.py -v`
Expected: FAIL (`unknown agent 'opencode'`).

- [ ] **Step 4: Add the OpenCodeProvider**

In `bin/_kb_enforce.py`, add the class (it is plugin-file based, not hook-map based) and register it. Add near the top: `PLUGIN_SRC = Path(__file__).resolve().parent / "opencode" / "data-olympus-gate.ts"` and `PLUGIN_NAME = "data-olympus-gate.ts"`.

```python
class OpenCodeProvider:
    name = "opencode"
    tier = "hard"

    def default_target(self) -> Path:
        return Path(os.path.expanduser("~/.config/opencode/plugin"))

    def install(self, target: Path) -> int:
        target.mkdir(parents=True, exist_ok=True)
        dest = target / PLUGIN_NAME
        if dest.exists():
            _backup(dest)
        shutil.cop2 = shutil.copy2  # noqa: E305 - keep stdlib name; see below
        shutil.copy2(PLUGIN_SRC, dest)
        print(f"installed data-olympus enforcement (v{SHIM_VERSION}) into {dest} [opencode, tier=hard]")
        return 0

    def uninstall(self, target: Path) -> int:
        dest = target / PLUGIN_NAME
        if dest.exists() and "data-olympus-enforce (managed)" in dest.read_text():
            dest.unlink()
            print(f"uninstalled data-olympus enforcement from {dest} [opencode]")
        else:
            print("nothing to uninstall")
        return 0

    def status(self, target: Path) -> int:
        dest = target / PLUGIN_NAME
        if dest.exists() and "data-olympus-enforce (managed)" in dest.read_text():
            print("opencode: installed, tier=hard, versions=['1']")
        else:
            print("opencode: not installed")
        return 0

    def doctor(self, _target: Path) -> int:
        ok, msg = _doctor_endpoint()
        print(f"doctor [opencode]: {msg}")
        return 0 if ok else 1
```

(Remove the stray `shutil.cop2 =` line — that was an editing artifact; just call `shutil.copy2(PLUGIN_SRC, dest)`.) Register:

```python
def registry() -> dict:
    return {
        "claude-code": _claude_provider(),
        "codex": _codex_provider(),
        "gemini": _gemini_provider(),
        "opencode": OpenCodeProvider(),
    }
```

- [ ] **Step 5: Run to verify pass + typecheck the plugin if possible**

Run: `uv run --extra dev pytest tests/test_kb_enforce_opencode.py -v`
Expected: pass. ruff clean on the python. (If `tsc` + the `@opencode-ai/plugin` types are available, optionally `npx tsc --noEmit` the plugin; if not available, skip — do not block.)

- [ ] **Step 6: Commit**

```bash
git add bin/opencode/data-olympus-gate.ts bin/_kb_enforce.py tests/test_kb_enforce_opencode.py
git commit -m "feat(enforce): add opencode provider (tool.execute.before plugin)"
```

---

### Task 6: Soft providers (Copilot CLI + Copilot IDE)

**Files:**
- Modify: `bin/_kb_enforce.py` (add `InstructionsProvider` + two instances)
- Test: `tests/test_kb_enforce_copilot.py` (new)

**Goal:** Soft providers write a MARKER-delimited managed block into an instructions Markdown file telling the agent to call `kb_consult` before code/architectural decisions. `copilot-cli` targets `~/.copilot/copilot-instructions.md` (verify path during impl); `copilot-ide` targets `.github/copilot-instructions.md` in the current repo. Idempotent block replacement; surgical uninstall; tier=soft.

- [ ] **Step 1: VERIFY the Copilot CLI instructions path**

Before coding, confirm the exact instructions file Copilot CLI reads. Run `copilot --help 2>&1 | grep -i instruction` and inspect `~/.copilot/` for an existing instructions file or config key. If an authoritative path is found, use it; otherwise default to `~/.copilot/copilot-instructions.md` and note the assumption in a code comment. Record the finding in the commit message.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_kb_enforce_copilot.py
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
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run --extra dev pytest tests/test_kb_enforce_copilot.py -v`
Expected: FAIL (unknown agents).

- [ ] **Step 4: Add the InstructionsProvider**

In `bin/_kb_enforce.py`, add:

```python
IBEGIN = "<!-- >>> data-olympus enforce (managed) >>> -->"
IEND = "<!-- <<< data-olympus enforce <<< -->"
INSTRUCTIONS_BODY = f"""{IBEGIN}
<!-- data-olympus-enforce v{SHIM_VERSION} -->
## Governed decisions (data-olympus)

Before making a code or architectural decision (choosing a library, pattern,
migration, API design, schema, or auth approach), you MUST call the `kb_consult`
MCP tool for the current workspace and follow the governing rules it returns.
Treat those rules as authoritative, not advisory. This agent cannot be hard-gated
locally, so compliance is observed via the data-olympus audit log.
{IEND}"""


class InstructionsProvider:
    tier = "soft"

    def __init__(self, name: str, default_target: Path) -> None:
        self.name = name
        self._default_target = default_target

    def default_target(self) -> Path:
        return self._default_target

    @staticmethod
    def _strip_block(text: str) -> str:
        if IBEGIN in text and IEND in text:
            pre = text.split(IBEGIN, 1)[0].rstrip("\n")
            post = text.split(IEND, 1)[1].lstrip("\n")
            joined = "\n".join(p for p in (pre, post) if p)
            return (joined + "\n") if joined else ""
        return text

    def install(self, target: Path) -> int:
        existing = target.read_text() if target.exists() else ""
        if target.exists():
            _backup(target)
        base = self._strip_block(existing).rstrip("\n")
        new = (base + "\n\n" if base else "") + INSTRUCTIONS_BODY + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new)
        print(f"installed data-olympus enforcement (v{SHIM_VERSION}) into {target} [{self.name}, tier=soft]")
        return 0

    def uninstall(self, target: Path) -> int:
        if not target.exists():
            print("nothing to uninstall")
            return 0
        _backup(target)
        target.write_text(self._strip_block(target.read_text()))
        print(f"uninstalled data-olympus enforcement from {target} [{self.name}]")
        return 0

    def status(self, target: Path) -> int:
        if target.exists() and IBEGIN in target.read_text():
            print(f"{self.name}: installed, tier=soft, versions=['{SHIM_VERSION}']")
        else:
            print(f"{self.name}: not installed")
        return 0

    def doctor(self, _target: Path) -> int:
        ok, msg = _doctor_endpoint()
        print(f"doctor [{self.name}]: {msg}")
        return 0 if ok else 1
```

Register both (Copilot IDE defaults to the repo's `.github/copilot-instructions.md`):

```python
def registry() -> dict:
    return {
        "claude-code": _claude_provider(),
        "codex": _codex_provider(),
        "gemini": _gemini_provider(),
        "opencode": OpenCodeProvider(),
        "copilot-cli": InstructionsProvider(
            "copilot-cli", Path(os.path.expanduser("~/.copilot/copilot-instructions.md"))),
        "copilot-ide": InstructionsProvider(
            "copilot-ide", Path(".github/copilot-instructions.md")),
    }
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run --extra dev pytest tests/test_kb_enforce_copilot.py -v`
Expected: pass. ruff clean.

- [ ] **Step 6: Commit**

```bash
git add bin/_kb_enforce.py tests/test_kb_enforce_copilot.py
git commit -m "feat(enforce): add soft copilot-cli and copilot-ide providers"
```

---

### Task 7: Antigravity stub provider

**Files:**
- Modify: `bin/_kb_enforce.py` (add `UnsupportedProvider`)
- Test: `tests/test_kb_enforce_antigravity.py` (new)

**Goal:** `kb enforce install --agent antigravity` exits non-zero with a clear "unsupported pending vendor extensibility docs" message (no documented hook/instructions/MCP surface exists). status/doctor likewise report unsupported.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kb_enforce_antigravity.py
"""Antigravity is a documented-unsupported provider stub."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True)


def test_antigravity_install_reports_unsupported():
    r = _run("install", "--agent", "antigravity", "--settings", "/tmp/x")
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "unsupported" in out
    assert "antigravity" in out


def test_antigravity_status_reports_unsupported():
    r = _run("status", "--agent", "antigravity", "--settings", "/tmp/x")
    assert "unsupported" in (r.stdout + r.stderr).lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --extra dev pytest tests/test_kb_enforce_antigravity.py -v`
Expected: FAIL (`unknown agent 'antigravity'`).

- [ ] **Step 3: Add the UnsupportedProvider**

```python
class UnsupportedProvider:
    tier = "unsupported"

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self._reason = reason

    def default_target(self) -> Path:
        return Path("/dev/null")

    def _report(self) -> int:
        print(f"{self.name}: unsupported -- {self._reason}", file=sys.stderr)
        return 69  # EX_UNAVAILABLE

    def install(self, _target: Path) -> int:
        return self._report()

    def uninstall(self, _target: Path) -> int:
        return self._report()

    def status(self, _target: Path) -> int:
        print(f"{self.name}: unsupported -- {self._reason}")
        return 0

    def doctor(self, _target: Path) -> int:
        return self._report()
```

Register:

```python
        "antigravity": UnsupportedProvider(
            "antigravity",
            "no documented local hook/instructions/MCP surface as of 2026-06; "
            "revisit when Google publishes an extensibility API"),
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --extra dev pytest tests/test_kb_enforce_antigravity.py -v`
Expected: pass. ruff clean.

- [ ] **Step 5: Commit**

```bash
git add bin/_kb_enforce.py tests/test_kb_enforce_antigravity.py
git commit -m "feat(enforce): add antigravity unsupported-provider stub"
```

---

### Task 8: `install --all`, `status` (all agents), and detection

**Files:**
- Modify: `bin/_kb_enforce.py` (support `--all`; `status` with no `--agent` lists every provider)
- Test: `tests/test_kb_enforce_all.py` (new)

**Goal:** `kb enforce status` (no `--agent`) prints one status line per registered provider against each provider's default target. `kb enforce install --all` installs every HARD/SOFT provider into its default target (skipping unsupported), printing a per-agent tier summary. (To keep tests hermetic, `--all` honors a `KB_ENFORCE_HOME` override that re-roots default targets under a temp dir.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kb_enforce_all.py
"""install --all and status-all behaviours."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "bin" / "_kb_enforce.py"


def _run(*args: str, env=None):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True, env=env)


def test_status_all_lists_every_provider(tmp_path):
    import os
    env = {**os.environ, "KB_ENFORCE_HOME": str(tmp_path)}
    r = _run("status", env=env)
    assert r.returncode == 0
    for name in ("claude-code", "codex", "gemini", "opencode", "copilot-cli", "copilot-ide", "antigravity"):
        assert name in r.stdout


def test_install_all_skips_unsupported(tmp_path):
    import os
    env = {**os.environ, "KB_ENFORCE_HOME": str(tmp_path)}
    r = _run("install", "--all", env=env)
    assert r.returncode == 0
    assert "antigravity" in r.stdout  # mentioned as skipped/unsupported
    # claude settings written under the re-rooted home
    assert (tmp_path / ".claude" / "settings.json").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --extra dev pytest tests/test_kb_enforce_all.py -v`
Expected: FAIL (no `--all`; status requires `--agent`; no `KB_ENFORCE_HOME`).

- [ ] **Step 3: Implement `--all`, status-all, and `KB_ENFORCE_HOME`**

Make default targets honor `KB_ENFORCE_HOME` (re-root `~` under it when set), add `--all`, and let `status` with no agent iterate. Concretely:

Add a home helper and use it in every `os.path.expanduser` default target:

```python
def _home() -> str:
    override = os.getenv("KB_ENFORCE_HOME")
    return override if override else os.path.expanduser("~")
```

Replace each provider factory's `os.path.expanduser("~/...")` with `os.path.join(_home(), "...")` (e.g. Claude: `Path(_home()) / ".claude" / "settings.json"`; Codex: `Path(_home()) / ".codex" / "hooks.json"`; Gemini: `Path(_home()) / ".gemini" / "settings.json"`; OpenCode: `Path(_home()) / ".config" / "opencode" / "plugin"`; copilot-cli: `Path(_home()) / ".copilot" / "copilot-instructions.md"`). Leave copilot-ide as the repo-relative `.github/copilot-instructions.md`.

Update `main()`:

```python
def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="kb enforce")
    p.add_argument("command", choices=["install", "uninstall", "status", "doctor"])
    p.add_argument("--agent", default=None)
    p.add_argument("--all", action="store_true")
    p.add_argument("--settings", default=None)
    args = p.parse_args(argv)
    reg = registry()

    if args.all or (args.command == "status" and not args.agent):
        rc = 0
        for name, provider in reg.items():
            if args.all and getattr(provider, "tier", "") == "unsupported":
                print(f"{name}: skipped (unsupported)")
                continue
            target = provider.default_target()
            rc |= {
                "install": provider.install, "uninstall": provider.uninstall,
                "status": provider.status, "doctor": provider.doctor,
            }[args.command](target)
        return rc

    agent = args.agent or "claude-code"
    provider = reg.get(agent)
    if provider is None:
        print(f"kb enforce: unknown agent '{agent}' (known: {', '.join(sorted(reg))})",
              file=sys.stderr)
        return 64
    target = Path(args.settings) if args.settings else provider.default_target()
    return {
        "install": provider.install, "uninstall": provider.uninstall,
        "status": provider.status, "doctor": provider.doctor,
    }[args.command](target)
```

- [ ] **Step 4: Run to verify pass + full installer regression**

Run: `uv run --extra dev pytest tests/test_kb_enforce_all.py tests/test_kb_enforce_install.py tests/test_kb_enforce_registry.py tests/test_kb_enforce_codex.py tests/test_kb_enforce_gemini.py tests/test_kb_enforce_opencode.py tests/test_kb_enforce_copilot.py tests/test_kb_enforce_antigravity.py -v`
Expected: all pass. ruff clean.

- [ ] **Step 5: Commit**

```bash
git add bin/_kb_enforce.py tests/test_kb_enforce_all.py
git commit -m "feat(enforce): kb enforce install --all + status-all across providers"
```

---

### Task 9: Docs, CHANGELOG, README, full sweep

**Files:**
- Modify: `docs/enforcement.md` (per-agent install section + honest tier table)
- Modify: `CHANGELOG.md` (`[Unreleased]`, MANDATORY)
- Modify: `bin/kb` (extend the `kb enforce` Usage line to mention `--agent <name>|--all`)
- Test: full suite

- [ ] **Step 1: Extend `docs/enforcement.md`**

Add a "Per-agent providers" section (no em-dashes) with:
- the tier table from the spec (Claude/Codex/Gemini hard; Copilot CLI/IDE soft; Antigravity deferred);
- per-agent install commands: `kb enforce install --agent codex|gemini|opencode|copilot-cli|copilot-ide`, and `kb enforce install --all`;
- the Codex trust note (operator will be prompted to trust the hook, or use `--dangerously-bypass-hook-trust`);
- the OpenCode caveat (batch-tool and bash-wrapped writes are not gated; the plugin gates `bash` to reduce the gap);
- that copilot-ide is repo-scoped (`.github/copilot-instructions.md`) and soft (advisory + audit, not blocking).

- [ ] **Step 2: Update `CHANGELOG.md` (mandatory)**

Under the topmost `## [Unreleased]` `### Added`, add:

```markdown
- Cross-agent enforcement providers: `kb enforce install --agent codex|gemini|opencode|copilot-cli|copilot-ide` and `--all`. Codex and Gemini get hard PreToolUse/BeforeTool gates (merging into existing hooks), OpenCode gets a `tool.execute.before` plugin, and Copilot CLI/IDE get a soft instructions-file + MCP provider. Antigravity is a documented-unsupported stub. The `kb-enforce-hook` dispatcher gained a `--dialect gemini` output mode.
```

- [ ] **Step 3: Update the `kb enforce` Usage line in `bin/kb`**

Change the usage comment line to:

```bash
#   kb enforce install|uninstall|status|doctor [--agent NAME | --all] [--settings PATH]
```

- [ ] **Step 4: Full verification sweep**

Run:
```bash
uv run --extra dev ruff check .
uv run --extra dev pytest
bats tests/test_kb_enforce_hook.bats tests/test_kb_enforce_hook_gemini.bats tests/test_kb_cli_enforce.bats
uv run data-olympus lint example-bundle
shellcheck bin/kb-enforce-hook bin/kb
```
Expected: ruff clean; pytest all pass; bats green; lint exit 0; shellcheck clean.

- [ ] **Step 5: Commit**

```bash
git add docs/enforcement.md CHANGELOG.md bin/kb
git commit -m "docs(enforce): document slice-2 providers + mandatory changelog entry"
```

---

## Self-Review

**Spec coverage:**
- Provider registry + HookFileProvider -> Task 1.
- Dispatcher dialects (claude/codex share; gemini differs) -> Task 2 (verified only two dialects needed; codex reuses claude).
- Codex provider (hooks.json merge + trust note) -> Task 3.
- Gemini provider (settings.json, BeforeAgent per-prompt confirmed via bundled docs, BeforeTool matcher) -> Task 4.
- OpenCode plugin + provider (gates bash too; documents batch gap) -> Task 5.
- Soft Copilot CLI + IDE (instructions block + tier=soft; IDE repo-scoped; CLI path verified at impl) -> Task 6.
- Antigravity unsupported stub -> Task 7.
- install --all + status-all + detection -> Task 8.
- Docs + mandatory CHANGELOG + Usage -> Task 9.

**Placeholder scan:** every code/test step has complete code. The one deliberate verification step (Task 6 Step 1, Copilot CLI instructions path) is a real action with a defined fallback, not a vague placeholder. The OpenCode provider snippet's stray `shutil.cop2 =` line is explicitly called out for removal in the same step.

**Type/name consistency:** `HookFileProvider`, `OpenCodeProvider`, `InstructionsProvider`, `UnsupportedProvider`, `registry()`, `_doctor_endpoint()`, `_home()`, `MARKER`, `SHIM_VERSION`, `IBEGIN`/`IEND`, dispatcher `--dialect`/`emit_context`/`emit_deny` are used consistently across tasks. Provider names (`claude-code`, `codex`, `gemini`, `opencode`, `copilot-cli`, `copilot-ide`, `antigravity`) match between registry, tests, and docs.

**Known follow-ups (not slice-2 blockers):** live end-to-end runs of each agent against a real model (needs operator keys); fixing OpenCode's batch-tool gap upstream; the egress proxy (slice 3); `gate_degraded`/`gate_bypass` emission (carried from slice 1).
