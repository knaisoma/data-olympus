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
        return len(self.body.strip())


def _heading_depth(line: str) -> int | None:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return None
    if heading_text(line) is None:
        return None
    depth = len(stripped) - len(stripped.lstrip("#"))
    return depth if 1 <= depth <= 6 else None


def _split_by_headings(text: str) -> list[Section]:
    """Split on ATX headings. Preamble before the first heading is its own
    section with a synthetic 'Preamble' heading."""
    lines = text.splitlines()
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
        if depth is not None:
            # Flush the accumulated preamble/section before starting a new one.
            # Only flush a preamble that has real content; a leading heading
            # with no preamble must not emit an empty 'Preamble' candidate.
            if started or "".join(current_body).strip():
                flush()
            current_heading = heading_text(line) or "Untitled"
            current_body = []
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
