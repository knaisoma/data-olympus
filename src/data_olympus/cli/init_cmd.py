"""`data-olympus init <dir>`: scaffold a new knowledge bundle (issue #66)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from data_olympus.scaffold import ALL_TIERS, ScaffoldError, scaffold_bundle

if TYPE_CHECKING:
    import argparse


def _cmd_init(args: argparse.Namespace) -> int:
    tiers = args.tiers.split(",") if args.tiers else None
    try:
        result = scaffold_bundle(Path(args.dir), tiers=tiers)
    except ScaffoldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"scaffolded {result.root} "
        f"(tiers: {', '.join(result.tiers)}; "
        f"{len(result.files_written)} file(s) written, "
        f"{len(result.indexes_written)} index(es) generated)"
    )
    return 0


def add_init_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``init`` subcommand on the CLI's subparser action."""
    p = sub.add_parser(
        "init",
        help="scaffold a new knowledge bundle: tier directories, root index.md, "
        "template.md, and one example document per supported concept type",
    )
    p.add_argument("dir", help="bundle directory to create (created if missing)")
    p.add_argument(
        "--tiers", default=None,
        help="comma-separated subset of tier directories to scaffold "
        f"(default: all of {', '.join(ALL_TIERS)})",
    )
    p.set_defaults(func=_cmd_init)
