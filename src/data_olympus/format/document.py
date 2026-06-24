"""The Document model: a parsed concept file (frontmatter + body)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .frontmatter import parse_frontmatter


@dataclass(frozen=True)
class Document:
    path: Path
    frontmatter: dict[str, Any]
    body: str

    @classmethod
    def load(cls, path: str | Path) -> Document:
        """Load a document from disk. Callers must not mutate `frontmatter` in place."""
        p = Path(path)
        fm, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        return cls(path=p, frontmatter=fm, body=body)

    @property
    def id(self) -> str | None:
        return self.frontmatter.get("id")

    @property
    def type(self) -> str | None:
        return self.frontmatter.get("type")

    @property
    def status(self) -> str | None:
        return self.frontmatter.get("status")

    @property
    def tier(self) -> str | None:
        return self.frontmatter.get("tier")
