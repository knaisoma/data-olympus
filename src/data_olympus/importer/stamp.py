"""Frontmatter stamping helpers for the importer.

The governance vocabulary (types, statuses, tiers) is single-sourced from
``data_olympus.format.validate`` — the importer never hardcodes a second copy.
Everything stamped here lands as ``status: draft`` by default; the ADR path may
carry a derived status, but the orchestrator flags any non-draft.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

import yaml

from data_olympus.format.validate import STATUSES, TIERS, TYPES

DEFAULT_TYPE = "standard"
DRAFT_STATUS = "draft"

# A small, deterministic keyword -> tag map. Heuristic only: we never invent
# facts, we only surface obvious topical tags from the source words. Empty when
# nothing matches (the caller falls back to a generic tag so the recommended
# ``tags`` field is always present and lint stays clean).
_TAG_KEYWORDS: dict[str, str] = {
    "security": "security",
    "secret": "security",
    "credential": "security",
    "auth": "auth",
    "test": "testing",
    "git": "git",
    "commit": "git",
    "deploy": "deployment",
    "kubernetes": "kubernetes",
    "k8s": "kubernetes",
    "docker": "docker",
    "database": "database",
    "sql": "database",
    "api": "api",
    "style": "style",
    "format": "style",
    "review": "review",
    "workflow": "workflow",
    "python": "python",
    "typescript": "typescript",
    "documentation": "docs",
}

# Strip a leading ATX heading marker and any trailing hashes.
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.*?)\s*#*\s*$")
# Split a body into sentences on the first period/question/exclamation followed
# by whitespace. Deliberately simple: we only need the first sentence.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ID_PREFIX_RE = re.compile(r"[^A-Za-z0-9]+")


def assert_known_vocab() -> None:
    """Fail fast if the defaults we stamp ever fall out of the schema vocab.

    A cheap guard so a future schema rename cannot silently make the importer
    emit lint-dirty drafts.
    """
    if DEFAULT_TYPE not in TYPES:  # pragma: no cover - guard
        raise AssertionError(f"DEFAULT_TYPE {DEFAULT_TYPE!r} not in schema TYPES")
    if DRAFT_STATUS not in STATUSES:  # pragma: no cover - guard
        raise AssertionError(f"DRAFT_STATUS {DRAFT_STATUS!r} not in schema STATUSES")


def normalize_tier(tier: str) -> str:
    """Return ``tier`` if it is a valid schema tier, else raise ValueError.

    Accepts the exact schema spelling (``T1``..``T4``, ``meta``) or a bare digit
    (``1`` -> ``T1``). Anything else is rejected so a typo never produces a
    lint-dirty draft.
    """
    t = tier.strip()
    if t in TIERS:
        return t
    if t.isdigit() and f"T{t}" in TIERS:
        return f"T{t}"
    raise ValueError(f"invalid tier {tier!r} (allowed: {sorted(TIERS)})")


def heading_text(line: str) -> str | None:
    """Return the text of an ATX heading line, or None if not a heading."""
    m = _HEADING_RE.match(line)
    return m.group("text").strip() if m else None


def title_from_heading(heading: str, fallback: str) -> str:
    """Clean a heading into a title. Strips markdown emphasis/backticks."""
    text = heading.strip().strip("*_`").strip()
    return text or fallback


def first_sentence(body: str, *, limit: int = 200) -> str:
    """Return the first sentence of ``body`` as a one-line description.

    Skips blank lines and heading lines; collapses inner whitespace; truncates
    to ``limit`` chars. Returns '' when the body has no prose.
    """
    for raw in body.splitlines():
        line = raw.strip()
        if not line or heading_text(raw) is not None:
            continue
        # Drop leading bullet / list markers so the description reads cleanly.
        line = re.sub(r"^([-*+]|\d+[.)])\s+", "", line)
        if not line:
            continue
        sentence = _SENTENCE_END_RE.split(line, maxsplit=1)[0].strip()
        sentence = re.sub(r"\s+", " ", sentence)
        if len(sentence) > limit:
            sentence = sentence[: limit - 1].rstrip() + "…"
        return sentence
    return ""


def tags_from_text(text: str, *, fallback: str) -> list[str]:
    """Heuristically derive topical tags from source words.

    Deterministic and order-stable: scans the lowercased text once, collecting
    each mapped tag in first-seen order. Falls back to ``[fallback]`` when
    nothing matches so the recommended ``tags`` field is never empty.
    """
    lowered = text.lower()
    seen: list[str] = []
    for keyword, tag in _TAG_KEYWORDS.items():
        if keyword in lowered and tag not in seen:
            seen.append(tag)
    return seen or [fallback]


def slugify(text: str, *, fallback: str = "concept") -> str:
    """Return a filesystem-safe lowercase slug for a filename stem."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug or fallback


def sanitize_id_prefix(prefix: str) -> str:
    """Normalize a user-supplied id prefix to ``[A-Za-z0-9]`` runs joined by '-'.

    An id must not contain a colon (the index treats ``:`` in an id as invalid,
    see markdown_parse.parse_file). Uppercasing is preserved so ``STD-ACME`` and
    ``adr`` both round-trip as the user wrote them, minus separators.
    """
    cleaned = _ID_PREFIX_RE.sub("-", prefix).strip("-")
    return cleaned or "IMP"


class IdAllocator:
    """Hand out unique, zero-padded sequential ids under a prefix.

    Seeded with any ids already present in the output bundle so a re-run with
    ``--force`` (or an import into a hand-authored dir) never reuses an id. The
    padding width keeps filenames sortable for small corpora; ids past the pad
    width simply grow (e.g. ``IMP-100``).
    """

    def __init__(self, prefix: str, existing: set[str] | None = None, *, width: int = 3) -> None:
        self.prefix = sanitize_id_prefix(prefix)
        self._used: set[str] = set(existing or set())
        self._width = width
        self._counter = 0

    def next(self) -> str:
        while True:
            self._counter += 1
            candidate = f"{self.prefix}-{self._counter:0{self._width}d}"
            if candidate not in self._used:
                self._used.add(candidate)
                return candidate

    def reserve(self, doc_id: str) -> None:
        """Record an externally chosen id (e.g. an ADR number) as used."""
        self._used.add(doc_id)


def render_document(frontmatter: dict[str, Any], body: str) -> str:
    """Serialize ``frontmatter`` (YAML) above the verbatim ``body``.

    Uses ``yaml.safe_dump`` so no source value can forge a frontmatter key
    (same hardening as tools_write._render_memory). ``sort_keys=False`` keeps
    the schema field order we build. The body is preserved exactly; we only
    guarantee a single trailing newline.
    """
    dumped = yaml.safe_dump(
        frontmatter, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    return "---\n" + dumped + "---\n\n" + body.rstrip("\n") + "\n"


def build_frontmatter(
    *,
    doc_id: str,
    doc_type: str,
    status: str,
    tier: str,
    title: str,
    description: str,
    tags: list[str],
    category: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a schema-ordered frontmatter mapping.

    Required fields first (id/type/status/tier), then the recommended fields
    (title/description/tags/timestamp) so a stamped draft is lint-clean without
    warnings. ``extra`` carries governance extensions (supersedes/superseded_by).
    """
    fm: dict[str, Any] = {
        "id": doc_id,
        "type": doc_type,
        "status": status,
        "tier": tier,
    }
    if category:
        fm["category"] = category
    fm["title"] = title
    fm["description"] = description
    fm["tags"] = [str(t) for t in tags]
    fm["timestamp"] = datetime.date.today().isoformat()
    if extra:
        for key, value in extra.items():
            fm[key] = value
    return fm
