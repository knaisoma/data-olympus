"""Run the project's writing-rule gate.

A shim. The linter itself is the vendored module
`src/data_olympus/_vendor/prose_lint.py`, which the write pipeline also imports;
read its docstring for the rules, the usage, and the sync contract. This shim
needs the package importable (an editable or regular install).
"""
from __future__ import annotations

import sys

from data_olympus._vendor.prose_lint import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
