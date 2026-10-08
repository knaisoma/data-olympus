#!/usr/bin/env python3
"""Placeholder package version rule for the engine-managed release cycle.

After each cut, the first commit on `release/new` (and on `hotfix/new`) sets
`pyproject.toml` `[project].version` to the documented local placeholder
`0.0.0+unreleased`. No target version is committed there: build tooling
injects the computed version through the `release_artifacts.py` overlay, and
PyPI rejects local versions, so the placeholder can never be published.

Promotion requires the release squash on `main` to have exactly the tree of
the reviewed cycle head (`scripts/release_record.py`), so `main` also receives
the placeholder at every release. This module is the single, tested decision
used by:

- the `version-free-guard` job in `.github/workflows/ci.yaml`, through
  `python3 scripts/placeholder_version.py guard ...`;
- `tests/test_server_json.py`, which waives the server.json equality check
  only where the placeholder is legitimate.

Rule for `version-free-guard` (the base is the pull request base ref):

- base `release/new` or `hotfix/new`: the placeholder is accepted;
- base `main`: the placeholder is accepted only when the pull request head tree
  is exactly the tree of `origin/release/new` or `origin/hotfix/new` (a release
  or hotfix squash), or when `main` already declares the placeholder and the
  pull request leaves it unchanged; a missing cycle ref never matches;
- any other base, or no base: the placeholder is refused;
- any other local or non `X.Y.Z` version is refused everywhere;
- concrete `X.Y.Z` versions: unchanged skips, changed goes to the registry check.

CLI: `python3 scripts/placeholder_version.py guard --head-version V
--base-version V --base-ref REF [--head-commit SHA]`. It prints `skip` (allow,
nothing more to check) or `check` (continue to the tag reconciliation and
registry guard) on stdout, with the reason on stderr, and exits 1 when the
version must be refused. Cycle trees are read from `origin/<branch>` in the
current repository.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

PLACEHOLDER_VERSION = "0.0.0+unreleased"
PLACEHOLDER_BRANCHES = frozenset({"release/new", "hotfix/new"})
MAIN_BRANCH = "main"

_STABLE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_TREE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def placeholder_permitted(branch: str | None) -> bool:
    """True only when `branch` is exactly an engine-managed cycle branch."""
    return branch is not None and branch in PLACEHOLDER_BRANCHES


def server_json_placeholder_permitted(branch: str | None) -> bool:
    """True where a checkout may legitimately declare the placeholder.

    The cycle branches carry it by design and `main` receives it through the
    tree-equal release squash. Whether a pull request into `main` may introduce
    it is decided by `version-free-guard` (tree equality), not by this test
    context.
    """
    return placeholder_permitted(branch) or branch == MAIN_BRANCH


def branch_context(env: Mapping[str, str]) -> str | None:
    """Return the branch whose rules apply to this checkout, or None.

    On a pull request GitHub sets `GITHUB_BASE_REF` to the target branch; the
    head's own name and `GITHUB_REF_NAME` (`<n>/merge`) are not trusted for
    this decision. On a push there is no base ref, so the pushed branch
    `GITHUB_REF_NAME` applies. With neither set (a local run) there is no
    context and the placeholder is refused; set `GITHUB_BASE_REF` explicitly
    to run the suite locally on a cycle branch.
    """
    base = env.get("GITHUB_BASE_REF", "")
    if base:
        return base
    pushed = env.get("GITHUB_REF_NAME", "")
    return pushed or None


@dataclass(frozen=True, slots=True)
class GuardDecision:
    action: str  # "skip", "check" or "fail"
    reason: str


def _matching_cycle(head_tree: str | None, cycle_trees: Mapping[str, str | None]) -> str | None:
    if head_tree is None or _TREE.fullmatch(head_tree) is None:
        return None
    for branch in sorted(PLACEHOLDER_BRANCHES):
        tree = cycle_trees.get(branch)
        if tree is not None and tree == head_tree:
            return branch
    return None


def guard_decision(
    head_version: str,
    base_version: str,
    base_ref: str,
    *,
    head_tree: str | None = None,
    cycle_trees: Mapping[str, str | None] | None = None,
) -> GuardDecision:
    """Decide what `version-free-guard` does with a pull request version.

    `head_tree` is the tree of the pull request head commit and `cycle_trees`
    maps each cycle branch to the tree of `origin/<branch>`, or None when that
    ref is missing. Both are only consulted for a placeholder into `main`.
    """
    if head_version == PLACEHOLDER_VERSION:
        if placeholder_permitted(base_ref):
            return GuardDecision(
                "skip",
                f"declared version is the placeholder {PLACEHOLDER_VERSION} on a pull "
                f"request into {base_ref}: accepted, never published, nothing to guard",
            )
        if base_ref == MAIN_BRANCH:
            if base_version == PLACEHOLDER_VERSION:
                return GuardDecision(
                    "skip",
                    f"main already declares the placeholder {PLACEHOLDER_VERSION} and "
                    "this pull request leaves it unchanged: accepted",
                )
            branch = _matching_cycle(head_tree, cycle_trees or {})
            if branch is not None:
                return GuardDecision(
                    "skip",
                    f"placeholder {PLACEHOLDER_VERSION} into main: head tree equals "
                    f"origin/{branch}, a reviewed cycle squash: accepted",
                )
            return GuardDecision(
                "fail",
                f"placeholder {PLACEHOLDER_VERSION} may enter main only as a squash "
                "whose tree equals origin/release/new or origin/hotfix/new; this "
                "head tree matches neither (or the cycle ref is missing)",
            )
        return GuardDecision(
            "fail",
            f"placeholder {PLACEHOLDER_VERSION} is only accepted on pull requests into "
            f"{' or '.join(sorted(PLACEHOLDER_BRANCHES))}, or into main as a tree-equal "
            f"cycle squash; base is {base_ref or '(none)'}",
        )
    if _STABLE_VERSION.fullmatch(head_version) is None:
        return GuardDecision(
            "fail",
            f"declared version {head_version!r} is neither X.Y.Z nor the placeholder "
            f"{PLACEHOLDER_VERSION}",
        )
    if head_version == base_version:
        return GuardDecision(
            "skip", f"declared version unchanged ({head_version}); nothing to guard"
        )
    return GuardDecision(
        "check",
        f"declared version changed {base_version} -> {head_version}; checking it is free",
    )


def _tree(revision: str) -> str | None:
    """Return the tree of `revision`, or None when it does not resolve."""
    if not revision or revision.startswith("-"):
        return None
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{tree}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    tree = completed.stdout.strip()
    return tree if completed.returncode == 0 and _TREE.fullmatch(tree) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="placeholder_version")
    sub = parser.add_subparsers(dest="command", required=True)
    guard = sub.add_parser("guard", help="decide the version-free-guard action")
    guard.add_argument("--head-version", required=True)
    guard.add_argument("--base-version", required=True)
    guard.add_argument("--base-ref", required=True)
    guard.add_argument("--head-commit", default="", help="pull request head commit SHA")
    args = parser.parse_args(argv)

    head_tree: str | None = None
    cycle_trees: dict[str, str | None] = {}
    if args.head_version == PLACEHOLDER_VERSION and args.base_ref == MAIN_BRANCH:
        head_tree = _tree(args.head_commit)
        cycle_trees = {b: _tree(f"refs/remotes/origin/{b}") for b in PLACEHOLDER_BRANCHES}
    decision = guard_decision(
        args.head_version,
        args.base_version,
        args.base_ref,
        head_tree=head_tree,
        cycle_trees=cycle_trees,
    )
    print(decision.reason, file=sys.stderr)
    if decision.action == "fail":
        return 1
    print(decision.action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
