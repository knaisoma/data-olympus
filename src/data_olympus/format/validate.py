"""Validate a Document against the data-olympus governance schema (SPEC.md sections 4 and 9)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .document import Document

TYPES = frozenset({"standard", "decision", "workflow", "project", "memory", "reference"})
STATUSES = frozenset(
    {"draft", "active", "deprecated", "superseded", "proposed", "accepted", "rejected"}
)
TIERS = frozenset({"T1", "T2", "T3", "T4", "meta"})
RESERVED = frozenset({"index.md", "log.md", "template.md"})
REQUIRED = ("id", "type", "status", "tier")
RECOMMENDED = ("title", "description", "tags", "timestamp")

_ENUMS = {"type": TYPES, "status": STATUSES, "tier": TIERS}


@dataclass(frozen=True)
class Finding:
    severity: Literal["error", "warning"]
    field: str
    message: str


def validate_document(doc: Document) -> list[Finding]:
    """Return schema findings for a concept document.

    Reserved files (index.md, log.md) are exempt from the concept schema.
    """
    if doc.path.name in RESERVED:
        return []

    findings: list[Finding] = []
    fm = doc.frontmatter

    for key in REQUIRED:
        if fm.get(key) is None:
            findings.append(Finding("error", key, f"missing required field '{key}'"))

    for key, allowed in _ENUMS.items():
        value = fm.get(key)
        if value is not None and value not in allowed:
            findings.append(
                Finding("error", key, f"invalid {key} '{value}' (allowed: {sorted(allowed)})")
            )

    for key in RECOMMENDED:
        if not fm.get(key):
            findings.append(Finding("warning", key, f"missing recommended field '{key}'"))

    tags_val = fm.get("tags")
    if tags_val is not None and not isinstance(tags_val, list):
        findings.append(
            Finding("warning", "tags", f"'tags' should be a list, got {type(tags_val).__name__}")
        )

    return findings
