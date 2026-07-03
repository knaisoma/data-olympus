"""Normalize OKF-ish bundles into the data-olympus governance profile.

OKF docs already carry frontmatter with an id/type. This module:
- reads each ``.md`` file's existing frontmatter,
- maps common alias field names into the canonical schema fields,
- fills REQUIRED fields that are missing with draft-safe defaults, reporting
  every inference so nothing is invented silently,
- validates enum values against the schema vocab, downgrading an out-of-vocab
  status/type/tier to a safe default with a "needs review" note.

Field VALUES are never rewritten beyond the documented normalizations; the body
is preserved verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from data_olympus.format.frontmatter import parse_frontmatter
from data_olympus.format.validate import STATUSES, TIERS, TYPES

from .stamp import DEFAULT_TYPE, DRAFT_STATUS, first_sentence

if TYPE_CHECKING:
    from pathlib import Path

# Alias -> canonical frontmatter key. Only unambiguous renames; we never guess a
# value, only relocate a field the author already wrote under a different name.
_ALIASES: dict[str, str] = {
    "identifier": "id",
    "uid": "id",
    "kind": "type",
    "doctype": "type",
    "state": "status",
    "level": "tier",
    "name": "title",
    "summary": "description",
    "keywords": "tags",
    "date": "timestamp",
    "updated": "timestamp",
}

# Recommended fields we backfill from the body when absent (title/description),
# reporting the inference. tags/timestamp get generic defaults.


@dataclass
class NormalizedOKF:
    path: Path
    frontmatter: dict[str, Any]
    body: str
    inferences: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)


def _apply_aliases(fm: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Rename alias keys to canonical keys. Reports each rename. A canonical key
    already present wins over its alias (the alias is dropped and reported)."""
    out: dict[str, Any] = {}
    inferences: list[str] = []
    for key, value in fm.items():
        canonical = _ALIASES.get(key, key)
        if canonical != key:
            if canonical in fm and canonical != key:
                inferences.append(
                    f"dropped alias field {key!r} (canonical {canonical!r} already set)"
                )
                continue
            inferences.append(f"renamed field {key!r} -> {canonical!r}")
        out[canonical] = value
    return out, inferences


def normalize_okf_doc(path: Path, *, default_tier: str, category: str | None) -> NormalizedOKF:
    """Normalize one OKF doc into the governance profile.

    ``default_tier`` seeds a missing ``tier`` (required). ``category`` is stamped
    when the doc lacks one and the caller passed ``--category``.
    """
    text = path.read_text(encoding="utf-8")
    raw_fm, body = parse_frontmatter(text)
    fm, inferences = _apply_aliases(dict(raw_fm))
    needs_review: list[str] = []

    # id (required): synthesize from the filename stem when absent — flag it,
    # because a stable id normally comes from the author.
    if not fm.get("id"):
        fm["id"] = path.stem
        inferences.append(f"missing id; synthesized {fm['id']!r} from filename")
        needs_review.append(
            f"{path.name}: id was synthesized from the filename; confirm it is stable"
        )

    # type (required): default to standard when absent.
    if not fm.get("type"):
        fm["type"] = DEFAULT_TYPE
        inferences.append(f"missing type; defaulted to {DEFAULT_TYPE!r}")
    elif fm["type"] not in TYPES:
        needs_review.append(
            f"{path.name}: type {fm['type']!r} not in schema; defaulted to {DEFAULT_TYPE!r}"
        )
        fm["type"] = DEFAULT_TYPE

    # status: force to draft on import (never auto-activate). If the source
    # carried an in-force status, report that we downgraded it. Every case is
    # recorded (downgrade, out-of-schema, or synthesized default) so the
    # normalization stays fully auditable and no required field is invented
    # silently.
    src_status = fm.get("status")
    if not src_status:
        inferences.append(f"missing status; defaulted to {DRAFT_STATUS!r}")
    elif src_status != DRAFT_STATUS:
        if src_status in STATUSES:
            inferences.append(f"status {src_status!r} downgraded to {DRAFT_STATUS!r} on import")
        else:
            needs_review.append(
                f"{path.name}: source status {src_status!r} not in schema; set to {DRAFT_STATUS!r}"
            )
    fm["status"] = DRAFT_STATUS

    # tier (required): normalize or default.
    tier = fm.get("tier")
    if not tier:
        fm["tier"] = default_tier
        inferences.append(f"missing tier; defaulted to {default_tier!r} (--tier)")
    elif tier not in TIERS:
        needs_review.append(
            f"{path.name}: tier {tier!r} not in schema; defaulted to {default_tier!r} (--tier)"
        )
        fm["tier"] = default_tier

    # Recommended fields — backfill so the output is lint-clean.
    if not fm.get("title"):
        fm["title"] = str(fm["id"]).replace("-", " ").replace("_", " ").strip() or path.stem
        inferences.append(f"missing title; derived {fm['title']!r}")
    if not fm.get("description"):
        desc = first_sentence(body) or str(fm["title"])
        fm["description"] = desc
        inferences.append("missing description; derived from body")
    tags = fm.get("tags")
    if not tags:
        fm["tags"] = [str(fm["type"])]
        inferences.append("missing tags; defaulted to [type]")
    elif not isinstance(tags, list):
        fm["tags"] = [str(tags)]
        inferences.append("tags coerced to a list")
    else:
        fm["tags"] = [str(t) for t in tags]
    if not fm.get("timestamp"):
        import datetime

        fm["timestamp"] = datetime.date.today().isoformat()
        inferences.append("missing timestamp; defaulted to today")
    if category and not fm.get("category"):
        fm["category"] = category

    # Reorder to the canonical schema order for a clean, diff-stable file.
    ordered = _reorder(fm)
    return NormalizedOKF(
        path=path, frontmatter=ordered, body=body, inferences=inferences, needs_review=needs_review
    )


_ORDER = ("id", "type", "status", "tier", "category", "title", "description", "tags", "timestamp")


def _reorder(fm: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _ORDER:
        if key in fm:
            out[key] = fm[key]
    for key, value in fm.items():
        if key not in out:
            out[key] = value
    return out


def discover_okf_files(source: Path) -> list[Path]:
    """Return the OKF ``.md`` files to normalize.

    A single file is returned as-is. A directory is walked (non-recursively into
    VCS/meta dirs) for ``.md`` files carrying frontmatter; index/template/log
    reserved files are skipped.
    """
    from data_olympus.format.lint import discover_bundle_files

    if source.is_file():
        return [source]
    # Reuse the bundle discovery walk so skip-dir / reserved-file semantics match
    # the linter exactly.
    return discover_bundle_files(source)
