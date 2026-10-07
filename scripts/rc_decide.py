#!/usr/bin/env python3
"""Read-only stage-one admission and publication decisions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.sdlc_version import Git, VersionError  # noqa: E402


def preflight(
    *, cwd: Path, head: str, main: str, branch: str, branch_ref: str,
    dry_run: bool, event: str,
) -> str:
    """Require the current branch head and an ancestor main before building."""
    if event not in ("push", "workflow_dispatch"):
        raise ValueError("unsupported RC build event")
    engine_branch = branch
    if branch not in ("release/new", "hotfix/new"):
        if not (dry_run and event == "workflow_dispatch"):
            raise ValueError("work branches require a dry-run dispatch")
        engine_branch = "release/new"
    git = Git(cwd)
    h, m = git.resolve(head), git.resolve(main)
    if h != git.resolve(branch_ref):
        raise ValueError("H is no longer the branch head; build the latest head")
    if not git.ancestor(m, h):
        raise ValueError("recut_required: main must be an ancestor of H")
    bases = git.run("merge-base", "--all", m, h).splitlines()
    if len(bases) != 1:
        raise ValueError("expected exactly one merge base")
    cut_sha = bases[0]  # B, matching the engine's CUT_SHA output.
    if git.file(cut_sha, "release/ADOPTION.json") is not None:
        raise ValueError(
            "Task 9 must wire explicit adoption ratification before this cut can build"
        )
    return engine_branch


def decide(version: dict, *, dry_run: bool) -> dict:
    """All admitted runs build; neither cut builds nor dry runs can promote."""
    return version | {
        "build": True,
        "publish": False,
        "dry_run": dry_run,
        "promotable": bool(version["promotable"] and version["N"] > 0 and not dry_run),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    admission = commands.add_parser("preflight")
    admission.add_argument("--head", required=True)
    admission.add_argument("--main", default="refs/remotes/origin/main")
    admission.add_argument("--branch", required=True)
    admission.add_argument("--branch-ref", required=True)
    admission.add_argument("--event", required=True)
    admission.add_argument("--dry-run", choices=("true", "false"), default="false")
    decision = commands.add_parser("decide")
    decision.add_argument("--version-file", type=Path, required=True)
    decision.add_argument("--output", type=Path, required=True)
    decision.add_argument("--dry-run", choices=("true", "false"), default="false")
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            branch = preflight(
                cwd=Path.cwd(), head=args.head, main=args.main, branch=args.branch,
                branch_ref=args.branch_ref, event=args.event, dry_run=args.dry_run == "true",
            )
            print(f"ENGINE_BRANCH={branch}")
        else:
            result = decide(
                json.loads(args.version_file.read_text()), dry_run=args.dry_run == "true"
            )
            args.output.write_text(json.dumps(result, indent=2) + "\n")
            print(f"PROMOTABLE={str(result['promotable']).lower()}")
    except (ValueError, OSError, VersionError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
