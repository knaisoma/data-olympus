"""Unit tests for the shared path-containment guard safe_join_under_root.

This is the single guard every write path relies on (memory propose, edit,
resolve, bootstrap), so its symlink/traversal behavior is tested here directly
rather than only through each call site.
"""
from __future__ import annotations

import os

from data_olympus.auth import safe_join_under_root


def test_normal_path_is_joined(tmp_path) -> None:
    root = str(tmp_path / "root")
    os.makedirs(root)
    got = safe_join_under_root(root, "memory/inbox/x.md")
    assert got == os.path.join(root, "memory/inbox/x.md")


def test_traversal_escape_returns_none(tmp_path) -> None:
    root = str(tmp_path / "root")
    os.makedirs(root)
    assert safe_join_under_root(root, "../outside.md") is None
    assert safe_join_under_root(root, "a/../../outside.md") is None


def test_absolute_target_returns_none(tmp_path) -> None:
    root = str(tmp_path / "root")
    os.makedirs(root)
    # os.path.join with an absolute second arg discards root entirely; the
    # guard must reject this rather than write to an arbitrary absolute path.
    assert safe_join_under_root(root, "/etc/passwd") is None


def test_symlinked_parent_dir_escape_returns_none(tmp_path) -> None:
    """A pre-existing symlink inside the tree that points outside the root must
    be rejected: this is the exact memory/inbox blocker reproduction."""
    root = str(tmp_path / "root")
    outside = str(tmp_path / "outside")
    os.makedirs(root)
    os.makedirs(outside)
    os.makedirs(os.path.join(root, "memory"))
    os.symlink(outside, os.path.join(root, "memory", "inbox"))
    assert safe_join_under_root(root, "memory/inbox/escape.md") is None


def test_in_root_symlink_redirection_is_rejected(tmp_path) -> None:
    """A symlink that redirects to ANOTHER in-root path is rejected: the lexical
    target (classified/blocklisted/audited/git-added) must equal where bytes land,
    so an allowed path cannot be aimed at a different tier/file."""
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "real"))
    os.symlink(os.path.join(root, "real"), os.path.join(root, "link"))
    # Resolves to root/real/x.md (in-root) but != lexical root/link/x.md -> reject.
    assert safe_join_under_root(root, "link/x.md") is None


def test_empty_target_returns_none(tmp_path) -> None:
    root = str(tmp_path / "root")
    os.makedirs(root)
    assert safe_join_under_root(root, "") is None
