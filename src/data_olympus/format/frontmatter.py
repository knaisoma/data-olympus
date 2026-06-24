"""Parse the YAML frontmatter block out of a markdown document."""

from __future__ import annotations

import yaml

_DELIM = "---"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from a markdown body.

    Returns (frontmatter, body). If the document has no frontmatter block
    (first non-empty line is not a bare '---'), returns ({}, text) unchanged.
    Raises ValueError if a block is opened but never closed, or if the block
    does not parse to a mapping. CRLF input is not normalized; body lines
    retain their trailing carriage return.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != _DELIM:
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == _DELIM:
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
