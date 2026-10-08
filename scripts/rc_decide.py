#!/usr/bin/env python3
"""Read-only stage-one admission and publication decisions.

Stage one runs the code at H, which is data, not trusted configuration. When
the cut B carries release/ADOPTION.json, the adoption ratification and the
vendored standard are therefore read from main's Git blobs (the frozen M),
never from H's tree, workflow inputs or the environment. The engine stays the
only authority on whether the record and the ratification are valid: preflight
runs it once with exactly the arguments the build step will pass, and stage two
and promotion recompute with their own main checkout's copy.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import adoption_ratification as ratification  # noqa: E402
from scripts.sdlc_version import Git, VersionError, compute_version  # noqa: E402

RECORD = "release/ADOPTION.json"
TRUSTED_STANDARD = "adoption-standard.md"
_RATIFIED = re.compile(r"[\x21-\x7e]+")  # single line, no spaces: safe for GITHUB_ENV
_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")


@dataclass(frozen=True)
class Admission:
    """Engine branch and adoption mode for the build step."""

    engine_branch: str
    adoption: str = "none"  # none, ratified or dry-run
    ratified: str | None = None
    standard_file: Path | None = None

    def engine_args(self) -> list[str]:
        if self.adoption == "ratified":
            assert self.ratified is not None and self.standard_file is not None
            return ["--adoption-ratified", self.ratified,
                    "--standard-file", str(self.standard_file)]
        if self.adoption == "dry-run":
            return ["--adoption-dry-run"]
        return []

    def env(self) -> str:
        return (f"ENGINE_BRANCH={self.engine_branch}\nADOPTION_MODE={self.adoption}\n"
                f"ADOPTION_RATIFIED={self.ratified or ''}\n"
                f"ADOPTION_STANDARD_FILE={self.standard_file or ''}\n")


def _literal(tree: ast.Module, name: str) -> str:
    """The single top-level string literal bound to name, or refuse."""
    bindings = [node for node in tree.body
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                and any(isinstance(target, ast.Name) and target.id == name
                        for target in (node.targets if isinstance(node, ast.Assign)
                                       else [node.target]))]
    if (len(bindings) != 1 or not isinstance(bindings[0], ast.Assign)
            or len(bindings[0].targets) != 1
            or not isinstance(bindings[0].value, ast.Constant)
            or not isinstance(bindings[0].value.value, str)):
        raise ValueError(f"trusted adoption ratification: {name} must be one string literal")
    return bindings[0].value.value


def trusted_ratification(git: Git, main: str, trusted_dir: Path) -> tuple[str, Path]:
    """Read RATIFIED and the vendored standard from main's blobs, never from H.

    The module is parsed, not imported or executed. The standard blob is
    copied to a fresh file under trusted_dir; when main lacks it the file is
    absent, so the engine refuses the ratification.
    """
    source = git.file(main, ratification.MODULE)
    try:
        tree = ast.parse(source or "")
    except SyntaxError as error:
        raise ValueError("trusted adoption ratification: main's module does not parse") \
            from error
    ratified = _literal(tree, "RATIFIED")
    standard = _literal(tree, "STANDARD_FILE")
    if not _RATIFIED.fullmatch(ratified):
        raise ValueError("trusted adoption ratification: RATIFIED must be one token")
    if not _PATH.fullmatch(standard) or ".." in standard.split("/"):
        raise ValueError("trusted adoption ratification: STANDARD_FILE must be a relative path")
    trusted_dir.mkdir(parents=True, exist_ok=True)
    target = trusted_dir.resolve() / TRUSTED_STANDARD
    target.unlink(missing_ok=True)
    text = git.file(main, standard)
    if text is not None:
        target.write_text(text, encoding="utf-8")
    return ratified, target


def preflight(
    *, cwd: Path, head: str, main: str, branch: str, branch_ref: str,
    dry_run: bool, event: str, trusted_dir: Path,
) -> Admission:
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
    if git.file(cut_sha, RECORD) is None:
        return Admission(engine_branch)
    if dry_run and event == "workflow_dispatch":
        # Only a dispatched dry run may evaluate the record unratified; it is
        # never promotable (decide) and stage two never admits dispatches.
        admission = Admission(engine_branch, "dry-run")
        compute_version(cwd=cwd, head=h, main=m, branch=engine_branch, adoption_dry_run=True)
        return admission
    ratified, standard = trusted_ratification(git, m, trusted_dir)
    # The engine is the authority; this fails the run before any build.
    compute_version(cwd=cwd, head=h, main=m, branch=engine_branch,
                    adoption_ratified=ratified, standard_file=standard)
    return Admission(engine_branch, "ratified", ratified, standard)


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
    admission.add_argument("--trusted-dir", type=Path, default=Path("to-delete/rc-trusted"))
    decision = commands.add_parser("decide")
    decision.add_argument("--version-file", type=Path, required=True)
    decision.add_argument("--output", type=Path, required=True)
    decision.add_argument("--dry-run", choices=("true", "false"), default="false")
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            admitted = preflight(
                cwd=Path.cwd(), head=args.head, main=args.main, branch=args.branch,
                branch_ref=args.branch_ref, event=args.event, dry_run=args.dry_run == "true",
                trusted_dir=args.trusted_dir,
            )
            print(admitted.env(), end="")
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
