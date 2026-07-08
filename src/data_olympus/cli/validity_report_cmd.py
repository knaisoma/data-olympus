"""`data-olympus validity-report`: list expired and soon-to-expire concept docs.

Walks a bundle directory directly (the same file-discovery model as `lint`;
no index/server needed) and reports every concept document whose `validity.
valid_until` has already passed, plus (optionally) any expiring within N days.
This is the CLI-side safety net the accepted decision calls for: a typo'd
`valid_until` silently removes a rule from `kb_search` discovery, so an
operator needs an easy way to audit what has expired or is about to.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from data_olympus.format import discover_bundle_files
from data_olympus.format.document import Document
from data_olympus.format.validate import (
    is_expired,
    normalize_validity_date,
    today_iso,
)

if TYPE_CHECKING:
    import argparse


def _doc_validity(doc: Document) -> tuple[str, str] | None:
    """Return ``(valid_until, recheck_by)`` normalized ISO dates, or None when
    the document has no ``validity`` block or it is malformed (fail open,
    matching the lint/index semantics: an unparsable value is treated as
    absent rather than reported as expired/expiring)."""
    validity = doc.frontmatter.get("validity")
    if not isinstance(validity, dict):
        return None
    valid_until, bad_until = normalize_validity_date(validity.get("valid_until"))
    recheck_by, bad_recheck = normalize_validity_date(validity.get("recheck_by"))
    if bad_until or bad_recheck:
        return None
    return valid_until, recheck_by


def run_validity_report(
    *, path: str, today: str | None, expiring_within: int | None,
) -> int:
    root = Path(path)
    if not root.is_dir():
        print(f"error: {path} is not a directory")
        return 1
    resolved_today = today if today is not None else today_iso()

    expired: list[tuple[str, str]] = []
    expiring: list[tuple[str, str]] = []
    for md in discover_bundle_files(root):
        doc = Document.load(md)
        validity = _doc_validity(doc)
        if validity is None:
            continue
        valid_until, _recheck_by = validity
        if not valid_until:
            continue
        doc_id = str(doc.id or md.relative_to(root))
        if is_expired(valid_until, resolved_today):
            expired.append((doc_id, valid_until))
        elif expiring_within is not None:
            cutoff_ok = valid_until <= _add_days(resolved_today, expiring_within)
            if cutoff_ok:
                expiring.append((doc_id, valid_until))

    for doc_id, valid_until in sorted(expired):
        print(f"EXPIRED  {doc_id}: valid_until {valid_until}")
    for doc_id, valid_until in sorted(expiring):
        print(f"EXPIRING {doc_id}: valid_until {valid_until}")
    print(f"{len(expired)} expired, {len(expiring)} expiring")
    return 0


def _add_days(today: str, days: int) -> str:
    import datetime
    return (datetime.date.fromisoformat(today) + datetime.timedelta(days=days)).isoformat()


def add_validity_report_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "validity-report",
        help="list expired and soon-to-expire docs (validity.valid_until)",
    )
    p.add_argument("path", nargs="?", default=".", help="bundle root (default: .)")
    p.add_argument(
        "--today", default=None,
        help="ISO date to evaluate against (default: real today); for CI/testing",
    )
    p.add_argument(
        "--expiring-within", type=int, default=None, metavar="N",
        help="also list docs whose valid_until falls within N days from --today",
    )
    p.set_defaults(func=_cmd_validity_report)


def _cmd_validity_report(args: argparse.Namespace) -> int:
    return run_validity_report(
        path=args.path, today=args.today, expiring_within=args.expiring_within,
    )
