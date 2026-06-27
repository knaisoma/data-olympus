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
PLUGIN_SRC = Path(__file__).resolve().parent / "opencode" / "data-olympus-gate.ts"
PLUGIN_NAME = "data-olympus-gate.ts"


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
        print(
            f"installed data-olympus enforcement (v{SHIM_VERSION}) into {target} "
            f"[{self.name}, tier={self.tier}]"
        )
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


class OpenCodeProvider:
    """OpenCode provider. Unlike the hard shell-hook agents, OpenCode is wired by
    dropping a managed TypeScript plugin file into the plugin directory. Install
    copies the bundled template; uninstall removes it only if it still carries the
    managed marker, so operator-authored plugins in the same dir are untouched.
    """

    name = "opencode"
    tier = "hard"

    def default_target(self) -> Path:
        return Path(os.path.expanduser("~/.config/opencode/plugin"))

    def install(self, target: Path) -> int:
        target.mkdir(parents=True, exist_ok=True)
        dest = target / PLUGIN_NAME
        if dest.exists():
            _backup(dest)
        shutil.copy2(PLUGIN_SRC, dest)
        print(
            f"installed data-olympus enforcement (v{SHIM_VERSION}) into {dest} "
            f"[opencode, tier=hard]"
        )
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
    """A soft provider that writes a MARKER-delimited managed block into a
    Markdown instructions file. Unlike the hard hook providers, it cannot block a
    tool call; it instructs the agent to consult the KB and relies on the
    data-olympus audit log to observe compliance. The block is delimited by the
    IBEGIN/IEND HTML comments so install is idempotent (one block, replaced on
    re-install) and uninstall is surgical (operator content preserved).
    """

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
        print(
            f"installed data-olympus enforcement (v{SHIM_VERSION}) into {target} "
            f"[{self.name}, tier=soft]"
        )
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


def registry() -> dict:
    return {
        "claude-code": _claude_provider(),
        "codex": _codex_provider(),
        "gemini": _gemini_provider(),
        "opencode": OpenCodeProvider(),
        # copilot-cli: the GitHub Copilot CLI loads "custom instructions from
        # AGENTS.md and related files" (verified via `copilot --help`:
        # `--no-custom-instructions` disables exactly that). No instructions file
        # exists in ~/.copilot/ on this laptop, so we default to the documented
        # global custom-instructions path ~/.copilot/copilot-instructions.md (one
        # of the "related files"). Operators can override with --settings.
        "copilot-cli": InstructionsProvider(
            "copilot-cli", Path(os.path.expanduser("~/.copilot/copilot-instructions.md"))),
        "copilot-ide": InstructionsProvider(
            "copilot-ide", Path(".github/copilot-instructions.md")),
    }


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
