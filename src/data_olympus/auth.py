"""Structural rule (always applies, not configurable) + policy blocklist
(operator-configurable, empty by default).

Per spec §2.8 rev 2:
- Structural rule rejects: empty, NUL, absolute, traversal, non-md, non-indexed,
  structurally-excluded. Normalization happens BEFORE prefix match.
- Policy blocklist consists of tier names (e.g. {"T1"}) and fnmatch globs
  (e.g. ["decisions/GDEC-008-*.md"]).

Per operator decision 2026-06-01: both blocklists default to empty (allow-by-default
modulo the structural rule).
"""
from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

INDEXED_PREFIXES: tuple[str, ...] = (
    "universal/", "tech-stacks/", "projects/",
    "decisions/", "workflows/", "operator/",
    "tooling/", "audits/", "plans/",
    "workforce/", "templates/",
)

STRUCTURALLY_EXCLUDED: frozenset[str] = frozenset({
    ".git", ".worktrees", ".ruff_cache", "__pycache__",
    "to-delete", "archive", "_archive", "tools",
    ".pytest_cache", ".mypy_cache", "node_modules",
})


def _normalize_target_path(raw: str) -> str | None:
    """Return the canonical relative POSIX path, or None if structurally invalid."""
    if not raw or not raw.strip():
        return None
    if "\x00" in raw:
        return None
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        return None
    parts = raw.replace("\\", "/").split("/")
    if any(p == "" or p == "." or p == ".." for p in parts):
        return None
    if any(p in STRUCTURALLY_EXCLUDED for p in parts):
        return None
    return str(PurePosixPath(*parts))


def is_writable_path(target_path: str) -> bool:
    """Structural rule. Independent of operator policy."""
    canonical = _normalize_target_path(target_path)
    if canonical is None:
        return False
    if not canonical.endswith(".md"):
        return False
    return any(canonical.startswith(p) for p in INDEXED_PREFIXES)


class PathBlocklist:
    """Operator-configured per-tier + per-path blocklist. Both default empty."""

    def __init__(self, tier_blocks: list[str], path_blocks: list[str]) -> None:
        self._tier_blocks = {t.strip() for t in tier_blocks if t.strip()}
        self._path_blocks = [p.strip() for p in path_blocks if p.strip()]

    def blocks(self, target_path: str, target_tier: str) -> bool:
        if target_tier in self._tier_blocks:
            return True
        return any(fnmatch.fnmatch(target_path, p) for p in self._path_blocks)
