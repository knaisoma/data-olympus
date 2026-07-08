"""Front-matter parsing for the index. Reuses the yaml-based parser from
data_olympus.format.frontmatter, with lenient failure (malformed -> empty)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from data_olympus.format.frontmatter import parse_frontmatter
from data_olympus.format.validate import VALIDITY_DATE_FIELDS, normalize_validity_date

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
    # validity/freshness (issue #107): all dates normalized to ISO YYYY-MM-DD.
    # A malformed value anywhere in the ``validity`` block fails the WHOLE
    # block open (every field here is "" and ``validity_malformed`` is True),
    # matching format.validate._validity_findings' lint semantics: an author
    # typo must not silently half-apply a validity window.
    valid_from: str = ""
    valid_until: str = ""
    last_verified: str = ""
    recheck_by: str = ""
    verification_source: str = ""
    validity_malformed: bool = False


def _as_str_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


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

    valid_from, valid_until, last_verified, recheck_by, verification_source, validity_malformed = (
        _parse_validity(fm.get("validity"))
    )

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
        valid_from=valid_from,
        valid_until=valid_until,
        last_verified=last_verified,
        recheck_by=recheck_by,
        verification_source=verification_source,
        validity_malformed=validity_malformed,
    )
    return doc, malformed


def _parse_validity(validity: object) -> tuple[str, str, str, str, str, bool]:
    """Parse the optional ``validity`` frontmatter object.

    Returns ``(valid_from, valid_until, last_verified, recheck_by,
    verification_source, malformed)``. A malformed date value anywhere in the
    block (or ``validity`` present but not a mapping) fails the WHOLE block
    open: every field is returned empty and ``malformed`` is True, so the
    index build can warn and increment a health counter rather than silently
    indexing a partially-parsed validity window (matching
    ``format.validate``'s lint semantics for the same input).
    """
    if validity is None:
        return "", "", "", "", "", False
    if not isinstance(validity, dict):
        return "", "", "", "", "", True

    normalized: dict[str, str] = {}
    malformed = False
    for key in VALIDITY_DATE_FIELDS:
        norm, bad = normalize_validity_date(validity.get(key))
        normalized[key] = norm
        malformed = malformed or bad
    if malformed:
        return "", "", "", "", "", True

    source = validity.get("verification_source")
    verification_source = str(source) if source is not None else ""
    return (
        normalized["valid_from"],
        normalized["valid_until"],
        normalized["last_verified"],
        normalized["recheck_by"],
        verification_source,
        False,
    )
