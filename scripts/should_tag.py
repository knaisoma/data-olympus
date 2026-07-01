#!/usr/bin/env python3
"""Decide the release tag for tag-release.yml.

After a release PR merges (bumping pyproject), the version has no matching tag,
so this emits `vX.Y.Z` for CI to create, build, and release. Normal PRs never
touch the version, so this emits nothing and no tag is cut.

Crucially, it also emits the tag when it already exists AND points at the
current commit. That is the "reconcile" case: a prior run created and pushed the
tag but the image build or GitHub Release failed. Emitting the tag again lets the
downstream (idempotent) build/release jobs re-run and finish the release, instead
of being skipped on a full workflow rerun. When the tag exists on a different
(older) commit, that is the steady state and this emits nothing.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

_SECTION_RE = re.compile(r'^\s*\[([^\]]+)\]\s*$')
_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"')


def project_version(text: str) -> str:
    """Return the version declared under the [project] table.

    Scoped to [project] so a same-named `version =` key under an unrelated
    table (e.g. a [tool.*] config) is never mistaken for the project version.
    """
    in_project = False
    for line in text.splitlines():
        section_match = _SECTION_RE.match(line)
        if section_match:
            in_project = section_match.group(1).strip() == "project"
            continue
        if not in_project:
            continue
        version_match = _VERSION_RE.match(line)
        if version_match:
            return version_match.group(1)
    raise ValueError("no version declared in [project] table of pyproject.toml")


def tag_to_create(version: str, existing: set[str]) -> str | None:
    tag = f"v{version}"
    return None if tag in existing else tag


def release_action(
    version: str,
    existing: set[str],
    tag_commit: str | None,
    head_commit: str,
) -> str | None:
    """Return the tag to create-or-reconcile, or None for a no-op.

    - tag missing -> emit (create, then build and release)
    - tag exists and points at the current commit -> emit (reconcile a partial
      release; the downstream tag/image/release steps are idempotent)
    - tag exists on a different commit, or its commit is unknown -> None

    `tag_commit` is the commit the existing tag points at, or None if the tag
    does not exist (or could not be resolved).
    """
    to_create = tag_to_create(version, existing)
    if to_create is not None:
        return to_create
    if tag_commit is not None and tag_commit == head_commit:
        return f"v{version}"
    return None


def _existing_tags() -> set[str]:
    out = subprocess.run(["git", "tag", "--list", "v*"], capture_output=True, text=True)
    return set(out.stdout.split())


def _tag_commit(tag: str) -> str | None:
    """Return the commit the tag points at (dereferenced), or None if unresolved."""
    out = subprocess.run(["git", "rev-list", "-n", "1", tag], capture_output=True, text=True)
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and commit else None


def _head_commit() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip()


def main() -> int:
    version = project_version(pathlib.Path("pyproject.toml").read_text())
    existing = _existing_tags()
    tag = f"v{version}"
    tag_commit = _tag_commit(tag) if tag in existing else None
    print(release_action(version, existing, tag_commit, _head_commit()) or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
