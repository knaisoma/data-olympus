"""Single source of truth for the benchmark numbers quoted in the docs.

The tables in ``docs/comparison.md`` (and the headline table in ``WHY.md``) are
generated from the committed result JSONs by the functions here, delimited by
HTML-comment markers:

    <!-- BENCH:comparison_per_category START -->
    ...generated table...
    <!-- BENCH:comparison_per_category END -->

``scripts/check_benchmark_docs.py`` regenerates each block and fails if the
committed doc drifts from the results, so a stale hand-edited number cannot land.
Run ``python -m benchmarks.docs_tables --write`` to refresh the docs in place.

Every renderer reads ONLY the committed JSON artifacts (``results/results.json``,
``governance_results/ablation.json``, ``real_corpus/example_bundle_result.json``)
so the docs, the check, and the artifacts can never disagree.
"""
from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_RESULTS = _REPO_ROOT / "benchmarks" / "results" / "results.json"
_GOV = _REPO_ROOT / "benchmarks" / "governance_results" / "ablation.json"
_REAL = _REPO_ROOT / "benchmarks" / "real_corpus" / "example_bundle_result.json"

# Marker names -> renderer. Each renderer returns the table body WITHOUT the
# surrounding markers (the marker lines themselves stay in the doc).
_CATS = ["exact", "semantic", "status", "graph", "ALL"]
_SYNTH_METHOD_ORDER = [
    "data-olympus", "bm25", "bm25-status-aware", "grep-read", "whole-dump",
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt(v: float | None, places: int = 3) -> str:
    return f"{v:.{places}f}" if v is not None else "n/a"


def _synth_rows() -> dict[tuple[str, str], dict]:
    data = _load(_RESULTS)
    return {(r["method"], r["category"]): r for r in data["rows"]}


def render_comparison_per_category() -> str:
    """Full per-category table for every synthetic-corpus method."""
    rows = _synth_rows()
    out = [
        "| Method | Category | Mean Tokens | Norm Tokens | Recall@k | "
        "Contains-Gold | Serves-Stale | NDCG@k | MRR | N |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]

    def _cat_key(c: str) -> int:
        return _CATS.index(c) if c in _CATS else 99

    for method in _SYNTH_METHOD_ORDER:
        for cat in sorted(_CATS, key=_cat_key):
            r = rows.get((method, cat))
            if r is None:
                continue
            out.append(
                f"| {method} | {cat} | {r['mean_tokens']:.0f} | "
                f"{r['norm_tokens']:.0f} | {_fmt(r['recall'])} | "
                f"{_fmt(r['contains_gold'])} | {_fmt(r['serves_stale'])} | "
                f"{_fmt(r['ndcg'])} | {_fmt(r['mrr'])} | {r['n']} |"
            )
    return "\n".join(out)


def render_headline() -> str:
    """WHY.md headline: data-olympus vs the two BM25 baselines, ALL category."""
    rows = _synth_rows()
    do = rows[("data-olympus", "ALL")]
    bm = rows[("bm25", "ALL")]
    sa = rows[("bm25-status-aware", "ALL")]
    out = [
        "| What we measured | data-olympus | BM25 | Status-aware BM25 |",
        "|---|---|---|---|",
        f"| Tokens sent to the model per query (as-shipped) | {do['mean_tokens']:.0f} | "
        f"{bm['mean_tokens']:.0f} | {sa['mean_tokens']:.0f} |",
        f"| Tokens under normalized payload policy | {do['norm_tokens']:.0f} | "
        f"{bm['norm_tokens']:.0f} | {sa['norm_tokens']:.0f} |",
        f"| Overall recall@k | {_fmt(do['recall'])} | {_fmt(bm['recall'])} | "
        f"{_fmt(sa['recall'])} |",
        f"| Serves-stale rate (retired rule reached the agent) | "
        f"{_fmt(do['serves_stale'])} | {_fmt(bm['serves_stale'])} | "
        f"{_fmt(sa['serves_stale'])} |",
    ]
    return "\n".join(out)


def render_governance() -> str:
    """Governance ablation: ALL + key strata per config."""
    data = _load(_GOV)
    by = {(r["config"], r["stratum"]): r for r in data["rows"]}
    configs = list(dict.fromkeys(r["config"] for r in data["rows"]))
    out = [
        "| Config | trigger_covered recall | paraphrase_uncovered (held-out) | "
        "negative FP rate | ALL recall | tokens/query |",
        "|---|---|---|---|---|---|",
    ]
    for cfg in configs:
        tc = by.get((cfg, "trigger_covered"))
        pu = by.get((cfg, "paraphrase_uncovered"))
        neg = by.get((cfg, "negative"))
        allr = by.get((cfg, "ALL"))
        if not (tc and pu and neg and allr):
            continue
        out.append(
            f"| {cfg} | {tc['recall']:.3f} | {pu['recall']:.3f} | "
            f"{neg['false_positive_rate']:.3f} | {allr['recall']:.3f} | "
            f"{allr['mean_tokens']:.0f} |"
        )
    return "\n".join(out)


def render_real_corpus() -> str:
    """Real (non-templated) corpus lexical result."""
    r = _load(_REAL)
    k = r["k"]
    out = [
        "| Metric | Lexical stack (embeddings off) |",
        "|---|---|",
        f"| Corpus | {r['docs_indexed']} committed example-bundle docs |",
        f"| Labeled queries | {r['labeled_queries']} paraphrased intents |",
        f"| recall@{k} | {r[f'recall@{k}']:.3f} |",
        f"| recall@{2 * k} | {r[f'recall@{2 * k}']:.3f} |",
        f"| MRR@{k} | {r[f'mrr@{k}']:.3f} |",
    ]
    return "\n".join(out)


RENDERERS = {
    "comparison_per_category": render_comparison_per_category,
    "headline": render_headline,
    "governance": render_governance,
    "real_corpus": render_real_corpus,
}


# ---------------------------------------------------------------------------
# Marked-block machinery (shared by --write and the drift check)
# ---------------------------------------------------------------------------

# Which markers live in which doc. A doc may host several blocks.
DOC_BLOCKS: dict[str, list[str]] = {
    "docs/comparison.md": ["comparison_per_category", "governance", "real_corpus"],
    "WHY.md": ["headline"],
}


def _markers(name: str) -> tuple[str, str]:
    return (f"<!-- BENCH:{name} START -->", f"<!-- BENCH:{name} END -->")


def replace_block(text: str, name: str, body: str) -> str:
    """Return ``text`` with the marked ``name`` block's interior set to ``body``.

    Raises ``ValueError`` when the markers are missing or malformed so a doc that
    forgot its markers fails loudly rather than silently skipping the check.
    """
    start, end = _markers(name)
    i = text.find(start)
    j = text.find(end)
    if i == -1 or j == -1 or j < i:
        raise ValueError(f"markers for block {name!r} not found or malformed")
    prefix = text[: i + len(start)]
    suffix = text[j:]
    return f"{prefix}\n{body}\n{suffix}"


def extract_block(text: str, name: str) -> str:
    """Return the current interior (stripped) of the marked ``name`` block."""
    start, end = _markers(name)
    i = text.find(start)
    j = text.find(end)
    if i == -1 or j == -1 or j < i:
        raise ValueError(f"markers for block {name!r} not found or malformed")
    return text[i + len(start): j].strip("\n")


def check_or_write(*, write: bool) -> list[str]:
    """Sync each doc's marked blocks against the renderers.

    Returns a list of human-readable drift messages (empty when in sync). When
    ``write`` is True the docs are rewritten in place and the returned list is
    always empty.
    """
    problems: list[str] = []
    for rel, block_names in DOC_BLOCKS.items():
        path = _REPO_ROOT / rel
        text = path.read_text(encoding="utf-8")
        new_text = text
        for name in block_names:
            expected = RENDERERS[name]().strip("\n")
            if write:
                new_text = replace_block(new_text, name, expected)
            else:
                current = extract_block(text, name)
                if current != expected:
                    problems.append(
                        f"{rel}: block {name!r} is stale (doc numbers drifted "
                        f"from benchmark results). Run "
                        f"`python -m benchmarks.docs_tables --write`."
                    )
        if write and new_text != text:
            path.write_text(new_text, encoding="utf-8")
    return problems


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Sync benchmark tables into the docs.")
    ap.add_argument("--write", action="store_true", help="rewrite docs in place")
    args = ap.parse_args()
    problems = check_or_write(write=args.write)
    if args.write:
        print("benchmark doc tables written.")
        return 0
    if problems:
        for p in problems:
            print(p)
        return 1
    print("benchmark doc tables in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
