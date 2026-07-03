"""`data-olympus import`: migrate an existing rule corpus into a draft bundle.

Wires the CLI flags to ``data_olympus.importer.run_import`` and renders the
import report either human-readably or as JSON (``--json``). Exit codes:
- 0: import succeeded and the output is lint-clean.
- 1: import succeeded but the output has lint errors (should not happen on the
     happy path; surfaces a stamping regression instead of silently passing).
- 2: bad input or a refused re-run (ImportError_).
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from data_olympus.importer import ImportError_, run_import

if TYPE_CHECKING:
    import argparse

    from data_olympus.importer.model import ImportReport


def _render_human(report: ImportReport) -> str:
    lines: list[str] = []
    lines.append(f"Imported {report.kind} from {report.source}")
    lines.append(f"  output: {report.out_dir}")
    lines.append(f"  created {len(report.created)} draft(s):")
    for name in report.created:
        lines.append(f"    + {name}")
    if report.skipped:
        lines.append(f"  skipped {len(report.skipped)} section(s) (too short):")
        for s in report.skipped:
            lines.append(f"    - {s.heading}: {s.reason}")
    if report.inferences:
        lines.append(f"  inferences ({len(report.inferences)}):")
        for note in report.inferences:
            lines.append(f"    * {note}")
    if report.needs_review:
        lines.append(f"  needs human review ({len(report.needs_review)}):")
        for note in report.needs_review:
            lines.append(f"    ! {note}")
    if report.lint:
        lines.append(f"  lint findings ({len(report.lint)}):")
        for f in report.lint:
            lines.append(f"    [{f.severity}] {f.path}: {f.field}: {f.message}")
        lines.append(f"  lint clean: {report.lint_clean}")
    else:
        lines.append("  lint: clean (no findings)")
    lines.append("  next steps:")
    for step in report.next_steps:
        lines.append(f"    - {step}")
    return "\n".join(lines)


def _cmd_import(args: argparse.Namespace) -> int:
    try:
        report = run_import(
            source=args.source,
            kind=args.kind,
            tier=args.tier,
            out=args.out,
            category=args.category,
            id_prefix=args.id_prefix,
            force=args.force,
        )
    except ImportError_ as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict()))
    else:
        print(_render_human(report))

    return 0 if report.lint_clean else 1


def add_import_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``import`` subcommand on the CLI's subparser action."""
    p = sub.add_parser(
        "import",
        help="migrate an existing rule corpus (CLAUDE.md/AGENTS.md/.cursorrules/ADR/OKF) "
        "into a governed draft bundle",
    )
    p.add_argument("source", help="path to the source file (flat rule file) or directory (ADR/OKF)")
    p.add_argument(
        "--kind",
        required=True,
        choices=["claude-md", "agents-md", "gemini-md", "cursorrules", "adr", "okf"],
        help="source corpus kind",
    )
    p.add_argument(
        "--tier",
        required=True,
        help="governance tier for the imported drafts (T1|T2|T3|T4|meta, or a bare digit)",
    )
    p.add_argument(
        "-o", "--out", default=None,
        help="output bundle directory (default: <source>/imported-<kind>). Refuses to write "
        "into a dir that already holds governed files unless --force is given.",
    )
    p.add_argument("--category", default=None, help="optional category stamped on every draft")
    p.add_argument(
        "--id-prefix", default=None,
        help="id prefix for generated ids (default: per-kind, e.g. CLAUDE/AGENTS/OKF; "
        "ADR imports always use ADR-NNNN)",
    )
    p.add_argument(
        "--force", action="store_true",
        help="overwrite an output dir that was already used as an import target",
    )
    p.add_argument("--json", action="store_true", help="emit the import report as JSON")
    p.set_defaults(func=_cmd_import)
