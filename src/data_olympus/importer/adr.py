"""Parse an adr-tools directory (doc/adr/NNNN-title.md convention).

Maps each ADR file's leading number + title into an id (``ADR-NNNN``) and title,
and reads its ``## Status`` section to derive a governance status plus
``supersedes`` / ``superseded_by`` references where the ADR text records them.

adr-tools status conventions handled:
- ``Accepted``      -> status: accepted
- ``Proposed``      -> status: proposed
- ``Rejected``      -> status: rejected
- ``Deprecated``    -> status: deprecated
- ``Superseded by ADR-0005`` / ``Superseded by [ADR-0005](0005-...)`` ->
      status: superseded, superseded_by: ADR-0005
- ``Supersedes ADR-0002``                     -> supersedes: ADR-0002

The ADR body is preserved verbatim under stamped frontmatter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from data_olympus.format.validate import STATUSES

from .stamp import first_sentence, slugify

if TYPE_CHECKING:
    from pathlib import Path

# doc/adr filename convention: 4-digit number, dash, slug.
_ADR_FILENAME_RE = re.compile(r"^(?P<num>\d{1,5})[-_](?P<slug>.+?)\.md$", re.IGNORECASE)
# A reference to another ADR anywhere in a status line: ADR-0005, ADR 5, or a
# bare 4-digit number inside a "superseded by ..." clause.
_ADR_REF_RE = re.compile(r"ADR[-\s]?0*(\d{1,5})", re.IGNORECASE)
_BARE_NUM_RE = re.compile(r"\b0*(\d{1,5})\b")

# Map the leading keyword of a status line to a schema status. Only these
# spellings are recognized; an unrecognized status is reported and the doc lands
# as draft (never silently mis-stamped).
_STATUS_KEYWORDS: dict[str, str] = {
    "accepted": "accepted",
    "proposed": "proposed",
    "rejected": "rejected",
    "deprecated": "deprecated",
    "superseded": "superseded",
}


def _canonical_adr_id(num: int) -> str:
    return f"ADR-{num:04d}"


@dataclass
class ParsedADR:
    path: Path
    number: int
    doc_id: str
    slug: str
    title: str
    body: str
    status: str
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    inferences: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)


# A leading "N. " / "N) " ordinal in an ADR heading (adr-tools writes
# "# 12. Use X"); we strip it so the title is the decision, not the number.
_LEADING_ORDINAL_RE = re.compile(r"^\d+\s*[.)]\s+")


def _first_title(body: str, fallback: str) -> str:
    """Return the first ATX-heading text in the body (minus its ordinal), else
    the fallback slug."""
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            text = stripped.lstrip("#").strip().strip("*_`").strip()
            text = _LEADING_ORDINAL_RE.sub("", text).strip()
            if text:
                return text
    return fallback


def _status_section(body: str) -> str:
    """Return the text under a ``## Status`` heading (up to the next heading)."""
    lines = body.splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        stripped = line.lstrip()
        is_heading = stripped.startswith("#")
        if is_heading:
            heading = stripped.lstrip("#").strip().lower()
            if heading == "status":
                capturing = True
                continue
            if capturing:
                break  # next heading ends the Status section
        elif capturing:
            out.append(line)
    return "\n".join(out).strip()


@dataclass
class StatusInfo:
    status: str = "draft"
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    inferences: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)


def _parse_status(status_text: str, *, this_num: int) -> StatusInfo:
    """Parse an ADR ``## Status`` section into a StatusInfo.

    Each non-empty line of the Status section is examined. A line beginning with
    a recognized keyword sets the status; a "supersedes"/"superseded by" clause
    records the referenced ADR ids. Unknown status text yields draft + a review
    flag so nothing is mis-stamped.
    """
    status = "draft"
    supersedes: list[str] = []
    superseded_by: str | None = None
    inferences: list[str] = []
    needs_review: list[str] = []
    if not status_text:
        needs_review.append("no Status section found; defaulted to draft")
        return StatusInfo(status, supersedes, superseded_by, inferences, needs_review)

    recognized = False
    for raw in status_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        # "supersedes X" (this ADR replaces X) — check before the keyword map so
        # "Superseded by" (passive) is not confused with "Supersedes" (active).
        if low.startswith("supersedes") or low.startswith("supercedes"):
            for ref in _extract_refs(line, exclude=this_num):
                if ref not in supersedes:
                    supersedes.append(ref)
            recognized = True
            continue
        first_word = low.split()[0].rstrip(":.")
        mapped = _STATUS_KEYWORDS.get(first_word)
        if mapped:
            status = mapped
            recognized = True
            if mapped == "superseded":
                refs = _extract_refs(line, exclude=this_num)
                if refs:
                    superseded_by = refs[0]
                    if len(refs) > 1:
                        needs_review.append(
                            f"Status line names multiple superseding ADRs {refs}; "
                            f"used {superseded_by}"
                        )
                else:
                    needs_review.append(
                        "status 'superseded' but no superseding ADR id found in Status section"
                    )
            continue

    if not recognized:
        needs_review.append(
            f"unrecognized Status text {status_text.splitlines()[0]!r}; defaulted to draft"
        )
    if status != "draft":
        inferences.append(f"status inferred as {status!r} from ADR Status section")
    if status not in STATUSES:  # pragma: no cover - keywords are all valid
        needs_review.append(f"derived status {status!r} not in schema; forcing draft")
        status = "draft"
    return StatusInfo(status, supersedes, superseded_by, inferences, needs_review)


def _extract_refs(line: str, *, exclude: int) -> list[str]:
    """Extract referenced ADR ids from a status line, excluding self-references."""
    nums: list[int] = []
    matched = list(_ADR_REF_RE.finditer(line))
    if matched:
        nums = [int(m.group(1)) for m in matched]
    else:
        # No explicit ADR- prefix: fall back to bare numbers (adr-tools often
        # writes just "Superseded by 0007").
        nums = [int(m.group(1)) for m in _BARE_NUM_RE.finditer(line)]
    out: list[str] = []
    for n in nums:
        if n == exclude:
            continue
        ref = _canonical_adr_id(n)
        if ref not in out:
            out.append(ref)
    return out


def parse_adr_file(path: Path) -> ParsedADR | None:
    """Parse a single ADR file. Returns None when the filename does not match
    the adr-tools ``NNNN-title.md`` convention (the caller reports the skip)."""
    m = _ADR_FILENAME_RE.match(path.name)
    if not m:
        return None
    number = int(m.group("num"))
    slug = slugify(m.group("slug"))
    body = path.read_text(encoding="utf-8")
    title = _first_title(body, fallback=m.group("slug").replace("-", " ").strip())
    status_text = _status_section(body)
    info = _parse_status(status_text, this_num=number)
    return ParsedADR(
        path=path,
        number=number,
        doc_id=_canonical_adr_id(number),
        slug=slug,
        title=title,
        body=body,
        status=info.status,
        supersedes=info.supersedes,
        superseded_by=info.superseded_by,
        inferences=info.inferences,
        needs_review=info.needs_review,
    )


def discover_adr_files(source: Path) -> list[Path]:
    """Return the ADR markdown files under ``source`` in number order.

    Accepts either an adr directory directly or a repo root containing a
    ``doc/adr`` or ``docs/adr`` subtree. Only files matching the NNNN-title.md
    convention are returned; ``index.md`` / ``template.md`` are ignored.
    """
    candidates: list[Path]
    if source.is_dir():
        roots = [source]
        for sub in ("doc/adr", "docs/adr"):
            p = source / sub
            if p.is_dir():
                roots.append(p)
        seen: set[Path] = set()
        candidates = []
        for root in roots:
            for md in sorted(root.glob("*.md")):
                if md not in seen and _ADR_FILENAME_RE.match(md.name):
                    seen.add(md)
                    candidates.append(md)
    else:
        candidates = [source] if _ADR_FILENAME_RE.match(source.name) else []
    candidates.sort(key=lambda p: int(_ADR_FILENAME_RE.match(p.name).group("num")))  # type: ignore[union-attr]
    return candidates


def _section_text(body: str, name: str) -> str:
    """Return the text under a ``## <name>`` heading (case-insensitive)."""
    out: list[str] = []
    capturing = False
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            if capturing:
                break
            capturing = stripped.lstrip("#").strip().lower() == name.lower()
            continue
        if capturing:
            out.append(line)
    return "\n".join(out).strip()


def description_for(adr: ParsedADR) -> str:
    """Return a one-sentence description for the ADR.

    Prefers the Context section's first sentence (the decision's rationale),
    then the Decision section, then any body prose, then the title. The Date:
    metadata line adr-tools emits is never used as a description.
    """
    for section in ("Context", "Decision"):
        text = _section_text(adr.body, section)
        sentence = first_sentence(text)
        if sentence:
            return sentence
    # Fall back to whole-body prose but drop a leading "Date:" line first.
    body_no_date = "\n".join(
        ln for ln in adr.body.splitlines() if not ln.strip().lower().startswith("date:")
    )
    return first_sentence(body_no_date) or adr.title
