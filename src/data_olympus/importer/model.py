"""Data model for the importer: draft documents and the import report.

These are pure data holders. Splitting/stamping logic lives in the per-kind
modules (``flat``, ``adr``, ``okf``) and the orchestrator (``run``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DraftDoc:
    """A single draft concept ready to be written into the output bundle.

    ``frontmatter`` is an ordered mapping serialized verbatim above the body;
    ``body`` is the ORIGINAL source text, never rewritten. ``filename`` is the
    basename the orchestrator writes it under (relative to the output dir).
    """

    filename: str
    frontmatter: dict[str, Any]
    body: str
    # Free-form notes surfaced in the report's "inferences" list for this doc
    # (e.g. "type inferred as decision", "title from heading").
    inferences: list[str] = field(default_factory=list)
    # True when the doc needs a human to look at it before activation beyond the
    # blanket draft-status review (e.g. a non-draft ADR status, a dangling
    # supersedes ref, an OKF doc missing an id we had to synthesize).
    needs_review: list[str] = field(default_factory=list)


@dataclass
class SkippedSection:
    """A candidate section that was NOT turned into a draft (too short, empty)."""

    heading: str
    reason: str


@dataclass
class LintFinding:
    """A flattened lint finding for the report (path relative to out dir)."""

    path: str
    severity: str
    field: str
    message: str


@dataclass
class ImportReport:
    """The machine- and human-readable result of an import run.

    ``created`` lists files actually written (relative paths). ``skipped`` lists
    candidate sections that were too short to become a draft. ``inferences`` and
    ``needs_review`` aggregate per-doc notes. ``lint`` holds the post-write lint
    result over the output. ``next_steps`` carries the dedup pointer text.
    """

    kind: str
    source: str
    out_dir: str
    created: list[str] = field(default_factory=list)
    skipped: list[SkippedSection] = field(default_factory=list)
    inferences: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)
    lint: list[LintFinding] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    @property
    def lint_clean(self) -> bool:
        """True when no lint ERROR was produced over the output (warnings ok)."""
        return not any(f.severity == "error" for f in self.lint)

    def to_dict(self) -> dict[str, Any]:
        """Machine-readable shape emitted under ``--json``."""
        return {
            "kind": self.kind,
            "source": self.source,
            "out_dir": self.out_dir,
            "created": list(self.created),
            "skipped": [{"heading": s.heading, "reason": s.reason} for s in self.skipped],
            "inferences": list(self.inferences),
            "needs_review": list(self.needs_review),
            "lint": [
                {
                    "path": f.path,
                    "severity": f.severity,
                    "field": f.field,
                    "message": f.message,
                }
                for f in self.lint
            ],
            "lint_clean": self.lint_clean,
            "next_steps": list(self.next_steps),
        }


class ImportError_(Exception):
    """Raised on unrecoverable importer input problems (bad source, refuse-on-rerun).

    Named with a trailing underscore to avoid shadowing the builtin ``ImportError``.
    The CLI catches it and prints the message to stderr with a non-zero exit.
    """
