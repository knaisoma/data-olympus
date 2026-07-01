#!/usr/bin/env python3
"""Emit the tag to create when pyproject's version has no matching v* tag.

Used by tag-release.yml: after a release PR merges (bumping pyproject), the
version exceeds the latest tag, so this emits `vX.Y.Z` for CI to create. Normal
PRs never touch the version, so this emits nothing and no tag is cut.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)


def project_version(text: str) -> str:
    m = _VERSION_RE.search(text)
    if not m:
        raise ValueError("no version declared in pyproject.toml")
    return m.group(1)


def tag_to_create(version: str, existing: set[str]) -> str | None:
    tag = f"v{version}"
    return None if tag in existing else tag


def _existing_tags() -> set[str]:
    out = subprocess.run(["git", "tag", "--list", "v*"], capture_output=True, text=True)
    return set(out.stdout.split())


def main() -> int:
    version = project_version(pathlib.Path("pyproject.toml").read_text())
    tag = tag_to_create(version, _existing_tags())
    print(tag or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
