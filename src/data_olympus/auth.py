"""Structural rule (always applies, not configurable) + policy blocklist
(operator-configurable, empty by default).

Rules:
- Structural rule rejects: empty, NUL, absolute, traversal, non-md, non-indexed,
  structurally-excluded. Normalization happens BEFORE prefix match.
- Policy blocklist consists of tier names (e.g. {"T1"}) and fnmatch globs
  (e.g. ["decisions/ADR-008-*.md"]).

By default both blocklists are empty (allow-by-default modulo the structural rule).
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import PurePosixPath

# Generic, deployment-neutral default. A deployment with extra top-level
# directories supplies its own writable set via KB_INDEXED_PREFIXES (a
# comma-separated list), which replaces this default wholesale.
DEFAULT_INDEXED_PREFIXES: tuple[str, ...] = (
    "universal/", "tech-stacks/", "projects/",
    "decisions/", "workflows/", "memory/",
    "tooling/", "templates/",
)


def indexed_prefixes() -> tuple[str, ...]:
    """Active writable prefixes: KB_INDEXED_PREFIXES if set, else default."""
    raw = os.environ.get("KB_INDEXED_PREFIXES", "").strip()
    if not raw:
        return DEFAULT_INDEXED_PREFIXES
    return tuple(s.strip() for s in raw.split(",") if s.strip())

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
    """Structural rule. Independent of policy blocklist."""
    canonical = _normalize_target_path(target_path)
    if canonical is None:
        return False
    if not canonical.endswith(".md"):
        return False
    return any(canonical.startswith(p) for p in indexed_prefixes())


def safe_join_under_root(root: str, target_path: str) -> str | None:
    """Join ``target_path`` under ``root`` and return the absolute path only when
    it resolves to a location strictly inside ``root``; return None otherwise.

    Defence against symlink escape and traversal: ``os.path.realpath`` resolves
    every symlink in the existing path prefix (and any ``..`` segments), so a
    pre-existing malicious symlink in the checked-out tree (e.g. ``memory/inbox``
    pointing outside the worktree) makes the resolved path fall outside ``root``
    and is rejected here, before any ``os.makedirs`` / ``open`` side effect.

    The returned value is the *unresolved* join (``os.path.join(root,
    target_path)``) so callers keep writing to the in-tree path and ``git add``
    the relative ``target_path`` exactly as before; returning the resolved real
    path would change those semantics. This is the single shared containment
    guard used by every write path (memory propose, edit, resolve, bootstrap).
    """
    root_real = os.path.realpath(root)
    full = os.path.join(root, target_path)
    real = os.path.realpath(full)
    if real != root_real and real.startswith(root_real + os.sep):
        return full
    return None


class PathBlocklist:
    """Operator-configured per-tier + per-path blocklist. Both default empty."""

    def __init__(self, tier_blocks: list[str], path_blocks: list[str]) -> None:
        self._tier_blocks = {t.strip() for t in tier_blocks if t.strip()}
        self._path_blocks = [p.strip() for p in path_blocks if p.strip()]

    def blocks(self, target_path: str, target_tier: str) -> bool:
        if target_tier in self._tier_blocks:
            return True
        return any(fnmatch.fnmatch(target_path, p) for p in self._path_blocks)
