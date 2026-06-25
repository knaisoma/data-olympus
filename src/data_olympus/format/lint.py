"""Walk a bundle directory and validate every concept document."""

from __future__ import annotations

from pathlib import Path

from .document import Document
from .validate import RESERVED, Finding, validate_document

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


def discover_bundle_files(root: str | Path) -> list[Path]:
    """Return the sorted '*.md' files under root that are subject to concept
    linting, i.e. everything `lint_bundle` would validate.

    This is the single source of truth for which files a bundle lints. The CLI
    uses the length of this list to report how many files were actually linted
    and to fail when a bundle has no concepts to lint (otherwise a broken walk
    would silently pass as "0 errors across 0 files").

    Returns only files subject to the concept schema. Skipped:
    - Files inside vendor/VCS/archival/meta directories (_SKIP_DIRS).
    - Well-known repo-meta filenames that sit DIRECTLY at the bundle root
      (_ROOT_META_FILES).  The same filename in a subdirectory is NOT skipped.
    - Reserved filenames (`index.md`, `log.md`, `template.md`), which
      `validate_document` exempts from the concept schema and so can never
      produce a finding.  Counting them as "linted" would let a bundle that has
      lost all its concept docs but kept its generated indexes still pass the
      zero-file guard.
    """
    root = Path(root)
    files: list[Path] = []
    for md in sorted(root.rglob("*.md")):
        if any(part in _SKIP_DIRS for part in md.parts):
            continue
        if md.parent == root and md.name in _ROOT_META_FILES:
            continue
        if md.name in RESERVED:
            continue
        files.append(md)
    return files


def lint_files(files: list[Path]) -> dict[Path, list[Finding]]:
    """Validate an already-discovered list of concept files. Returns {path:
    findings} for any file that produced at least one finding.

    Pair with `discover_bundle_files` to lint a bundle in a single traversal."""
    results: dict[Path, list[Finding]] = {}
    for md in files:
        findings = validate_document(Document.load(md))
        if findings:
            results[md] = findings
    return results


def lint_bundle(root: str | Path) -> dict[Path, list[Finding]]:
    """Validate every concept '*.md' under root. Returns {path: findings} for any
    file that produced at least one finding.

    File discovery (which files are validated vs skipped) is delegated to
    `discover_bundle_files`.
    """
    return lint_files(discover_bundle_files(root))
