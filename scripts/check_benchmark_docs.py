#!/usr/bin/env python3
"""CI guard: benchmark numbers quoted in the docs must match the results.

The tables in ``docs/comparison.md`` and the headline table in ``WHY.md`` are
generated from the committed result JSONs by ``benchmarks.docs_tables`` between
``<!-- BENCH:<name> START/END -->`` markers. This guard regenerates each block
and fails when a committed doc has drifted from the results (a hand-edited or
stale number), so the docs can never silently disagree with the benchmark
artifacts. Fix drift with ``python -m benchmarks.docs_tables --write``.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path

# Make the repo root importable so `benchmarks` resolves when run from CI.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_DEPENDENCY_LOCK_MISMATCH = "dependency_lock does not match uv.lock"
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_ROOT_LOCK_VERSION = re.compile(
    r'(?m)(^name = "data-olympus"\nversion = ")([^"]+)("$)'
)


def _release_root_version_only_lock_drift(
    document: Mapping[str, object],
    repo_root: Path,
) -> bool:
    """Accept a release metadata bump only when every other lock byte matches."""
    expected_lock = document.get("dependency_lock")
    if not isinstance(expected_lock, Mapping):
        return False
    expected_sha = expected_lock.get("sha256")
    packages = expected_lock.get("packages")
    if not isinstance(expected_sha, str) or not isinstance(packages, list):
        return False
    expected_roots = [
        package
        for package in packages
        if isinstance(package, Mapping) and package.get("name") == "data-olympus"
    ]
    if len(expected_roots) != 1:
        return False
    expected_version = expected_roots[0].get("version")
    if not isinstance(expected_version, str) or not _VERSION.fullmatch(
        expected_version
    ):
        return False

    try:
        lock = (repo_root / "uv.lock").read_text(encoding="utf-8")
        project = tomllib.loads(
            (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        )
    except (OSError, tomllib.TOMLDecodeError):
        return False
    matches = list(_ROOT_LOCK_VERSION.finditer(lock))
    if len(matches) != 1:
        return False
    current_version = matches[0].group(2)
    project_table = project.get("project")
    if (
        not isinstance(project_table, dict)
        or project_table.get("name") != "data-olympus"
        or project_table.get("version") != current_version
        or current_version == expected_version
        or not _VERSION.fullmatch(current_version)
    ):
        return False

    historical_lock = _ROOT_LOCK_VERSION.sub(
        rf"\g<1>{expected_version}\g<3>",
        lock,
        count=1,
    )
    return hashlib.sha256(historical_lock.encode("utf-8")).hexdigest() == expected_sha


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
    problems = verify_receipt(document, repo_root)
    if problems == [_DEPENDENCY_LOCK_MISMATCH] and (
        _release_root_version_only_lock_drift(document, repo_root)
    ):
        return []
    return problems


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
