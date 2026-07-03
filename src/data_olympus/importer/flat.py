"""Heuristic splitting of flat rule files into candidate draft concepts.

Handles CLAUDE.md / AGENTS.md / GEMINI.md / .cursorrules: files that are a wall
of agent rules with no governance frontmatter. The split strategy:

1. If the file has ATX headings (``#``..``######``), each heading starts a new
   section; the run of lines up to the next heading of the same-or-shallower
   depth is that section's body. Preamble before the first heading becomes its
   own section only when it carries enough prose.
2. If the file has NO headings, group consecutive non-blank lines into
   "bullet clusters" separated by blank lines; each cluster becomes a section.

A section shorter than ``MIN_BODY_CHARS`` (excluding its heading) is skipped and
reported as too-short. The original text of each kept section is preserved
verbatim as the draft body.
"""

from __future__ import annotations

from dataclasses import dataclass

from .stamp import heading_text

# A section must carry at least this many body characters (heading excluded) to
# become a draft. Below this it is almost always a stray line or a section
# title with no content; we skip and report it rather than emit a stub draft.
MIN_BODY_CHARS = 40


@dataclass
class Section:
    """A candidate concept carved out of a flat file."""

    heading: str
    body: str

    @property
    def body_len(self) -> int:
        """Length of the section's PROSE, excluding a leading heading line.

        The boundary heading is kept inside ``body`` so the concept text is
        complete, but the too-short skip test must measure content, not the
        heading — otherwise a long heading over an empty body would spuriously
        clear the threshold."""
        text = self.body
        lines = text.splitlines()
        if lines and _heading_depth(lines[0]) is not None:
            text = "\n".join(lines[1:])
        return len(text.strip())


def _heading_depth(line: str) -> int | None:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return None
    if heading_text(line) is None:
        return None
    depth = len(stripped) - len(stripped.lstrip("#"))
    return depth if 1 <= depth <= 6 else None


def _top_heading_depth(lines: list[str]) -> int:
    """Return the heading depth to split at (the section boundary level).

    Splitting at a single shallow depth keeps nested subsections attached to
    their parent concept instead of over-splitting. The boundary is the
    SHALLOWEST depth that occurs MORE THAN ONCE, so a document with one H1 title
    over several H2 concepts splits at H2 rather than collapsing into a single
    giant concept. A lone leading H1 is itself a boundary heading (``depth <=
    boundary`` in ``_split_by_headings``): with enough prose it becomes its own
    section (usually the document-title concept), otherwise it is dropped as too
    short. Falls back to the shallowest depth when no depth repeats (e.g. a flat
    list of same-level headings, or a single heading). Assumes at least one
    heading exists (the caller only invokes this when ``has_headings`` is
    true)."""
    depths = [d for d in (_heading_depth(ln) for ln in lines) if d is not None]
    counts: dict[int, int] = {}
    for d in depths:
        counts[d] = counts.get(d, 0) + 1
    repeated = [d for d, n in counts.items() if n > 1]
    if repeated:
        return min(repeated)
    return min(depths)


def _split_by_headings(text: str) -> list[Section]:
    """Split at the shallowest heading depth. Deeper (nested) headings stay in
    their parent section's body, and the boundary heading line itself is kept in
    the body so no source text is dropped. Preamble before the first boundary
    heading becomes its own 'Preamble' section when it carries content."""
    lines = text.splitlines()
    boundary = _top_heading_depth(lines)
    sections: list[Section] = []
    current_heading = "Preamble"
    current_body: list[str] = []

    def flush() -> None:
        body = "\n".join(current_body).strip("\n")
        # Keep every section (even short ones) as a candidate; the caller
        # decides skip-vs-keep so it can report the skip. A section with an
        # empty body but a real heading is still a candidate (reported short).
        sections.append(Section(heading=current_heading, body=body))

    started = False
    for line in lines:
        depth = _heading_depth(line)
        if depth is not None and depth <= boundary:
            # New top-level section. Flush the accumulated preamble/section
            # first, but only emit a preamble that has real content so a leading
            # boundary heading does not produce an empty 'Preamble' candidate.
            if started or "".join(current_body).strip():
                flush()
            current_heading = heading_text(line) or "Untitled"
            # Keep the heading line in the body so the concept text is complete
            # and nothing the author wrote is silently dropped.
            current_body = [line]
            started = True
        else:
            current_body.append(line)
    if started or "".join(current_body).strip():
        flush()
    return sections


def _split_by_clusters(text: str) -> list[Section]:
    """Group blank-line-separated clusters for heading-less files.

    Each cluster of consecutive non-blank lines becomes a section whose heading
    is derived from the cluster's first line (trimmed). This is the fallback for
    ``.cursorrules`` and any flat file without markdown headings.
    """
    sections: list[Section] = []
    cluster: list[str] = []

    def flush() -> None:
        if not cluster:
            return
        block = "\n".join(cluster).strip("\n")
        first = cluster[0].strip()
        # Derive a heading from the first line: strip a leading bullet marker.
        heading = first.lstrip("-*+ ").strip() or "Rule"
        # Trim an over-long heading to a sane length for a title.
        if len(heading) > 80:
            heading = heading[:79].rstrip() + "…"
        sections.append(Section(heading=heading, body=block))

    for line in text.splitlines():
        if line.strip():
            cluster.append(line)
        else:
            flush()
            cluster = []
    flush()
    return sections


def has_headings(text: str) -> bool:
    return any(_heading_depth(line) is not None for line in text.splitlines())


def split_flat(text: str) -> list[Section]:
    """Return the ordered candidate sections for a flat rule file.

    Uses heading-based splitting when the file has any ATX heading, otherwise
    falls back to bullet-cluster grouping. Returns candidates including
    too-short ones; the orchestrator filters and reports skips.
    """
    if has_headings(text):
        return _split_by_headings(text)
    return _split_by_clusters(text)
