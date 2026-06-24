"""Walk a bundle directory and validate every concept document."""

from __future__ import annotations

from pathlib import Path

from .document import Document
from .validate import Finding, validate_document

# Directories whose contents are never KB concepts.  Kept in sync with
# _EXCLUDED_DIR_NAMES in src/data_olympus/index.py — if you add entries
# there, add them here too (and vice-versa).
_SKIP_DIRS = frozenset({
    # VCS / tooling
    ".git", "__pycache__", ".venv", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules",
    # Repo-meta / CI
    ".github", ".worktrees",
    # Archival and scratch
    "archive", "_archive", "to-delete",
    # Test data (fixture trees contain intentional duplicates)
    "test-fixtures", "cli-fixtures",
})

# Well-known repo-meta files that may live at the bundle root without being KB
# concepts.  Only files DIRECTLY under the bundle root are skipped; the same
# filename nested inside any subdirectory is a legitimate concept document and
# MUST still be validated (e.g. projects/acme-app/README.md is a project doc).
_ROOT_META_FILES = frozenset({
    "README.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md",
    "CHANGELOG.md", "NOTICE.md", "LICENSE.md", "AGENTS.md", "CLAUDE.md",
    "GEMINI.md",
})


def lint_bundle(root: str | Path) -> dict[Path, list[Finding]]:
    """Validate every '*.md' under root. Returns {path: findings} for any
    file that produced at least one finding.

    Skipped:
    - Files inside vendor/VCS/archival/meta directories (_SKIP_DIRS).
    - Well-known repo-meta filenames that sit DIRECTLY at the bundle root
      (_ROOT_META_FILES).  The same filename in a subdirectory is NOT skipped.
    """
    root = Path(root)
    results: dict[Path, list[Finding]] = {}
    for md in sorted(root.rglob("*.md")):
        if any(part in _SKIP_DIRS for part in md.parts):
            continue
        if md.parent == root and md.name in _ROOT_META_FILES:
            continue
        findings = validate_document(Document.load(md))
        if findings:
            results[md] = findings
    return results
