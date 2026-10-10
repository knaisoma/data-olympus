"""Pinned adoption ratification inputs for release workflows.

These constants are trusted only as checked into main. Stage two imports them
from its own main checkout. Stage one runs the code at H, and promotion runs
from the release squash S, whose tree equals H, so both read them (and the
vendored standards) as Git blobs of main instead: stage one from origin/main,
promotion from the RC's recorded M. See scripts/rc_decide.py and
docs/releases/adoption-cut-runbook.md. They are never taken from workflow
inputs, release evidence or the tree of H.

A record cut needs two ratifications (STD-U-821 amendment 1.5): RATIFIED with
STANDARD_FILE for amendment 1.3 (the adoption record) and EXTENSION_RATIFIED
with EXTENSION_STANDARD_FILE for amendment 1.5 (its reuse after a release).
Each value is <date>:<ref>; the engine refuses any other shape.
"""
from __future__ import annotations

from pathlib import Path
from typing import TypedDict

RATIFIED = "2026-10-07:knaisoma/company-knowledge@785bb77"
STANDARD_FILE = "docs/releases/std-u-821-amendment-1.3.md"
EXTENSION_RATIFIED = "2026-10-10:knaisoma/company-knowledge@2441459"
EXTENSION_STANDARD_FILE = "docs/releases/std-u-821-amendment-1.5.md"
# Path of this module, so stage one can read main's copy as a Git blob.
MODULE = "scripts/adoption_ratification.py"


def engine_args() -> list[str]:
    """Return version-engine arguments relative to the repository root."""
    return ["--adoption-ratified", RATIFIED, "--standard-file", STANDARD_FILE,
            "--extension-ratified", EXTENSION_RATIFIED,
            "--extension-standard-file", EXTENSION_STANDARD_FILE]


class EngineKwargs(TypedDict):
    adoption_ratified: str
    standard_file: Path
    extension_ratified: str
    extension_standard_file: Path


def engine_kwargs(root: Path) -> EngineKwargs:
    """compute_version keyword arguments, resolved against a trusted checkout."""
    return {"adoption_ratified": RATIFIED, "standard_file": Path(root) / STANDARD_FILE,
            "extension_ratified": EXTENSION_RATIFIED,
            "extension_standard_file": Path(root) / EXTENSION_STANDARD_FILE}
