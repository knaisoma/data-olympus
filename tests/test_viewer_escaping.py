"""item 7: viewer HTML/JS injection + substitution-ordering regression tests."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.viewer.generator import generate_visualization

if TYPE_CHECKING:
    from pathlib import Path


def _write_doc(root: Path, name: str, body: str, title: str = "Doc") -> None:
    (root / name).write_text(
        f"---\nid: {name[:-3]}\ntitle: {title}\ntype: concept\n---\n{body}\n",
        encoding="utf-8",
    )


def test_script_close_tag_in_body_is_escaped(tmp_path: Path) -> None:
    """A doc body containing </script> must not break out of the <script> block."""
    root = tmp_path / "bundle"
    root.mkdir()
    _write_doc(root, "a.md", "before </script><script>window.__pwned__=1;</script> after")
    out = tmp_path / "viz.html"
    generate_visualization(root, out, name="B")
    html = out.read_text(encoding="utf-8")
    # The raw closing tag must NOT appear inside the embedded JSON payload.
    assert "</script><script>window.__pwned__" not in html
    # It must be present only in its escaped form.
    assert "<\\/script>" in html


def test_display_name_placeholder_in_body_not_mangled(tmp_path: Path) -> None:
    """A doc body containing the literal __DISPLAY_NAME__ must survive verbatim
    (single-pass substitution), not get rewritten to the bundle name."""
    root = tmp_path / "bundle"
    root.mkdir()
    _write_doc(root, "a.md", "the literal __DISPLAY_NAME__ token here")
    out = tmp_path / "viz.html"
    generate_visualization(root, out, name="MyBundle")
    html = out.read_text(encoding="utf-8")
    # The placeholder inside the body's JSON payload is preserved verbatim.
    assert "the literal __DISPLAY_NAME__ token here" in html
    # And the real title slot got the bundle name.
    assert "MyBundle - data-olympus viewer" in html


def test_json_placeholder_in_display_name_not_mangled(tmp_path: Path) -> None:
    """A display name containing __JSON__ must not swallow the payload."""
    root = tmp_path / "bundle"
    root.mkdir()
    _write_doc(root, "a.md", "plain body")
    out = tmp_path / "viz.html"
    generate_visualization(root, out, name="__JSON__weird")
    html = out.read_text(encoding="utf-8")
    # The title slot keeps the literal name; the payload is still valid.
    assert "__JSON__weird - data-olympus viewer" in html
    assert "window.__BUNDLE_DATA__ = {" in html
