#!/usr/bin/env python3
"""CI guard: benchmark numbers quoted in the docs must match the results.

The tables in ``docs/comparison.md`` and the headline table in ``WHY.md`` are
generated from the committed result JSONs by ``benchmarks.docs_tables`` between
``<!-- BENCH:<name> START/END -->`` markers. This guard regenerates each block
and fails when a committed doc has drifted from the results (a hand-edited or
stale number), so the docs can never silently disagree with the benchmark
artifacts. Fix drift with ``python -m benchmarks.docs_tables --write``.

It also owns the *provenance* half of that contract, and the distinction is the
subtle part. A receipt describes one measurement that happened once, at one
revision, under one dependency set. That is not the same fact as the dependency
set the repository ships today, and this guard deliberately keeps them apart:

* The receipt's ``dependency_lock`` is verified against ``uv.lock`` **as
  committed at the receipt's own ``source_commit``**, read out of git history.
  That binding is checked byte for byte, so the measured environment stays
  tamper-evident.
* The working tree's current ``uv.lock`` is allowed to move freely. A dependency
  update does not invalidate a past measurement; it only means the numbers were
  measured somewhere else, which is what the published provenance label already
  says.
* ``source_tree`` is bound the same way, and for the same reason. The recorded
  digest is recomputed from the benchmark and product source **as committed at
  ``source_commit``**, never from the working tree. Recomputing the aggregate,
  rather than only walking the recorded file list, is what catches a file that
  existed at the measurement commit but was left out of the receipt.
* The working tree's current source is allowed to move freely. Comparing it to
  the receipt made every change under ``src/data_olympus/`` fail this guard, so
  no product fix could go green, and it asserted the same untruth as the lock
  comparison did.

Requiring the two to be equal is what previously made every dependency update
fail this guard, including security updates, and it asserted something untrue:
that today's dependency set produced numbers measured long before it existed.

What this does NOT establish: that the benchmarks were actually executed under
the recorded dependencies. ``build_receipt`` records committed files and
environment metadata without running anything. Claiming that current
dependencies produced the numbers requires a fresh measured run. Guarding the
*current* dependency set against artifact substitution is likewise out of scope
here and remains the job of dependency and security review.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

# Make the repo root importable so `benchmarks` resolves when run from CI.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_DEPENDENCY_LOCK_MISMATCH = "dependency_lock does not match uv.lock"
_SOURCE_TREE_MISMATCH = "source_tree sha256 does not match the repository"
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _measured_lock(repo_root: Path, source_commit: str) -> bytes | None:
    """Return ``uv.lock`` exactly as committed at the measurement revision.

    CI checks out with ``fetch-depth: 0`` precisely so this history is present;
    a shallow clone makes the measured lock unreachable and this returns None
    rather than silently skipping the check.
    """
    result = subprocess.run(
        ["git", "show", f"{source_commit}:uv.lock"],
        cwd=repo_root,
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def historical_lock_problems(
    document: Mapping[str, object],
    repo_root: Path,
) -> list[str]:
    """Verify the receipt's dependency lock against its own measurement commit.

    Reuses ``benchmarks.receipt._dependency_lock`` against a scratch copy of the
    historical file so the digest and package summary are computed by the exact
    production code path rather than a second implementation that could drift.
    """
    from benchmarks.receipt import _dependency_lock

    expected = document.get("dependency_lock")
    if not isinstance(expected, Mapping):
        return ["dependency_lock is missing from the receipt"]
    expected_sha = expected.get("sha256")
    expected_packages = expected.get("packages")
    if not isinstance(expected_sha, str) or not isinstance(expected_packages, list):
        return ["dependency_lock is malformed in the receipt"]

    source_commit = document.get("source_commit")
    if not isinstance(source_commit, str) or not _SHA_PATTERN.fullmatch(source_commit):
        return ["source_commit must be a lowercase 40 character git SHA"]

    lock = _measured_lock(repo_root, source_commit)
    if lock is None:
        return [
            "measured uv.lock is unreachable at source_commit "
            f"{source_commit}; a full-history checkout is required"
        ]
    if hashlib.sha256(lock).hexdigest() != expected_sha:
        return [
            "dependency_lock does not match uv.lock at source_commit "
            f"{source_commit}"
        ]

    with tempfile.TemporaryDirectory() as scratch:
        (Path(scratch) / "uv.lock").write_bytes(lock)
        measured = _dependency_lock(Path(scratch))
    if measured["packages"] != expected_packages:
        return [
            "dependency_lock packages do not match uv.lock at source_commit "
            f"{source_commit}"
        ]
    return []


def _measured_source(repo_root: Path, source_commit: str) -> dict[str, bytes] | None:
    """Every measured source file as committed, or None when history is absent.

    Returns the whole set rather than one file at a time so the aggregate can be
    recomputed from the commit itself. A receipt that omits a file which existed
    at the measurement commit is then a mismatch rather than a shorter walk.
    """
    from benchmarks.receipt import SOURCE_PATTERNS

    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", source_commit],
        cwd=repo_root,
        capture_output=True,
    )
    if listing.returncode != 0:
        return None
    names = [n for n in listing.stdout.decode("utf-8").split("\0") if n]
    wanted = sorted(
        name for name in names
        if any(PurePosixPath(name).full_match(pattern) for pattern in SOURCE_PATTERNS)
    )
    contents: dict[str, bytes] = {}
    for name in wanted:
        blob = subprocess.run(
            ["git", "show", f"{source_commit}:{name}"],
            cwd=repo_root,
            capture_output=True,
        )
        if blob.returncode != 0:
            return None
        contents[name] = blob.stdout
    return contents


def historical_source_tree_problems(
    document: Mapping[str, object],
    repo_root: Path,
) -> list[str]:
    """Verify the receipt's source digest against its own measurement commit.

    Materialises the committed files into a scratch tree and reuses
    ``benchmarks.receipt._file_group`` so the aggregate is produced by the exact
    code that wrote it, rather than by a second implementation that could drift.
    """
    from benchmarks.receipt import SOURCE_PATTERNS, _file_group, _relative_files

    expected = document.get("source_tree")
    if not isinstance(expected, Mapping):
        return ["source_tree is missing from the receipt"]
    expected_sha = expected.get("sha256")
    expected_files = expected.get("files")
    if not isinstance(expected_sha, str) or not isinstance(expected_files, list):
        return ["source_tree is malformed in the receipt"]

    source_commit = document.get("source_commit")
    if not isinstance(source_commit, str) or not _SHA_PATTERN.fullmatch(source_commit):
        return ["source_commit must be a lowercase 40 character git SHA"]

    measured = _measured_source(repo_root, source_commit)
    if measured is None:
        return [
            "measured source is unreachable at source_commit "
            f"{source_commit}; a full-history checkout is required"
        ]

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        for name, content in measured.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        group = _file_group(root, _relative_files(root, SOURCE_PATTERNS))
    # Both halves are compared. The digest alone would accept a receipt whose
    # file list had an entry removed while the digest was left intact, and the
    # per-file walk in verify_receipt cannot see an omission because it iterates
    # the very list being shortened.
    if group["sha256"] != expected_sha:
        return [
            "source_tree does not match the source at source_commit "
            f"{source_commit}"
        ]
    if group["files"] != expected_files:
        return [
            "source_tree file list does not match the source at source_commit "
            f"{source_commit}"
        ]
    return []


def receipt_problems(repo_root: Path) -> list[str]:
    """Verify committed benchmark evidence before checking rendered claims."""
    from benchmarks.receipt import RECEIPT_PATH, verify_receipt

    path = repo_root / RECEIPT_PATH
    if not path.is_file():
        return [f"missing benchmark receipt: {RECEIPT_PATH}"]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"invalid benchmark receipt: {exc}"]

    # Every failure except the two current-tree comparisons is preserved exactly
    # as verify_receipt reported it. Each of those is replaced by a strictly
    # separate historical check rather than suppressed, so a mismatch against
    # the measurement commit can never be hidden by dropping one.
    replaced = {_DEPENDENCY_LOCK_MISMATCH, _SOURCE_TREE_MISMATCH}
    problems = [p for p in verify_receipt(document, repo_root) if p not in replaced]
    return (
        problems
        + historical_lock_problems(document, repo_root)
        + historical_source_tree_problems(document, repo_root)
    )


def main() -> int:
    from benchmarks.docs_tables import check_or_write

    problems = receipt_problems(_ROOT)
    if not problems:
        problems = check_or_write(write=False)
    if problems:
        print("benchmark-docs guard: FAIL", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("benchmark-docs guard: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
