"""Walk a bundle directory and validate every concept document."""

from __future__ import annotations

from pathlib import Path

from .document import Document
from .validate import Finding, validate_document

_SKIP_DIRS = frozenset({
    ".git", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache", "node_modules"
})


def lint_bundle(root: str | Path) -> dict[Path, list[Finding]]:
    """Validate every '*.md' under root. Returns {path: findings} for any
    file that produced at least one finding. Skips VCS/vendor directories."""
    root = Path(root)
    results: dict[Path, list[Finding]] = {}
    for md in sorted(root.rglob("*.md")):
        if any(part in _SKIP_DIRS for part in md.parts):
            continue
        findings = validate_document(Document.load(md))
        if findings:
            results[md] = findings
    return results
