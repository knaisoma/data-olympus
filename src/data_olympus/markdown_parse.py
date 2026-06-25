"""Front-matter parsing for the index. Reuses the yaml-based parser from
data_olympus.format.frontmatter, with lenient failure (malformed -> empty)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from data_olympus.format.frontmatter import parse_frontmatter

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class ParsedDoc:
    """A parsed markdown document with optional front-matter fields."""

    path: Path
    id: str
    tier: str
    category: str
    tags: list[str] = field(default_factory=list)
    title: str = ""
    body: str = ""
    git_remote_url: str | None = None
    status: str = ""
    doc_type: str = ""
    applies_when: list[str] = field(default_factory=list)
    description: str = ""


def _as_str_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def parse_file(path: Path) -> ParsedDoc:
    """Read a markdown file and return a ParsedDoc.

    Raises FileNotFoundError if path does not exist. Malformed front matter is
    treated as no front matter (lenient): returns empty metadata fields.
    """
    text = path.read_text(encoding="utf-8")
    try:
        fm, body = parse_frontmatter(text)
    except ValueError:
        fm, body = {}, text

    id_value = fm.get("id", "")
    if not isinstance(id_value, str) or ":" in id_value:
        id_value = ""

    git_remote_url = fm.get("git_remote_url")
    if not isinstance(git_remote_url, str) or not git_remote_url.strip():
        git_remote_url = None

    return ParsedDoc(
        path=path,
        id=id_value,
        tier=str(fm.get("tier", "")),
        category=str(fm.get("category", "")),
        tags=_as_str_list(fm.get("tags", [])),
        title=str(fm.get("title", "")),
        body=body,
        git_remote_url=git_remote_url,
        status=str(fm.get("status", "")),
        doc_type=str(fm.get("type", "")),
        applies_when=_as_str_list(fm.get("applies_when", [])),
        description=str(fm.get("description", "")) if fm.get("description") is not None else "",
    )
