"""`data-olympus migrate status`: persist the status autofill to disk (#147).

The `status` subcommand writes the `status` field into legacy (pre-0.4.0)
markdown docs that are missing it, using the default `active` (see
``data_olympus.status_migrate``). This is the EXPLICIT counterpart to the
index build's VIRTUAL autofill (KB_STATUS_AUTOFILL): the build never touches
the markdown source, so an operator runs this once to make the change durable.

Without ``--apply`` the command is a DRY RUN: it reports which files would be
changed and exits 0, changing nothing. ``--apply`` performs the write and (when
an audit log path is configured) records one audited event per changed file.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


def add_migrate_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire the ``migrate`` command (with its ``status`` subcommand) in."""
    p_migrate = sub.add_parser(
        "migrate",
        help="one-off corpus migrations (e.g. persist the status autofill)",
    )
    migrate_sub = p_migrate.add_subparsers(dest="migrate_command", required=True)
    p_status = migrate_sub.add_parser(
        "status",
        help="write a default `status: active` into legacy docs missing it (#147)",
    )
    p_status.add_argument(
        "path", nargs="?", default=".", help="corpus root (default: .)"
    )
    p_status.add_argument(
        "--apply",
        action="store_true",
        help="write the change to disk (default: dry run, reports and changes nothing)",
    )
    p_status.add_argument(
        "--audit-log",
        default=None,
        help="audit-log path for the applied writes (default: $KB_AUDIT_LOG_PATH; "
        "omitted entirely if neither is set)",
    )
    p_status.set_defaults(func=_cmd_migrate_status)


def _cmd_migrate_status(args: argparse.Namespace) -> int:
    import sys

    from data_olympus.format import discover_bundle_files
    from data_olympus.format.document import Document
    from data_olympus.status_migrate import DEFAULT_STATUS, apply_status_autofill

    root = Path(args.path)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    if not args.apply:
        # Dry run: report the files that WOULD be rewritten, change nothing.
        would_change: list[str] = []
        for md in discover_bundle_files(root):
            try:
                doc = Document.load(md)
            except ValueError:
                continue  # malformed frontmatter: a lint concern, not migrated
            status = doc.frontmatter.get("status")
            if not (isinstance(status, str) and status.strip()):
                would_change.append(str(md.relative_to(root)))
        for rel in would_change:
            print(f"would set status: {DEFAULT_STATUS}  {rel}")
        print(
            f"dry run: {len(would_change)} document(s) would be updated "
            f"(re-run with --apply to write)"
        )
        return 0

    audit_log = None
    audit_path = args.audit_log or os.environ.get("KB_AUDIT_LOG_PATH")
    if audit_path:
        from data_olympus.audit_log import AuditLog

        audit_log = AuditLog(
            log_path=audit_path,
            hmac_key=os.environ.get("KB_AUDIT_HMAC_KEY", ""),
        )

    result = apply_status_autofill(root, audit_log=audit_log)
    for rel in result.changed_paths:
        print(f"set status: {DEFAULT_STATUS}  {rel}")
    if result.skipped_malformed:
        for rel in result.skipped_malformed:
            print(f"skipped (malformed frontmatter, fix with `kb lint`): {rel}")
    print(
        f"applied: {result.changed} changed, {result.already_present} already had "
        f"status, {len(result.skipped_malformed)} skipped "
        f"({result.scanned} scanned)"
    )
    return 0
