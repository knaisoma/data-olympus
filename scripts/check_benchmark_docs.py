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

import json
import sys
from pathlib import Path

# Make the repo root importable so `benchmarks` resolves when run from CI.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


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
    return verify_receipt(document, repo_root)


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
