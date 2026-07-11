#!/usr/bin/env python3
"""Release security-clearance gate: fail if any Dependabot or CodeQL (code
scanning) alert is OPEN for the repo.

Used in two places (see .rules/release-planning.md and .rules/release-routine.md):
the Friday planner drives every open alert to resolution or a justified
dismissal; the Monday cutter runs this as a readiness gate and refuses to build
the RC while anything is open.

CLI: `python3 scripts/security_alerts.py [--repo knaisoma/data-olympus]`
Exit 0 = clean, 5 = open alerts remain, 2 = the gh query failed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys


def _open(alerts: list[dict]) -> list[dict]:
    return [a for a in alerts if a.get("state") == "open"]


def evaluate(dependabot_alerts: list[dict], codeql_alerts: list[dict]) -> tuple[int, str]:
    """Return (exit_code, report). 0 = clean; 5 = one or more open alerts."""
    dep = _open(dependabot_alerts)
    cq = _open(codeql_alerts)
    if not dep and not cq:
        return 0, "security clear: 0 open Dependabot alerts, 0 open CodeQL alerts"
    lines: list[str] = []
    for a in dep:
        sev = (a.get("security_advisory") or {}).get("severity", "?")
        pkg = ((a.get("dependency") or {}).get("package") or {}).get("name", "?")
        lines.append(f"DEPENDABOT #{a.get('number')} [{sev}] {pkg}: {a.get('html_url', '')}")
    for a in cq:
        rule = a.get("rule") or {}
        lines.append(
            f"CODEQL #{a.get('number')} [{rule.get('severity', '?')}] "
            f"{rule.get('description', '')}: {a.get('html_url', '')}"
        )
    total = len(dep) + len(cq)
    report = (
        f"{total} open security alert(s) must be resolved or dismissed with "
        f"justification before this release:\n" + "\n".join(lines)
    )
    return 5, report


def _fetch(repo: str, endpoint: str) -> list[dict]:
    """Fetch a paginated GitHub alerts endpoint via gh, one object per line."""
    path = f"/repos/{repo}/{endpoint}?state=open"
    out = subprocess.run(
        ["gh", "api", "--paginate", path, "--jq", ".[]"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        print(f"gh api {path} failed:\n{out.stderr}", file=sys.stderr)
        raise SystemExit(2)
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="security_alerts")
    parser.add_argument("--repo", default="knaisoma/data-olympus")
    args = parser.parse_args(argv)
    dependabot = _fetch(args.repo, "dependabot/alerts")
    codeql = _fetch(args.repo, "code-scanning/alerts")
    code, report = evaluate(dependabot, codeql)
    print(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
