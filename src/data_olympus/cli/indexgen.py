"""Regenerate index.md files for progressive disclosure."""

from __future__ import annotations

from pathlib import Path

from data_olympus.format import Document
from data_olympus.format.validate import RESERVED as _RESERVED

_SKIP = {".git", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache", "node_modules"}


def _dirs_with_markdown(root: Path) -> list[Path]:
    dirs: set[Path] = set()
    for md in root.rglob("*.md"):
        if any(part in _SKIP for part in md.parts):
            continue
        if md.name in _RESERVED:
            continue
        dirs.add(md.parent)
    return sorted(dirs)


def _entry(doc_path: Path) -> tuple[str, str, str]:
    """Return (title, relative_url, description) for a concept file."""
    doc = Document.load(doc_path)
    title = doc.frontmatter.get("title") or doc_path.stem
    desc = doc.frontmatter.get("description") or ""
    return str(title), doc_path.name, str(desc)


def regenerate_indexes(root: Path) -> list[Path]:
    """Write an index.md into every directory that holds concept docs.
    Returns the list of index.md paths written."""
    root = Path(root)
    written: list[Path] = []
    for d in _dirs_with_markdown(root):
        concepts = sorted(p for p in d.glob("*.md") if p.name not in _RESERVED)
        subdirs = sorted(
            sub
            for sub in d.iterdir()
            if sub.is_dir() and sub.name not in _SKIP and any(sub.rglob("*.md"))
        )
        lines: list[str] = [f"# {d.name or root.name}", ""]
        if concepts:
            lines.append("# Concepts")
            for c in concepts:
                title, url, desc = _entry(c)
                suffix = f" - {desc}" if desc else ""
                lines.append(f"* [{title}]({url}){suffix}")
            lines.append("")
        if subdirs:
            lines.append("# Subdirectories")
            for sub in subdirs:
                lines.append(f"* [{sub.name}/]({sub.name}/)")
            lines.append("")
        idx_path = d / "index.md"
        idx_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        written.append(idx_path)
    return written
