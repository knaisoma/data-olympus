"""Parse the YAML frontmatter block out of a markdown document."""

from __future__ import annotations

from typing import Any

import yaml

_DELIM = "---"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from a markdown body.

    Returns (frontmatter, body). If the document has no frontmatter block
    (first non-empty line is not a bare '---'), returns ({}, text) unchanged.
    Raises ValueError if a block is opened but never closed, or if the block
    does not parse to a mapping. CRLF input is not normalized; body lines
    retain their trailing carriage return.

    The closing delimiter must sit at column zero: a '---' line that is
    indented is YAML content, not the end of the block. Folded scalars make
    this reachable: ``yaml.safe_dump`` renders a value containing a newline
    followed by '---' (e.g. a free-text ``created_by``) as a quoted scalar
    whose continuation lines are indented, and an indented ``---`` line
    inside such a value would otherwise end the block early (issue #317).
    Trailing whitespace and carriage returns on the closer are tolerated,
    matching the pre-existing handling of CRLF input.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != _DELIM:
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].rstrip() == _DELIM:
            raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:])
            try:
                data = yaml.safe_load(raw) or {}
            except yaml.YAMLError as exc:
                raise ValueError(f"invalid YAML in frontmatter: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError("frontmatter must be a YAML mapping")
            return data, body
    raise ValueError("unterminated frontmatter block")
