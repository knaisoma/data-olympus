"""Tests for the thin-pointer renderer."""
from __future__ import annotations

from data_olympus.thin_pointer import THIN_POINTER_MARKER, render_thin_pointer


def test_marker_is_stable() -> None:
    assert THIN_POINTER_MARKER == "<!-- KB-THIN-POINTER v1 -->"


def test_render_starts_with_marker_and_carries_location_and_hint() -> None:
    out = render_thin_pointer(
        kb_path="projects/foo/README.md", kb_id="projects-foo-README", kind="project",
    )
    # First non-empty line is the marker so the lint is unambiguous.
    first = next(line for line in out.splitlines() if line.strip())
    assert first == THIN_POINTER_MARKER
    # Human-readable location + agent retrieval hint both present.
    assert "projects/foo/README.md" in out
    assert "kb get projects-foo-README" in out
    assert "project" in out
