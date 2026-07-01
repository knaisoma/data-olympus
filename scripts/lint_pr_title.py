#!/usr/bin/env python3
"""Validate that a PR title is a Conventional Commit.

The repo has no Node toolchain, so this is the STD-U-810 §7.1 "commitlint or
equivalent". It reuses compute_release's parser so the accepted grammar matches
the bump engine exactly.
"""
from __future__ import annotations

import sys

from scripts.compute_release import classify

_ALLOWED = {
    "feat", "fix", "perf", "chore", "docs",
    "refactor", "test", "ci", "build", "style", "revert",
}


def is_valid_title(title: str) -> bool:
    ctype, _ = classify(title, "")
    return ctype in _ALLOWED


def main(argv: list[str]) -> int:
    title = argv[0] if argv else ""
    if is_valid_title(title):
        print(f"PR title ok: {title}")
        return 0
    print(
        f"Invalid PR title: {title!r}\n"
        f"Must be a Conventional Commit: type(scope): subject, "
        f"type in {sorted(_ALLOWED)}; add '!' or a 'BREAKING CHANGE:' footer for breaking.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
