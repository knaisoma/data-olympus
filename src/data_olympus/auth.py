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


def normalize_target_path(raw: str) -> str | None:
    """Return the canonical relative POSIX path, or None if structurally invalid.

    The returned string is the SINGLE authoritative form of the path: backslashes
    are folded to ``/`` and the segments are re-joined with POSIX separators.
    Every downstream operation (classification, blocklist match, filesystem join,
    ``git add``) MUST use this canonical value, never the raw input. If callers
    validated with the canonical form but then joined/wrote/git-added the raw
    string, a value like ``decisions\\x.md`` would pass validation as
    ``decisions/x.md`` on Linux yet land a literal root-level file named
    ``decisions\\x.md`` that is outside every indexed prefix and invisible to
    ``KB_WRITE_BLOCK_PATHS`` globs. Returning the canonical form and using it
    everywhere keeps the policy decision and the filesystem effect in lockstep.

    Rejections: empty/whitespace-only, NUL or any other control character
    (``\\n``, ``\\r``, ``\\t``, etc. would let a payload smuggle newlines into a
    path), absolute paths, Windows drive letters, ``.``/``..`` traversal, and any
    structurally-excluded segment.
    """
    if not raw or not raw.strip():
        return None
    # Reject control characters (NUL, newline, CR, tab, and the rest of the
    # C0/C1 range) before any normalization: a newline in a path is never a
    # legitimate target and would otherwise smuggle through classification and
    # audit records.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        return None
    parts = raw.replace("\\", "/").split("/")
    if any(p == "" or p == "." or p == ".." for p in parts):
        return None
    if any(p in STRUCTURALLY_EXCLUDED for p in parts):
        return None
    return str(PurePosixPath(*parts))


# Back-compat alias for the previous private name; both point at the same guard.
_normalize_target_path = normalize_target_path


def is_writable_path(target_path: str) -> bool:
    """Structural rule. Independent of policy blocklist.

    Callers that go on to write MUST first resolve the canonical form with
    :func:`normalize_target_path` and operate on that; this predicate only
    answers whether the canonical form is a writable indexed ``.md`` path.
    """
    canonical = normalize_target_path(target_path)
    if canonical is None:
        return False
    if not canonical.endswith(".md"):
        return False
    return any(canonical.startswith(p) for p in indexed_prefixes())


def path_rejection_reason(raw_target_path: str) -> str:
    """Why a path fails :func:`is_writable_path`, for a caller that already knows
    it does. Three distinct causes were previously collapsed into one opaque
    reason string (``not_md_or_excluded`` / ``traversal_or_excluded``), which
    reads identically whether the path was a malicious traversal attempt or a
    perfectly legitimate path outside the deployment's configured
    ``KB_INDEXED_PREFIXES`` - the latter is an ordinary, expected outcome for any
    deployment whose repo layout has top-level directories beyond the generic
    default (see ``indexed_prefixes()``), not a security event, and conflating
    the two sent at least one operator hunting for a traversal bug that was
    actually a missing ``KB_INDEXED_PREFIXES`` entry.

    Returns one of: ``structurally_invalid`` (empty/control-chars/absolute/
    traversal/excluded-segment - :func:`normalize_target_path` rejected it),
    ``not_markdown``, or ``not_in_indexed_prefixes`` (a syntactically fine path
    outside every configured prefix - a deployment-configuration gap, not a
    structural or security rejection).
    """
    canonical = normalize_target_path(raw_target_path)
    if canonical is None:
        return "structurally_invalid"
    if not canonical.endswith(".md"):
        return "not_markdown"
    return "not_in_indexed_prefixes"


def safe_join_under_root(root: str, target_path: str) -> str | None:
    """Join ``target_path`` under ``root`` and return the absolute path only when
    it resolves to exactly that lexical location inside ``root``; else return None.

    Two checks, both required:

    1. The realpath-resolved location is strictly inside ``root``. This rejects
       traversal and symlinks that point *outside* the worktree (e.g. a malicious
       ``memory/inbox`` symlinked to ``/etc``).
    2. The resolved location equals the lexical join of ``root`` and
       ``target_path``. This additionally rejects a symlink component that
       redirects to *another in-root path*: without it, an allowed lexical
       ``target_path`` (which is what gets classified, blocklist-checked, audited,
       and ``git add``-ed) could land its bytes on a different file, decoupling
       the policy decision from the filesystem effect.

    The returned value is the *unresolved* join so callers keep writing to the
    in-tree path and ``git add`` the relative ``target_path`` exactly as before.
    This is the single shared containment guard used by every write path (memory
    propose, edit, resolve, bootstrap).
    """
    root_real = os.path.realpath(root)
    full = os.path.join(root, target_path)
    real = os.path.realpath(full)
    expected = os.path.normpath(os.path.join(root_real, target_path))
    inside = real != root_real and real.startswith(root_real + os.sep)
    if inside and real == expected:
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
