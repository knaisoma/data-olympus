"""Token-compact read-tool response measurement harness (issue #65).

Renders representative kb_search / kb_get / kb_list / kb_outline / kb_health
responses against the committed ``example-bundle`` in both the verbose (legacy)
and compact (new default) shapes, and counts tokens with the SAME tokenizer used
by the rest of the benchmark suite (:mod:`benchmarks.tokenizer`). The output is a
per-tool before/after table plus an aggregate.

This is a measuring instrument, not a test: it is deterministic (fixed corpus,
fixed queries), reproducible (``python -m benchmarks.token_compact``), and its
numbers are the evidence in the issue #65 PR body. The regression test in
``tests/test_token_compact_budget.py`` calls :func:`measure` and asserts the
compact default stays under a measured budget with headroom.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from benchmarks.tokenizer import Tokenizer, get_tokenizer
from data_olympus.index import Index
from data_olympus.tools_read import (
    kb_get_fn,
    kb_health_fn,
    kb_list_fn,
    kb_outline_fn,
    kb_search_fn,
    shape_response,
)

# Repo root is two levels up from this file (benchmarks/token_compact.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_BUNDLE = _REPO_ROOT / "example-bundle"

# Fixed representative scenarios. Each is (label, tool, kwargs). Kept small and
# deterministic so the table reproduces byte-for-byte across runs.
_SEARCH_QUERY = "commit message format"
_SEARCH_QUERY_BROAD = "module standard commit secret"
_GET_IDS = (
    "STD-U-004",  # small standard with a supersession chain
    "ADR-002",  # decision doc
    "MEM-2026-06-20-nestjs-module-naming-collision",  # larger memory doc
)


@dataclass(frozen=True)
class Row:
    """One tool scenario measured in both modes."""

    label: str
    verbose_tokens: int
    compact_tokens: int
    verbose_bytes: int
    compact_bytes: int

    @property
    def saved_tokens(self) -> int:
        return self.verbose_tokens - self.compact_tokens

    @property
    def pct_saved(self) -> float:
        if self.verbose_tokens == 0:
            return 0.0
        return 100.0 * self.saved_tokens / self.verbose_tokens


@dataclass(frozen=True)
class Report:
    tokenizer_name: str
    rows: list[Row]

    @property
    def total_verbose(self) -> int:
        return sum(r.verbose_tokens for r in self.rows)

    @property
    def total_compact(self) -> int:
        return sum(r.compact_tokens for r in self.rows)

    @property
    def total_pct_saved(self) -> float:
        if self.total_verbose == 0:
            return 0.0
        return 100.0 * (self.total_verbose - self.total_compact) / self.total_verbose


def _build_index(dest: Path) -> Index:
    """Copy the example-bundle into a scratch git repo and build an Index over it.

    A git repo is required so the index records real ``last_modified`` values;
    the deterministic ``source_commit`` keeps token counts stable across runs.
    """
    kb = dest / "kb"
    shutil.copytree(EXAMPLE_BUNDLE, kb)
    env = {
        "GIT_AUTHOR_NAME": "bench",
        "GIT_AUTHOR_EMAIL": "bench@example.com",
        "GIT_COMMITTER_NAME": "bench",
        "GIT_COMMITTER_EMAIL": "bench@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    subprocess.run(["git", "init", "-q"], cwd=kb, env=env, check=True)
    subprocess.run(["git", "add", "-A"], cwd=kb, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "bench"], cwd=kb, env=env, check=True)
    idx = Index(dest / "index.db")
    idx.build(kb, source_commit="bench0000")
    return idx


def _measure_row(label: str, resp: object, tok: Tokenizer) -> Row:
    verbose = json.dumps(shape_response(resp, verbose=True), ensure_ascii=False)  # type: ignore[arg-type]
    compact = json.dumps(shape_response(resp, verbose=False), ensure_ascii=False)  # type: ignore[arg-type]
    return Row(
        label=label,
        verbose_tokens=tok.count(verbose),
        compact_tokens=tok.count(compact),
        verbose_bytes=len(verbose.encode()),
        compact_bytes=len(compact.encode()),
    )


def measure(tokenizer: str = "simple") -> Report:
    """Run every scenario and return a :class:`Report`."""
    tok = get_tokenizer(tokenizer)
    rows: list[Row] = []
    with tempfile.TemporaryDirectory() as td:
        idx = _build_index(Path(td))

        rows.append(
            _measure_row(
                f"kb_search (limit 20, {_SEARCH_QUERY!r})",
                kb_search_fn(idx=idx, query=_SEARCH_QUERY),
                tok,
            )
        )
        rows.append(
            _measure_row(
                f"kb_search (limit 100, {_SEARCH_QUERY_BROAD!r})",
                kb_search_fn(idx=idx, query=_SEARCH_QUERY_BROAD, limit=100),
                tok,
            )
        )
        for did in _GET_IDS:
            rows.append(_measure_row(f"kb_get ({did})", kb_get_fn(idx=idx, id=did), tok))
        rows.append(_measure_row("kb_list (T1)", kb_list_fn(idx=idx, tier="T1"), tok))
        rows.append(_measure_row("kb_outline", kb_outline_fn(idx=idx), tok))
        rows.append(
            _measure_row(
                "kb_health",
                kb_health_fn(idx=idx, last_git_pull_at=None, staleness_degraded_sec=3600),
                tok,
            )
        )
    return Report(tokenizer_name=tok.name, rows=rows)


def format_markdown_table(report: Report) -> str:
    """Render the report as the Markdown table used in the PR body / docs."""
    lines = [
        f"Tokenizer: `{report.tokenizer_name}`",
        "",
        "| Tool scenario | Verbose (tokens) | Compact (tokens) | Saved | % |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in report.rows:
        lines.append(
            f"| {r.label} | {r.verbose_tokens} | {r.compact_tokens} "
            f"| {r.saved_tokens} | {r.pct_saved:.1f}% |"
        )
    lines.append(
        f"| **Aggregate** | **{report.total_verbose}** | **{report.total_compact}** "
        f"| **{report.total_verbose - report.total_compact}** "
        f"| **{report.total_pct_saved:.1f}%** |"
    )
    return "\n".join(lines)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description="Measure token-compact read-tool responses (issue #65)."
    )
    ap.add_argument(
        "--tokenizer",
        default="simple",
        help="tokenizer name: 'simple' (default, dependency-free) or 'tiktoken'.",
    )
    args = ap.parse_args()
    report = measure(tokenizer=args.tokenizer)
    print(format_markdown_table(report))


if __name__ == "__main__":
    main()
