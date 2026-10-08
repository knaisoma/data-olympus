"""Pinned adoption ratification inputs for release workflows."""

RATIFIED = "2026-10-07:knaisoma/company-knowledge@785bb77"
STANDARD_FILE = "docs/releases/std-u-821-amendment-1.3.md"


def engine_args() -> list[str]:
    """Return version-engine arguments relative to the repository root."""
    return ["--adoption-ratified", RATIFIED, "--standard-file", STANDARD_FILE]
