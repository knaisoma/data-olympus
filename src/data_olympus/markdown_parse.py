"""Lenient markdown front-matter parser. No YAML dependency; supports the simple
key: value and key: [a, b] forms used in the KB."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_LIST_RE = re.compile(r"^\[(.*)\]$")


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


def _parse_front_matter(raw: str) -> dict[str, str | list[str]]:
    """Parse simple key: value lines. Returns empty dict on malformed input."""
    out: dict[str, str | list[str]] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        m = _LIST_RE.match(value)
        if m:
            parts = [p.strip().strip("'\"") for p in m.group(1).split(",") if p.strip()]
            out[key] = parts
        else:
            out[key] = value.strip("'\"")
    return out


def parse_file(path: Path) -> ParsedDoc:
    """Read a markdown file and return a ParsedDoc.

    Raises FileNotFoundError if path does not exist.
    Returns empty metadata fields if no valid front matter.
    """
    text = path.read_text(encoding="utf-8")
    match = _FM_RE.match(text)
    if match:
        try:
            fm = _parse_front_matter(match.group(1))
        except Exception:
            fm = {}
        body = text[match.end():]
    else:
        fm = {}
        body = text

    tags = fm.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    # Lenient: if YAML was malformed enough to leave colons in keys, treat as empty
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
        tags=[str(t) for t in tags],
        title=str(fm.get("title", "")),
        body=body,
        git_remote_url=git_remote_url,
    )
