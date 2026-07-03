#!/usr/bin/env python3
"""CI guard: docs must not drift from the canonical schema in code.

SPEC.md and docs/adoption.md restate, in prose, the controlled-vocabulary
enums and reserved-filename list that ``data_olympus.format.validate`` defines
in code (``TYPES``, ``STATUSES``, ``TIERS``, ``RESERVED``). Those two sources
of truth have no structural link — nothing stops the code from adding a new
``status`` value while the docs still list the old set. This script parses
each prose restatement out of the docs, and fails when its value set no
longer matches the canonical one imported directly from the package.

What this checks:

1. Every `` `type`: ... ``, `` `status`: ... ``, or `` `tier`: ... `` enum
   restatement in SPEC.md and docs/adoption.md against
   ``data_olympus.format.validate.TYPES`` / ``STATUSES`` / ``TIERS``.
2. The reserved-filename list stated in SPEC.md against
   ``data_olympus.format.validate.RESERVED``.

What this deliberately does NOT check: `applies_when` (not an enum) or any
other field's documentation; whether the prose reads well; whether a doc's
example frontmatter blocks are internally consistent. Extend the tuple in
``check_doc_consistency`` if a future field's enum should also be guarded.

Expected doc format (what the parser looks for)
------------------------------------------------

An enum restatement is a Markdown sentence fragment of the form::

    `type`: <anything> `value-one`, `value-two`, ... `value-n`.

i.e. a backtick-quoted field name, a colon, then a comma-separated (optionally
`` `` and ``, `` or `` alternating, and possibly wrapped across lines) list of
backtick-quoted values, terminated by a sentence-ending period (a "." followed
by whitespace or end-of-string, so a period inside a backtick-quoted filename
like `` `index.md` `` does not end the scan early). The parser is tolerant of:

- Line wraps in the middle of the value list (common in prose paragraphs).
- An "or" before the last item (`` `x`, `y`, or `z`. ``).
- Leading qualifier text between the colon and the first backtick-quoted
  value (e.g. "one of", "controlled vocabulary:", "lifecycle state:").
- A period inside a backtick-quoted value itself (e.g. a reserved filename).

It is NOT tolerant of a value list that spans multiple sentences, or values
that aren't backtick-quoted. If SPEC.md's prose changes shape enough to break
the regex, this script should fail loudly (a `ParseError`) rather than
silently report zero values as "in sync" — an empty extracted set is treated
as a parse failure, not a pass.

The reserved-filename restatement is matched the same way, anchored on the
sentence "Reserved filenames." (SPEC.md section 3) and pulls every
backtick-quoted `*.md` token from that sentence.

Invocation
----------

    python scripts/check_doc_consistency.py [--root PATH]

Exit code 0 when every check is in sync, 1 when any drift or parse failure is
found (all failures are collected and printed before exiting, not just the
first).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


class ParseError(ValueError):
    """A doc's prose no longer matches the shape this script expects."""


# --- extraction -------------------------------------------------------------

_BACKTICK_VALUE = re.compile(r"`([^`]+)`")

# A sentence-ending period: followed by whitespace or end-of-string, NOT by
# another non-space character. This distinguishes the "." that ends a
# sentence from the "." inside a backtick-quoted filename like `index.md`
# (where the period is immediately followed by a lowercase letter, not
# whitespace), so the non-greedy scan below doesn't stop mid-filename.
_SENTENCE_END = re.compile(r"\.(?=\s|$)")


def _span_to_sentence_end(text: str, start: int) -> str:
    """Return text[start:end] where end is the next sentence-ending period.

    Raises ParseError if no sentence-ending period is found after ``start``.
    """
    m = _SENTENCE_END.search(text, start)
    if m is None:
        raise ParseError("no sentence-ending '.' found after the field marker")
    return text[start:m.start()]


def _extract_enum_occurrences(text: str, *, field: str) -> list[tuple[int, set[str]]]:
    """Extract every backtick-quoted value list following ``\\`field\\`:`` in text.

    A doc may restate a field's enum in more than one place (SPEC.md restates
    `type`/`status` in both section 4.2 and the section 9 conformance summary);
    checking only the first occurrence would silently miss drift in the
    second. Returns one ``(line_number, values)`` entry per occurrence of the
    `` `field`: `` marker, matching from the marker to the next
    sentence-ending period (across newlines; prose line-wraps a long enum
    list). Raises ParseError if the marker never appears, or if any single
    occurrence has no backtick-quoted values following it (an empty result is
    a parse failure, not an empty-but-valid enum: every field this script
    checks has a non-empty canonical set).
    """
    marker = re.escape(f"`{field}`:")
    occurrences = list(re.finditer(marker, text))
    if not occurrences:
        raise ParseError(f"no '`{field}`:' marker found")
    results: list[tuple[int, set[str]]] = []
    for m in occurrences:
        line_no = text.count("\n", 0, m.start()) + 1
        span = _span_to_sentence_end(text, m.end())
        values = set(_BACKTICK_VALUE.findall(span))
        if not values:
            raise ParseError(
                f"'`{field}`:' at line {line_no} found but no backtick-quoted "
                "values followed it"
            )
        results.append((line_no, values))
    return results


def _extract_reserved(text: str) -> tuple[int, set[str]]:
    """Extract the reserved-filename list from SPEC.md's "Reserved filenames." sentence.

    Returns ``(line_number, values)``. Only the first occurrence is checked:
    SPEC.md states the reserved-filename list normatively once (section 3);
    other mentions elsewhere in the file are parenthetical cross-references,
    not restatements of the canonical list.
    """
    m = re.search(r"Reserved filenames\.", text)
    if m is None:
        raise ParseError("no 'Reserved filenames.' sentence found")
    line_no = text.count("\n", 0, m.start()) + 1
    span = _span_to_sentence_end(text, m.end())
    values = set(_BACKTICK_VALUE.findall(span))
    if not values:
        raise ParseError("'Reserved filenames.' found but no backtick-quoted values followed it")
    return line_no, values


# --- checks ------------------------------------------------------------------


def _diff_message(
    *, source: str, field: str, line_no: int, extracted: set[str], canonical: set[str]
) -> str | None:
    """Return a human-readable drift message, or None if extracted == canonical."""
    if extracted == canonical:
        return None
    missing = canonical - extracted  # in code, not in doc
    extra = extracted - canonical  # in doc, not in code
    parts = [f"{source}:{line_no}: '{field}' enum drifted from data_olympus.format.validate"]
    if missing:
        parts.append(f"  missing from doc (in code): {sorted(missing)}")
    if extra:
        parts.append(f"  stale in doc (not in code): {sorted(extra)}")
    return "\n".join(parts)


def check_doc_consistency(root: Path) -> list[str]:
    """Return a list of drift/parse-error messages; empty means everything is in sync."""
    # Imported here (not at module scope) so a bad --root or missing package
    # produces a clear error from main(), not an import-time traceback.
    from data_olympus.format.validate import RESERVED, STATUSES, TIERS, TYPES

    errors: list[str] = []

    spec_path = root / "SPEC.md"
    adoption_path = root / "docs" / "adoption.md"

    for label, path, field, canonical in (
        ("SPEC.md", spec_path, "type", TYPES),
        ("SPEC.md", spec_path, "status", STATUSES),
        ("SPEC.md", spec_path, "tier", TIERS),
        ("docs/adoption.md", adoption_path, "type", TYPES),
        ("docs/adoption.md", adoption_path, "status", STATUSES),
        ("docs/adoption.md", adoption_path, "tier", TIERS),
    ):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{label}: could not read {path}: {exc}")
            continue
        try:
            occurrences = _extract_enum_occurrences(text, field=field)
        except ParseError as exc:
            errors.append(f"{label}: {exc}")
            continue
        for line_no, extracted in occurrences:
            msg = _diff_message(
                source=label, field=field, line_no=line_no,
                extracted=extracted, canonical=set(canonical),
            )
            if msg:
                errors.append(msg)

    # Reserved filenames: SPEC.md only (docs/adoption.md phrases this as a
    # parenthetical, not the anchored "Reserved filenames." sentence).
    try:
        spec_text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"SPEC.md: could not read {spec_path}: {exc}")
    else:
        try:
            reserved_line_no, extracted_reserved = _extract_reserved(spec_text)
        except ParseError as exc:
            errors.append(f"SPEC.md: {exc}")
        else:
            msg = _diff_message(
                source="SPEC.md",
                field="RESERVED",
                line_no=reserved_line_no,
                extracted=extracted_reserved,
                canonical=set(RESERVED),
            )
            if msg:
                errors.append(msg)

    return errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repo root containing SPEC.md and docs/adoption.md (default: repo root)",
    )
    args = parser.parse_args(argv)

    errors = check_doc_consistency(args.root)
    if errors:
        print("doc consistency guard: FAILED", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print("doc consistency guard: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
