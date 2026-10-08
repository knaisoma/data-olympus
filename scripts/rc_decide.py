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


# Names whose use could rebind module globals without a visible assignment.
_DYNAMIC = frozenset({"globals", "locals", "vars", "setattr", "delattr", "exec", "eval",
                      "compile", "__import__", "__dict__", "__builtins__"})


def _bindings(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Every name bound anywhere in the module, with the binding node."""
    found: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.append((node.id, node))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.append((node.attr, node))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)) \
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            found.append((node.slice.value, node))
        elif isinstance(node, ast.alias):
            found.append((node.asname or node.name.split(".")[0], node))
        elif isinstance(node, ast.arg):
            found.append((node.arg, node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.name, node))
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            found.extend((name, node) for name in node.names)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar, ast.ExceptHandler)) and node.name:
            found.append((node.name, node))
        elif isinstance(node, ast.MatchMapping) and node.rest:
            found.append((node.rest, node))
    return found


def _literal(tree: ast.Module, name: str) -> str:
    """The value of name, which must be bound exactly once, by a top-level
    `name = "literal"`, so that parsing agrees with what an import would bind."""
    if any((isinstance(node, ast.Name) and node.id in _DYNAMIC)
           or (isinstance(node, ast.Attribute) and node.attr in _DYNAMIC)
           for node in ast.walk(tree)):
        raise ValueError("trusted adoption ratification: dynamic global access refused")
    bindings = [node for bound, node in _bindings(tree) if bound == name]
    top = [node for node in tree.body if isinstance(node, ast.Assign)
           and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
           and node.targets[0].id == name]
    if (len(bindings) != 1 or len(top) != 1 or bindings[0] is not top[0].targets[0]
            or not isinstance(top[0].value, ast.Constant)
            or not isinstance(top[0].value.value, str)):
        raise ValueError(f"trusted adoption ratification: {name} must be one string literal")
    return top[0].value.value


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


def main_ratification_kwargs(git: Git, *, head: str, main: str, trusted_dir: Path) -> dict:
    """Engine ratification kwargs from main's blobs when the cut carries the record.

    Used by promotion, whose own checkout is the squash S (tree equal to H), so
    its imported constants are candidate data there. Without a record, or with
    an ambiguous merge base, nothing is passed and the engine decides.
    """
    h, m = git.resolve(head), git.resolve(main)
    bases = git.run("merge-base", "--all", m, h, allow_one=True).splitlines()
    if len(bases) != 1 or git.file(bases[0], RECORD) is None:
        return {}
    ratified, standard = trusted_ratification(git, m, trusted_dir)
    return {"adoption_ratified": ratified, "standard_file": standard}


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
