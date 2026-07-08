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
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    contradicts: list[str] = field(default_factory=list)


def _as_str_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _as_id_list(value: object) -> list[str]:
    """Normalize a decision-chain reference field (``supersedes``,
    ``contradicts``) authored as either a single scalar ID or a list of IDs
    into a list of strings (issue #110).

    This is the lenient, index-time normalization: any shape that isn't
    exactly "absent", "a string", or "a list" is treated as empty here (never
    raises). Precise shape validation (e.g. a non-string entry inside the
    list) is a `kb lint` concern (`data_olympus.format.lint`), which inspects
    the raw frontmatter value directly rather than this coerced form.
    """
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def parse_file(path: Path) -> ParsedDoc:
    """Read a markdown file and return a ParsedDoc.

    Raises FileNotFoundError if path does not exist. Malformed front matter is
    treated as no front matter (lenient): returns empty metadata fields. Prefer
    :func:`parse_text` when the caller already has the raw text (e.g. the index
    build reads each file exactly once and reuses the text for both parsing and
    the stored ``content_markdown``).
    """
    return parse_text(path, path.read_text(encoding="utf-8"))


def parse_text(path: Path, text: str) -> ParsedDoc:
    """Parse already-read markdown ``text`` into a ParsedDoc (no file read).

    Split out from :func:`parse_file` so a caller that already holds the raw text
    parses it without a second disk read (finding (i)): the index build reads a
    file once and reuses that text for the parse AND the stored full markdown.
    Malformed front matter is treated as no front matter (lenient); the malformed
    flag is surfaced separately via :func:`parse_text_checked` so the build can
    warn rather than silently dropping status/supersedes on a YAML typo.
    """
    return parse_text_checked(path, text)[0]


def parse_text_checked(path: Path, text: str) -> tuple[ParsedDoc, bool]:
    """Like :func:`parse_text`, but also return whether front matter was malformed.

    Returns ``(doc, malformed)``. ``malformed`` is True when a front-matter block
    was present (the text opens with ``---``) but failed to parse as valid YAML
    mapping, so its ``status`` / ``supersedes`` / other fields were silently
    dropped (finding (j)). The index build uses this to emit a WARN log and
    increment a health-visible counter, since a doc whose ``status`` is lost has
    its staleness protection quietly disabled. A document with genuinely NO front
    matter (does not open with ``---``) is NOT malformed.
    """
    malformed = False
    try:
        fm, body = parse_frontmatter(text)
    except ValueError:
        fm, body = {}, text
        # Only a PRESENT-but-broken block is "malformed"; a doc with no front
        # matter at all (first line is not ``---``) is a normal, valid case.
        malformed = text.lstrip().startswith("---")

    id_value = fm.get("id", "")
    if not isinstance(id_value, str) or ":" in id_value:
        id_value = ""

    git_remote_url = fm.get("git_remote_url")
    if not isinstance(git_remote_url, str) or not git_remote_url.strip():
        git_remote_url = None

    superseded_by = fm.get("superseded_by")
    if not isinstance(superseded_by, str) or not superseded_by.strip():
        superseded_by = None

    doc = ParsedDoc(
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
        supersedes=_as_id_list(fm.get("supersedes")),
        superseded_by=superseded_by,
        contradicts=_as_id_list(fm.get("contradicts")),
    )
    return doc, malformed
