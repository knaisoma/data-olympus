#!/usr/bin/env python3
"""Print the next release-candidate channel tag `X.Y.Z-rc.N`.

CI usage: pipe the existing ghcr tags (one per line) on stdin and pass the
target base version. N is one past the highest existing `<base>-rc.N`.

    gh api "/orgs/knaisoma/packages/container/data-olympus/versions" \
      --jq '.[].metadata.container.tags[]' | python3 scripts/rc_tag.py --base 0.5.0
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.compute_release import next_rc_tag  # noqa: E402

if TYPE_CHECKING:
    from typing import TextIO


def main(argv: list[str] | None = None, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rc_tag")
    parser.add_argument("--base", required=True, help="target base version X.Y.Z")
    args = parser.parse_args(argv)
    src = stdin if stdin is not None else sys.stdin
    existing = [line for line in src.read().splitlines() if line.strip()]
    print(next_rc_tag(args.base, existing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
