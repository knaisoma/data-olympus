"""Tests for the structural rule (always applies) + policy blocklist (configurable)."""
from __future__ import annotations

from data_olympus.auth import (
    PathBlocklist,
    is_writable_path,
    normalize_target_path,
)

# ---- Structural rule (Codex blocker 2 fix: path traversal rejected) ----


def test_structural_rule_accepts_universal_md() -> None:
    assert is_writable_path("universal/foundation/STD-U-001.md") is True


def test_structural_rule_accepts_tech_stacks_md() -> None:
    assert is_writable_path("tech-stacks/backend-nestjs/STD-BN-001.md") is True


def test_structural_rule_accepts_projects_t3_md() -> None:
    assert is_writable_path("projects/example-project/README.md") is True


def test_structural_rule_rejects_non_md() -> None:
    assert is_writable_path("universal/foundation/STD-U-001.txt") is False


def test_structural_rule_rejects_non_indexed_prefix() -> None:
    assert is_writable_path("secrets/api-keys.md") is False


def test_structural_rule_rejects_structurally_excluded_dir() -> None:
    assert is_writable_path("tools/foo.md") is False


def test_structural_rule_rejects_traversal_double_dot() -> None:
    assert is_writable_path("projects/foo/../../memory/x.md") is False


def test_structural_rule_rejects_absolute_path() -> None:
    assert is_writable_path("/etc/passwd.md") is False


def test_structural_rule_rejects_drive_letter_absolute() -> None:
    assert is_writable_path("C:/Windows/x.md") is False


def test_structural_rule_rejects_nul_byte() -> None:
    assert is_writable_path("projects/example-project/foo\x00.md") is False


def test_structural_rule_rejects_empty_input() -> None:
    assert is_writable_path("") is False
    assert is_writable_path("   ") is False


def test_structural_rule_rejects_empty_segment_from_double_slash() -> None:
    assert is_writable_path("projects//foo.md") is False


# ---- Canonical path handling (item 4: backslash bypass + control chars) ----


def test_normalize_folds_backslashes_to_canonical() -> None:
    # A backslash path validates as its forward-slash canonical form...
    assert normalize_target_path("decisions\\x.md") == "decisions/x.md"
    assert is_writable_path("decisions\\x.md") is True


def test_normalize_returns_canonical_not_raw() -> None:
    # ...and the canonical form is what callers must use downstream, so it never
    # carries a literal backslash that would land a root-level file on Linux.
    canonical = normalize_target_path("decisions\\sub\\x.md")
    assert canonical == "decisions/sub/x.md"
    assert "\\" not in canonical


def test_normalize_rejects_newline_control_char() -> None:
    # A newline in a path is never legitimate and would smuggle YAML/audit content.
    assert normalize_target_path("decisions/x\n.md") is None
    assert is_writable_path("decisions/x\n.md") is False


def test_normalize_rejects_carriage_return_and_tab() -> None:
    assert normalize_target_path("decisions/x\r.md") is None
    assert normalize_target_path("decisions/x\t.md") is None


def test_normalize_rejects_nul_via_control_range() -> None:
    assert normalize_target_path("decisions/x\x00.md") is None


def test_normalize_rejects_backslash_traversal() -> None:
    # Backslash-encoded traversal folds to '..' segments and is rejected.
    assert normalize_target_path("projects\\..\\..\\memory\\x.md") is None
    assert is_writable_path("projects\\..\\..\\memory\\x.md") is False


def test_normalize_rejects_backslash_into_excluded_dir() -> None:
    assert normalize_target_path("tools\\foo.md") is None
    assert is_writable_path("tools\\foo.md") is False


# ---- Policy blocklist (empty by default; configurable) ----


def test_blocklist_empty_default_allows_all() -> None:
    bl = PathBlocklist(tier_blocks=[], path_blocks=[])
    assert bl.blocks("universal/foundation/STD-U-001.md", "T1") is False
    assert bl.blocks("projects/example-project/README.md", "T3") is False


def test_blocklist_tier_blocks_match() -> None:
    bl = PathBlocklist(tier_blocks=["T1"], path_blocks=[])
    assert bl.blocks("universal/foundation/STD-U-001.md", "T1") is True
    assert bl.blocks("projects/example-project/README.md", "T3") is False


def test_blocklist_path_glob_blocks_match() -> None:
    bl = PathBlocklist(tier_blocks=[], path_blocks=["decisions/DEC-008-*.md"])
    assert bl.blocks("decisions/DEC-008-instruction-file-standard.md", "decisions") is True
    assert bl.blocks("decisions/DEC-009-other.md", "decisions") is False


def test_blocklist_combines_tier_and_path() -> None:
    bl = PathBlocklist(tier_blocks=["T1"], path_blocks=["memory/accepted/note-a.md"])
    assert bl.blocks("universal/foundation/STD-U-001.md", "T1") is True
    assert bl.blocks("memory/accepted/note-a.md", "memory") is True
    assert bl.blocks("memory/accepted/note-b.md", "memory") is False
