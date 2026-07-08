"""Validate a Document against the data-olympus governance schema (SPEC.md sections 4 and 9)."""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .document import Document

TYPES = frozenset({"standard", "decision", "workflow", "project", "memory", "reference"})
STATUSES = frozenset(
    {"draft", "active", "deprecated", "superseded", "proposed", "accepted", "rejected"}
)
# The in-force status class: guidance that currently applies and should be
# retrievable, as opposed to retired (superseded/deprecated/rejected) or
# not-yet-in-force (draft/proposed) statuses. This is the SINGLE definition of
# the class; Index.search(in_force=True) and the status reranker both consult it.
# ``approved`` is not in the schema STATUSES enum above (the SPEC vocabulary uses
# ``accepted`` for in-force decisions), but the target KB also uses ``approved``
# for accepted decisions, so it is included here so an in-force filter over a
# real KB does not silently drop those docs (issue #68).
IN_FORCE_STATUSES = frozenset({"active", "accepted", "approved"})

# --- validity / freshness (issue #107) --------------------------------------
#
# Optional nested ``validity`` frontmatter object (concept-level only):
#   valid_from / valid_until / last_verified / recheck_by: ISO date or datetime,
#   normalized to a plain ISO date (YYYY-MM-DD) at index/lint time.
#   verification_source: free text, not date-shaped, not handled here.
#
# `is_in_force` is the SINGLE definition of the computed in-force predicate
# (status-class AND validity window); Index.search(in_force=True) and the
# dense-candidate path both consult the SQL-fragment builders below rather than
# re-deriving the window logic. All date comparisons take ``today`` as an
# explicit ISO-date string so every caller (Index, kb lint, the CLI report) is
# deterministically testable and never reads the wall clock itself.
VALIDITY_DATE_FIELDS = ("valid_from", "valid_until", "last_verified", "recheck_by")


def today_iso() -> str:
    """Return the real wall-clock date as an ISO ``YYYY-MM-DD`` string.

    The single place that reads the clock for validity/freshness purposes;
    every other function in this module takes ``today`` as an explicit
    parameter so tests never depend on wall-clock time.
    """
    return datetime.date.today().isoformat()


def normalize_validity_date(value: object) -> tuple[str, bool]:
    """Normalize one ``validity.*`` date field to ``(iso_date, malformed)``.

    Accepts (in order): ``None`` (absent, NOT malformed -> ``("", False)``), a
    ``datetime.datetime`` or ``datetime.date`` (PyYAML auto-parses unquoted
    frontmatter dates into these), or a string holding an ISO date
    (``2026-06-01``) or an ISO datetime, optionally timezone-suffixed
    (``2026-06-01T12:00:00+02:00`` or the ``Z`` shorthand for UTC). Anything
    else — an unparsable string or a non-date/string type (e.g. an int) — is
    malformed: the caller must treat the value as absent (fail open) while
    still surfacing ``malformed=True`` for a lint warning / health counter.
    """
    if value is None:
        return "", False
    if isinstance(value, datetime.datetime):
        return value.date().isoformat(), False
    if isinstance(value, datetime.date):
        return value.isoformat(), False
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return "", False
        try:
            return datetime.date.fromisoformat(s).isoformat(), False
        except ValueError:
            pass
        try:
            s2 = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
            return datetime.datetime.fromisoformat(s2).date().isoformat(), False
        except ValueError:
            return "", True
    return "", True


def is_expired(valid_until: str | None, today: str) -> bool:
    """A doc is expired when ``valid_until`` is present and strictly before
    ``today``. Both must already be normalized ISO ``YYYY-MM-DD`` strings,
    which compare correctly with plain string ordering (and, not
    incidentally, with SQLite's lexicographic ``<``/``>=`` too). The boundary
    day (``valid_until == today``) is NOT expired (inclusive)."""
    return valid_until is not None and valid_until != "" and valid_until < today


def is_upcoming(valid_from: str | None, today: str) -> bool:
    """A doc is upcoming when ``valid_from`` is present and strictly after
    ``today``. The boundary day (``valid_from == today``) is already in force
    (inclusive)."""
    return valid_from is not None and valid_from != "" and valid_from > today


def is_in_force(
    status: str,
    valid_from: str | None,
    valid_until: str | None,
    today: str,
    *,
    is_inbox: bool = False,
) -> bool:
    """Single-sourced in-force predicate: status-class AND validity window.

    A document is in force when its ``status`` is in :data:`IN_FORCE_STATUSES`
    AND it is neither expired (``valid_until`` in the past) nor upcoming
    (``valid_from`` in the future). This is consulted by both the reference
    index's hard ``in_force`` filter and the dense (embedding) candidate
    source, so status-class and validity-window logic can never drift apart
    between the two call sites.

    ``is_inbox`` (issue #109) is the memory-inbox in-force floor: a document
    under the memory-inbox prefix is NEVER in force, no matter what status it
    claims (covers both a legacy inbox file and forged frontmatter on an
    agent-written memory). It is checked FIRST, short-circuiting the
    status/window logic, so the floor composes as an additional condition
    rather than a fork of this predicate. A future composable condition (e.g.
    the lifecycle-edges supersession-graph exclusion) should follow the same
    pattern: a keyword-only flag checked here, not a parallel function.
    """
    if is_inbox:
        return False
    if status not in IN_FORCE_STATUSES:
        return False
    return not is_expired(valid_until, today) and not is_upcoming(valid_from, today)


# --- memory-inbox in-force floor (issue #109) --------------------------------
#
# Single source for "is this path under the memory inbox" so index.py (which
# derives the `is_inbox` column at build time) and tools_write.py (which writes
# new memory proposals under this prefix) can never drift apart. Previously
# tools_write.py read KB_MEMORY_INBOX_PREFIX on its own with no relationship to
# the index's path classification at all.


def memory_inbox_prefix() -> str:
    """Return the directory prefix under which memory proposals are written.

    Defaults to the generic ``memory/inbox/``; a deployment with a different
    layout overrides it via ``KB_MEMORY_INBOX_PREFIX`` (a trailing slash is
    normalized in).
    """
    prefix = os.environ.get("KB_MEMORY_INBOX_PREFIX", "memory/inbox/").strip()
    return prefix if prefix.endswith("/") else prefix + "/"


def is_inbox_path(rel_path: str) -> bool:
    """True when ``rel_path`` (KB-root-relative) falls under the memory inbox.

    Normalizes backslashes so a path built on a platform that used them still
    matches. Consulted at index build time to derive the ``is_inbox`` column;
    NOT re-derived from ``category`` (a deployment-supplied taxonomy override
    could reclassify the same path under a different category, and the floor
    must still hold).
    """
    norm = rel_path.replace("\\", "/")
    return norm.startswith(memory_inbox_prefix())


def compute_freshness(
    *,
    valid_from: str | None,
    valid_until: str | None,
    recheck_by: str | None,
    today: str,
) -> str | None:
    """Return the deviation-only freshness indicator, or ``None`` when fresh.

    Priority: ``expired`` (valid_until in the past) beats ``upcoming``
    (valid_from in the future) beats ``stale`` (recheck_by in the past,
    advisory only — the doc otherwise stays in force and visible). Returns
    ``None`` when none of the three conditions hold, so a caller can drop the
    field entirely (compact output) rather than emit a "fresh" no-op value.
    """
    if is_expired(valid_until, today):
        return "expired"
    if is_upcoming(valid_from, today):
        return "upcoming"
    if recheck_by and recheck_by < today:
        return "stale"
    return None


def not_expired_sql_fragment(param: str = "?") -> str:
    """WHERE fragment excluding expired docs (``valid_until`` in the past).

    This is the DEFAULT ``kb_search`` exclusion (issue #107): a document past
    its ``valid_until`` is dropped from every default search result, not just
    from an ``in_force=True`` query, because an expired doc has no named
    successor to outrank it — if it stayed visible it could be the top hit and
    would govern. Takes exactly one bind parameter: ``today`` (ISO date).
    """
    return f"(docs.valid_until IS NULL OR docs.valid_until >= {param})"


def in_force_sql_fragment(param: str = "?") -> str:
    """WHERE fragment for the validity-WINDOW half of the in-force predicate.

    Callers AND this with their own status-class ``IN (...)`` fragment (see
    :data:`IN_FORCE_STATUSES`). Takes exactly two bind parameters, both
    ``today`` (once for the ``valid_from`` side, once for ``valid_until``).
    """
    return (
        f"(docs.valid_from IS NULL OR docs.valid_from <= {param}) "
        f"AND (docs.valid_until IS NULL OR docs.valid_until >= {param})"
    )


def not_inbox_sql_fragment() -> str:
    """WHERE fragment for the memory-inbox in-force floor (issue #109).

    Takes no bind parameters: ``is_inbox`` is a plain 0/1 column computed once
    at index build time (see :func:`is_inbox_path`), not a per-query value.
    Callers AND this alongside the status-class and validity-window fragments
    whenever ``in_force=True`` is requested, so an inbox doc can never satisfy
    the hard in-force filter no matter what status it claims.
    """
    return "docs.is_inbox = 0"


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


def validate_document(doc: Document, *, today: str | None = None) -> list[Finding]:
    """Return schema findings for a concept document.

    Reserved files (index.md, log.md) are exempt from the concept schema.

    ``today`` (ISO ``YYYY-MM-DD``) drives the three validity/freshness
    warnings below; it defaults to :func:`today_iso` (the real wall clock) but
    is injectable so a caller (tests, the CLI) gets deterministic results.
    These are wall-clock checks, so per the accepted decision they are ALWAYS
    warnings, never errors — an error would make ``kb lint`` (and CI) flake
    with the passage of time.
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

    findings.extend(_validity_findings(fm, today=today if today is not None else today_iso()))

    return findings


def _validity_findings(fm: dict[str, object], *, today: str) -> list[Finding]:
    """The three ``validity`` lint warnings (issue #107). Always warnings.

    - ``validity`` present but not a mapping, or any of its date sub-fields
      fails to parse: "malformed validity value(s)".
    - ``recheck_by`` in the past: advisory staleness warning.
    - ``valid_until`` in the past while ``status`` is in the in-force class:
      the "expired but active" safety net for a typo'd date silently removing
      a rule from discovery.
    Nothing is emitted when ``validity`` is absent.
    """
    validity = fm.get("validity")
    if validity is None:
        return []
    if not isinstance(validity, dict):
        return [Finding("warning", "validity", "'validity' should be a mapping")]

    normalized: dict[str, str] = {}
    malformed_fields: list[str] = []
    for key in VALIDITY_DATE_FIELDS:
        norm, bad = normalize_validity_date(validity.get(key))
        if bad:
            malformed_fields.append(key)
        normalized[key] = norm

    if malformed_fields:
        return [
            Finding(
                "warning", "validity",
                f"malformed validity value(s): {', '.join(malformed_fields)}",
            )
        ]

    findings: list[Finding] = []
    recheck_by = normalized["recheck_by"]
    if recheck_by and recheck_by < today:
        findings.append(
            Finding("warning", "validity", f"recheck_by '{recheck_by}' is in the past")
        )

    valid_until = normalized["valid_until"]
    status = fm.get("status")
    if valid_until and valid_until < today and status in IN_FORCE_STATUSES:
        findings.append(
            Finding(
                "warning", "validity",
                f"valid_until '{valid_until}' is in the past while status "
                f"'{status}' is in the in-force class",
            )
        )

    return findings
