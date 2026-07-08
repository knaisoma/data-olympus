"""`data-olympus` CLI entry point: lint, index, visualize."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from data_olympus.cli.indexgen import regenerate_indexes
from data_olympus.format import discover_bundle_files, lint_files
from data_olympus.viewer.generator import generate_visualization


def _require_dir(path: str) -> bool:
    if not Path(path).is_dir():
        print(f"error: {path} is not a directory", file=sys.stderr)
        return False
    return True


def _cmd_lint(args: argparse.Namespace) -> int:
    if not _require_dir(args.path):
        return 1
    root = Path(args.path)
    linted = discover_bundle_files(root)
    if not linted:
        print(f"error: no concept files to lint under {root}", file=sys.stderr)
        return 1
    results = lint_files(linted)
    error_files = 0
    total_errors = 0
    for path in sorted(results):
        findings = results[path]
        errors = [f for f in findings if f.severity == "error"]
        warnings = [f for f in findings if f.severity == "warning"]
        if errors:
            error_files += 1
        total_errors += len(errors)
        for f in errors + warnings:
            print(f"{path}: {f.severity}: {f.field}: {f.message}")
    print(f"{total_errors} errors across {error_files} files ({len(linted)} linted)")
    return 1 if total_errors else 0


def _cmd_index(args: argparse.Namespace) -> int:
    if not _require_dir(args.path):
        return 1
    written = regenerate_indexes(Path(args.path))
    print(f"wrote {len(written)} index.md file(s)")
    return 0


def _cmd_visualize(args: argparse.Namespace) -> int:
    if not _require_dir(args.path):
        return 1
    root = Path(args.path)
    out = Path(args.out) if args.out else root / "viz.html"
    stats = generate_visualization(root, out, name=args.name)
    print(f"wrote {out} ({stats['nodes']} nodes, {stats['edges']} edges)")
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    from data_olympus import setup_wizard
    return setup_wizard.run(
        argv_endpoint=args.endpoint,
        check_only=args.check,
        assume_yes=args.yes,
        no_version_check=args.no_version_check,
    )


def _cmd_report(args: argparse.Namespace) -> int:
    from data_olympus.cli.report_cmd import resolve_default_workspace, run_report
    return run_report(
        workspace=args.workspace or resolve_default_workspace(),
        rng=args.range,
        since=args.since,
        window_sec=args.window_sec,
        as_json=args.json,
        fail_on_unverified=args.fail_on_unverified,
        staged=args.staged,
        emit_events=args.emit_events,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="data-olympus")
    sub = parser.add_subparsers(dest="command", required=True)
    p_lint = sub.add_parser("lint", help="validate a bundle's frontmatter")
    p_lint.add_argument("path", nargs="?", default=".", help="bundle root (default: .)")
    p_lint.set_defaults(func=_cmd_lint)
    p_index = sub.add_parser("index", help="regenerate index.md for progressive disclosure")
    p_index.add_argument("path", nargs="?", default=".", help="bundle root (default: .)")
    p_index.set_defaults(func=_cmd_index)
    p_viz = sub.add_parser("visualize", help="render a self-contained HTML graph of the bundle")
    p_viz.add_argument("path", nargs="?", default=".", help="bundle root (default: .)")
    p_viz.add_argument(
        "-o", "--out", default=None, help="output HTML path (default: <root>/viz.html)"
    )
    p_viz.add_argument("--name", default=None, help="display name in the viewer header")
    p_viz.set_defaults(func=_cmd_visualize)
    p_setup = sub.add_parser(
        "setup", help="guided first-run/update: probe endpoint, wire agents, install hooks"
    )
    p_setup.add_argument(
        "--endpoint", default=None,
        help="server endpoint (default: $KB_ENDPOINT or http://localhost:8080)",
    )
    p_setup.add_argument(
        "--check", action="store_true",
        help="read-only doctor summary (also the update-check path); changes nothing",
    )
    p_setup.add_argument(
        "-y", "--yes", action="store_true",
        help="non-interactive: accept defaults (register detected agents, skip hook prompts)",
    )
    p_setup.add_argument(
        "--no-version-check", action="store_true",
        help="skip the PyPI/GitHub latest-version lookup (fully offline)",
    )
    p_setup.set_defaults(func=_cmd_setup)
    p_report = sub.add_parser("report", help="report governed changes lacking a consultation")
    p_report.add_argument("--workspace", default=None,
                          help="workspace label (default: main-worktree basename)")
    p_report.add_argument("--range", default=None, help="git revision range, e.g. HEAD~5..HEAD")
    p_report.add_argument("--since", default="7 days ago", help="git --since when no --range")
    p_report.add_argument("--window-sec", type=int, default=3600,
                          help="consult correlation window in seconds")
    p_report.add_argument("--json", action="store_true", help="emit JSON")
    p_report.add_argument("--fail-on-unverified", action="store_true",
                          help="exit 3 if any unverified governed change is found")
    p_report.add_argument("--staged", action="store_true",
                          help="classify the staged diff instead of git log (for pre-commit)")
    p_report.add_argument("--emit-events", action="store_true",
                          help="record a gate_bypass audit event per unverified governed change")
    p_report.set_defaults(func=_cmd_report)
    from data_olympus.cli.import_cmd import add_import_subparser
    add_import_subparser(sub)
    from data_olympus.cli.validity_report_cmd import add_validity_report_subparser
    add_validity_report_subparser(sub)
    from data_olympus.cli.init_cmd import add_init_subparser
    add_init_subparser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    exit_code: int = args.func(args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
