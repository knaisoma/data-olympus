#!/usr/bin/env python3
"""Compute the next SemVer release from Conventional Commits since the last tag.

Pure logic plus a thin git-driven CLI. Mapping is documented in
`.rules/versioning.md` (STD-U-810 pre-1.0 semantics). This module is the single
source of the bump rules; `should_tag.py` and `lint_pr_title.py` reuse its parser.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# Allow running as a plain script (python scripts/x.py): put repo root on the
# path so `scripts.*` imports resolve the same way they do under pytest.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.check_changelog import _is_functional  # noqa: E402  # one definition of "functional"

_SUBJECT_RE = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<bang>!)?:\s")
_BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
_RC_TAG_RE = re.compile(r"^v?(?P<base>\d+\.\d+\.\d+)-rc\.(?P<n>\d+)$")
_FEATURE = "feat"
_FIXES = ("fix", "perf")
_NONE, _PATCH, _MINOR = 0, 1, 2
_RANK_NAME = {_NONE: "none", _PATCH: "patch", _MINOR: "minor"}


def classify(subject: str, body: str) -> tuple[str | None, bool]:
    """Return (conventional type, is_breaking). type is None if not conventional."""
    m = _SUBJECT_RE.match(subject)
    if not m:
        return None, False
    breaking = bool(m.group("bang")) or bool(_BREAKING_FOOTER.search(body or ""))
    return m.group("type"), breaking


def bump_for(commits: list[tuple[str, str]], functional_changed: bool) -> tuple[str, dict]:
    """commits: list of (subject, body). Returns (bump, changes buckets)."""
    rank = _NONE
    features: list[str] = []
    fixes: list[str] = []
    breaking: list[str] = []
    for subject, body in commits:
        ctype, is_breaking = classify(subject, body)
        if ctype is None:
            continue
        if is_breaking:
            breaking.append(subject)
            rank = max(rank, _MINOR)
        elif ctype == _FEATURE:
            features.append(subject)
            rank = max(rank, _MINOR)  # pre-1.0 features-as-minor (see .rules/versioning.md)
        elif ctype in _FIXES:
            fixes.append(subject)
            rank = max(rank, _PATCH)
    if rank == _NONE and functional_changed:
        rank = _PATCH  # functional-change safety net: never leave source unreleased
    return _RANK_NAME[rank], {"features": features, "fixes": fixes, "breaking": breaking}


def next_version(current: str, bump: str) -> str:
    major, minor, patch = (int(x) for x in current.split("."))
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return current


def next_rc_number(base_version: str, existing_refs: Iterable[str]) -> int:
    """One past the highest N among existing `<base_version>-rc.N` refs, else 1.

    Refs may carry a leading `v`; refs for a different base or that are not
    `X.Y.Z-rc.N` are ignored.
    """
    highest = 0
    for ref in existing_refs:
        m = _RC_TAG_RE.match(ref.strip())
        if m is None or m.group("base") != base_version:
            continue
        highest = max(highest, int(m.group("n")))
    return highest + 1


def next_rc_tag(base_version: str, existing_refs: Iterable[str]) -> str:
    """The next release-candidate channel tag, e.g. `0.5.0-rc.3`."""
    return f"{base_version}-rc.{next_rc_number(base_version, existing_refs)}"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def _last_tag() -> str | None:
    out = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
        capture_output=True, text=True,
    )
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def _commits_since(tag: str | None) -> list[tuple[str, str]]:
    rng = f"{tag}..HEAD" if tag else "HEAD"
    fmt = "%s%x1f%b%x1e"
    out = _git("log", "--no-merges", f"--format={fmt}", rng)
    commits: list[tuple[str, str]] = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        subject, _, body = rec.partition("\x1f")
        commits.append((subject, body))
    return commits


def _changed_paths(tag: str | None) -> list[str]:
    rng = f"{tag}..HEAD" if tag else "HEAD"
    return [p for p in _git("diff", "--name-only", rng).splitlines() if p]


def main() -> int:
    tag = _last_tag()
    current = tag[1:] if tag else "0.0.0"
    commits = _commits_since(tag)
    functional = any(_is_functional(p) for p in _changed_paths(tag))
    bump, changes = bump_for(commits, functional)
    print(json.dumps({
        "releasable": bump != "none",
        "bump": bump,
        "current_version": current,
        "next_version": next_version(current, bump),
        "functional_changed": functional,
        "changes": changes,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
