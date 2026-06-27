#!/usr/bin/env python3
"""CI guard: a PR that changes functional code must also update CHANGELOG.md.

Reads the list of changed files from argv and exits non-zero when a functional
path changed without CHANGELOG.md. The `no-changelog` PR label sets
KB_NO_CHANGELOG=1 to skip.
"""
from __future__ import annotations

import os
import sys

FUNCTIONAL_PREFIXES = ("src/", "bin/", "deploy/")
FUNCTIONAL_FILES = ("SPEC.md",)


def _is_functional(path: str) -> bool:
    return path.startswith(FUNCTIONAL_PREFIXES) or path in FUNCTIONAL_FILES


def needs_changelog(changed: list[str], *, label_skip: bool) -> bool:
    """True when the guard should FAIL (functional change, no changelog, not skipped)."""
    if label_skip:
        return False
    if "CHANGELOG.md" in changed:
        return False
    return any(_is_functional(p) for p in changed)


def main(argv: list[str]) -> int:
    changed = [line.strip() for line in argv if line.strip()]
    label_skip = os.getenv("KB_NO_CHANGELOG", "") == "1"
    if needs_changelog(changed, label_skip=label_skip):
        print("CHANGELOG.md must be updated for functional changes "
              "(see .rules/changelog-per-release.md). Add a no-changelog label to skip.",
              file=sys.stderr)
        return 1
    print("changelog guard: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
