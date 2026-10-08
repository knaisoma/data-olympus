#!/usr/bin/env python3
"""Placeholder package version rule for the engine-managed release cycle.

After each cut, the first commit on `release/new` (and on `hotfix/new`) sets
`pyproject.toml` `[project].version` to the documented local placeholder
`0.0.0+unreleased`. No target version is committed there: build tooling
injects the computed version through the `release_artifacts.py` overlay, and
PyPI rejects local versions, so the placeholder can never be published.

The placeholder is accepted only in that cycle. This module is the single,
tested decision used by:

- the `version-free-guard` job in `.github/workflows/ci.yaml`, through
  `python3 scripts/placeholder_version.py guard ...`;
- `tests/test_server_json.py`, which waives the server.json equality check
  only when the placeholder is declared in that cycle.

Rule: the placeholder is permitted when the branch context (the pull request
base ref, or the pushed branch when there is no base ref) is exactly
`release/new` or `hotfix/new`. Everywhere else, including `main`, feature
branches, stacked pull requests and an unknown context, it fails closed. Any
other local or non `X.Y.Z` version is refused everywhere.

CLI: `python3 scripts/placeholder_version.py guard --head-version V
--base-version V --base-ref REF`. It prints `skip` (allow, nothing more to
check) or `check` (continue to the tag reconciliation and registry guard) on
stdout, with the reason on stderr. It exits 1 when the version must be refused.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

PLACEHOLDER_VERSION = "0.0.0+unreleased"
PLACEHOLDER_BRANCHES = frozenset({"release/new", "hotfix/new"})

_STABLE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def placeholder_permitted(branch: str | None) -> bool:
    """True only when `branch` is exactly an engine-managed cycle branch."""
    return branch is not None and branch in PLACEHOLDER_BRANCHES


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


def guard_decision(head_version: str, base_version: str, base_ref: str) -> GuardDecision:
    """Decide what `version-free-guard` does with a pull request version."""
    if head_version == PLACEHOLDER_VERSION:
        if placeholder_permitted(base_ref):
            return GuardDecision(
                "skip",
                f"declared version is the placeholder {PLACEHOLDER_VERSION} on a pull "
                f"request into {base_ref}: accepted, never published, nothing to guard",
            )
        return GuardDecision(
            "fail",
            f"placeholder {PLACEHOLDER_VERSION} is only accepted on pull requests into "
            f"{' or '.join(sorted(PLACEHOLDER_BRANCHES))}; base is {base_ref or '(none)'}",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="placeholder_version")
    sub = parser.add_subparsers(dest="command", required=True)
    guard = sub.add_parser("guard", help="decide the version-free-guard action")
    guard.add_argument("--head-version", required=True)
    guard.add_argument("--base-version", required=True)
    guard.add_argument("--base-ref", required=True)
    args = parser.parse_args(argv)

    decision = guard_decision(args.head_version, args.base_version, args.base_ref)
    print(decision.reason, file=sys.stderr)
    if decision.action == "fail":
        return 1
    print(decision.action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
