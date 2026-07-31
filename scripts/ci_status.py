#!/usr/bin/env python3
"""Release CI-status gate: fail-closed check that GitHub Actions check-runs
for an EXACT commit SHA are all green.

Used as a readiness gate before cutting/publishing a release: a commit with
no check-runs, an incomplete check-run, or a non-success conclusion is
treated as NOT ready (fail-closed), not silently skipped.

CLI: `python3 scripts/ci_status.py --sha <sha> [--repo knaisoma/data-olympus]
      [--required "lint,test,build"] [--json]`
Exit 0 = all required checks completed successfully, 1 = not ready.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

_SUCCESS_STATUS = "completed"
_SUCCESS_CONCLUSION = "success"
_SKIPPED_CONCLUSION = "skipped"


def _passes(check_run: dict[str, Any]) -> bool:
    return (
        check_run.get("status") == _SUCCESS_STATUS
        and check_run.get("conclusion") == _SUCCESS_CONCLUSION
    )


def _is_optional_skip(check_run: dict[str, Any], required: set[str]) -> bool:
    return (
        check_run.get("name") not in required
        and check_run.get("status") == _SUCCESS_STATUS
        and check_run.get("conclusion") == _SKIPPED_CONCLUSION
    )


def evaluate(check_runs: list[dict[str, Any]], required: list[str]) -> dict[str, Any]:
    """Fail-closed CI readiness decision for a single commit's check-runs.

    A check "passes" iff status == "completed" AND conclusion == "success".
    Any other status (queued, in_progress) or failing conclusion counts as NOT
    passing. A completed skipped check is non-blocking only when it is not in
    the required set. With no check_runs at all, readiness is False.
    """
    checks = [
        {
            "name": cr.get("name"),
            "status": cr.get("status"),
            "conclusion": cr.get("conclusion"),
        }
        for cr in check_runs
    ]
    found_any = len(check_runs) > 0

    required_names = set(required)
    passing_names = {c["name"] for c in checks if _passes(c)}
    missing_required = [name for name in required if name not in passing_names]

    all_present_pass = all(
        _passes(c) or _is_optional_skip(c, required_names) for c in checks
    )

    if required:
        all_success = found_any and not missing_required and all_present_pass
    else:
        all_success = found_any and all_present_pass

    return {
        "checks": checks,
        "found_any": found_any,
        "all_success": all_success,
        "required": required,
        "missing_required": missing_required,
    }


def _fetch(sha: str, repo: str) -> list[dict[str, Any]]:
    """Fetch check-runs for an exact commit SHA via gh, one object per line."""
    path = f"repos/{repo}/commits/{sha}/check-runs"
    out = subprocess.run(
        ["gh", "api", "--paginate", path, "--jq", ".check_runs[]"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path} failed:\n{out.stderr}")
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ci_status")
    parser.add_argument("--sha", required=True, help="exact commit SHA (7-40 hex chars)")
    parser.add_argument("--repo", default="knaisoma/data-olympus")
    parser.add_argument("--required", default="", help="comma-separated required check names")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    if not _SHA_RE.match(args.sha):
        print(f"invalid --sha {args.sha!r}: expected 7-40 hex characters", file=sys.stderr)
        return 2

    required = [name.strip() for name in args.required.split(",") if name.strip()]

    try:
        check_runs = _fetch(args.sha, args.repo)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    result = evaluate(check_runs, required)

    if args.as_json:
        print(json.dumps({"sha": args.sha, "repo": args.repo, **result}, indent=2))
    else:
        status = "READY" if result["all_success"] else "NOT READY"
        print(f"CI status for {args.repo}@{args.sha}: {status}")
        print(f"  found_any={result['found_any']} all_success={result['all_success']}")
        if result["missing_required"]:
            print(f"  missing required: {', '.join(result['missing_required'])}")
        for c in result["checks"]:
            print(f"  - {c['name']}: status={c['status']} conclusion={c['conclusion']}")

    return 0 if result["all_success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
