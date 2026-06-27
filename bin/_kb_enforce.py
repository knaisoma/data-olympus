#!/usr/bin/env python3
"""kb enforce installer (Claude Code provider).

Idempotently installs/removes data-olympus enforcement hook wiring inside a
Claude Code settings JSON, confined to a managed block tagged by MARKER so the
operator's own settings are never clobbered. Backs up before editing.

Subcommands: install | uninstall | status | doctor
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

MARKER = "data-olympus-enforce"  # tag stamped into each managed hook entry
SHIM_VERSION = "1"
HOOK_BIN = str(Path(__file__).resolve().parent / "kb-enforce-hook")

# Map: hook event name -> dispatcher mode + (optional) tool matcher.
HOOK_EVENTS = [
    ("SessionStart", "session-start", None),
    ("UserPromptSubmit", "user-prompt", None),
    ("PreToolUse", "pre-tool", "Edit|Write|MultiEdit|NotebookEdit"),
    ("Stop", "stop", None),
]


def _default_settings_path() -> Path:
    return Path(os.path.expanduser("~/.claude/settings.json"))


def _managed_entry(mode: str, matcher: str | None) -> dict:
    entry: dict = {
        "type": "command",
        "command": f"{HOOK_BIN} {mode}",
        MARKER: SHIM_VERSION,
    }
    block: dict = {"hooks": [entry]}
    if matcher is not None:
        block["matcher"] = matcher
    return block


def _strip_managed(hooks: dict) -> dict:
    """Return a copy of the hooks mapping with all MARKER-tagged entries removed."""
    out: dict = {}
    for event, blocks in hooks.items():
        kept_blocks = []
        for block in blocks:
            kept_hooks = [h for h in block.get("hooks", []) if MARKER not in h]
            if kept_hooks:
                nb = dict(block)
                nb["hooks"] = kept_hooks
                kept_blocks.append(nb)
        if kept_blocks:
            out[event] = kept_blocks
    return out


def _load(settings: Path) -> dict:
    if settings.exists() and settings.read_text().strip():
        return json.loads(settings.read_text())
    return {}


def _backup(settings: Path) -> None:
    if settings.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(settings, settings.with_suffix(settings.suffix + f".kb-bak-{ts}"))


def cmd_install(settings: Path) -> int:
    data = _load(settings)
    _backup(settings)
    hooks = _strip_managed(data.get("hooks", {}))  # remove any prior managed block first
    for event, mode, matcher in HOOK_EVENTS:
        hooks.setdefault(event, []).append(_managed_entry(mode, matcher))
    data["hooks"] = hooks
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2) + "\n")
    print(f"installed data-olympus enforcement (v{SHIM_VERSION}) into {settings}")
    return 0


def cmd_uninstall(settings: Path) -> int:
    data = _load(settings)
    if "hooks" not in data:
        print("nothing to uninstall")
        return 0
    _backup(settings)
    data["hooks"] = _strip_managed(data["hooks"])
    if not data["hooks"]:
        del data["hooks"]
    settings.write_text(json.dumps(data, indent=2) + "\n")
    print(f"uninstalled data-olympus enforcement from {settings}")
    return 0


def cmd_status(settings: Path) -> int:
    data = _load(settings)
    versions = {
        h[MARKER]
        for blocks in data.get("hooks", {}).values()
        for block in blocks
        for h in block.get("hooks", [])
        if MARKER in h
    }
    if not versions:
        print("claude-code: not installed")
        return 0
    stale = " (stale; run `kb enforce install`)" if SHIM_VERSION not in versions else ""
    print(f"claude-code: installed, tier=hard, versions={sorted(versions)}{stale}")
    return 0


def cmd_doctor(_settings: Path) -> int:  # uniform dispatch signature; settings unused
    endpoint = os.getenv("KB_ENDPOINT", "http://localhost:8080")
    try:
        with urllib.request.urlopen(f"{endpoint}/api/v1/health", timeout=5) as r:
            ok = r.status == 200
    except Exception as exc:  # noqa: BLE001 - report any failure
        print(f"doctor: cannot reach {endpoint}: {exc}")
        return 1
    print(f"doctor: endpoint {endpoint} reachable={ok}")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="kb enforce")
    p.add_argument("command", choices=["install", "uninstall", "status", "doctor"])
    p.add_argument("--agent", default="claude-code")
    p.add_argument("--settings", default=None)
    args = p.parse_args(argv)
    if args.agent != "claude-code":
        print(f"kb enforce: provider '{args.agent}' not implemented in slice 1", file=sys.stderr)
        return 64
    settings = Path(args.settings) if args.settings else _default_settings_path()
    return {
        "install": cmd_install, "uninstall": cmd_uninstall,
        "status": cmd_status, "doctor": cmd_doctor,
    }[args.command](settings)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
