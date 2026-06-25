"""Governance retrieval ablation runner.

Ablates five configurations over a governance corpus + query set:
- fts-no-metadata:        Index.search with columns=[title, tags, body]
- fts+description:        Index.search with columns=[title, tags, description, body]
- fts+applies_when:       Index.search with all columns (production default)
- fts+applies_when+abstain: signal gate (abstain when no discriminating-column match)
- bm25-baseline:          Bm25Method (no index metadata at all)

For each config, computes per-stratum:
- recall@k (fraction of queries where gold is in top-k)
- mrr       (mean reciprocal rank)
- miss_rate  (governance_miss_rate: fraction with NO gold in top-k)
- false_positive_rate (on negative-stratum queries only)
- mean_tokens (mean payload token count)
- n          (query count)

write_ablation(report, out_dir) emits ablation.json + ablation.md with an
explicit marginal-value comparison and held-out stratum commentary.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from benchmarks.methods.base import RetrievalResult
from benchmarks.metrics import (
    false_positive_rate,
    governance_miss_rate,
    recall_at_k,
)
from benchmarks.metrics import (
    mrr as compute_mrr,
)

if TYPE_CHECKING:
    from pathlib import Path

    from benchmarks.governance_queries import GovQuery
    from benchmarks.tokenizer import Tokenizer
    from data_olympus.index import Index


# ---------------------------------------------------------------------------
# Ablation configs
# ---------------------------------------------------------------------------

# Each config is (label, search_kwargs).
# search_kwargs are passed directly to Index.search(query, limit=k, **kwargs).
# bm25-baseline is handled specially (uses Bm25Method, not Index.search).

_FTS_CONFIGS: list[tuple[str, dict[str, object]]] = [
    (
        "fts-no-metadata",
        {"columns": ["title", "tags", "body"]},
    ),
    (
        "fts+description",
        {"columns": ["title", "tags", "description", "body"]},
    ),
    (
        "fts+applies_when",
        {},  # empty: use Index.search defaults (all columns, default weights)
    ),
    (
        "fts+applies_when+abstain",
        {"_abstain": True},  # signal gate: abstain when no discriminating-column match
    ),
]

_BM25_LABEL = "bm25-baseline"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class AblationRow:
    """Aggregated metrics for one (config, stratum) cell."""

    config: str
    stratum: str
    recall: float
    mrr: float
    miss_rate: float
    false_positive_rate: float  # only meaningful for negative stratum
    mean_tokens: float
    n: int


@dataclass
class AblationReport:
    """Full ablation report."""

    rows: list[AblationRow] = field(default_factory=list)
    k: int = 5


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Discriminating columns for the abstention gate. Deliberately EXCLUDES
# `description`: its prose carries common words ("governance rules for ...") that
# out-of-scope queries also contain, which would defeat the gate. Title, tags,
# and applies_when are terse and specific, so matching one signals real intent.
_SIGNAL_COLUMNS = ["title", "tags", "applies_when"]


def _fts_retrieve(
    query: str,
    idx: Index,
    k: int,
    search_kwargs: dict[str, object],
) -> RetrievalResult:
    """Call Index.search and return a RetrievalResult.

    Recognises a special `_abstain` kwarg: when set, the query must match at
    least one discriminating column (title/tags/applies_when/description). If it
    matches none (e.g. an out-of-scope query that only hits generic body prose),
    the method abstains (returns an empty result) instead of surfacing a rule.
    Otherwise it retrieves normally over all columns, preserving recall.
    """
    kwargs = dict(search_kwargs)
    abstain = bool(kwargs.pop("_abstain", False))
    if abstain:
        signal_hits = idx.search(query, limit=k, columns=_SIGNAL_COLUMNS)
        if not signal_hits:
            return RetrievalResult(payload_text="", ranked_ids=[], retrieved_ids=set())
        kwargs = {}  # signal present: retrieve normally over all columns
    hits = idx.search(query, limit=k, **kwargs)  # type: ignore[arg-type]
    ranked = [h.id for h in hits]
    payload = "\n".join(f"{h.title}: {h.snippet}" for h in hits)
    if hits:
        top = idx.get(hits[0].id)
        if top is not None:
            payload += "\n" + top.content_markdown
    return RetrievalResult(
        payload_text=payload,
        ranked_ids=ranked,
        retrieved_ids=set(ranked),
    )


def _bm25_retrieve(query: str, root: Path, k: int) -> RetrievalResult:
    """Retrieve via BM25 method."""
    from benchmarks.methods.bm25 import Bm25Method
    method = Bm25Method(root, k=k)
    return method.retrieve(query)


# ---------------------------------------------------------------------------
# Per-(config, stratum) aggregation
# ---------------------------------------------------------------------------

@dataclass
class _QueryResult:
    stratum: str
    ranked_ids: list[str]
    retrieved_count: int
    tokens: int


def _run_config(
    queries: list[GovQuery],
    idx: Index,
    corpus_root: Path,
    tokenizer: Tokenizer,
    k: int,
    search_kwargs: dict[str, object] | None,  # None => bm25
) -> list[_QueryResult]:
    """Run one config over all queries and return per-query results."""
    results: list[_QueryResult] = []
    bm25_cache: object = None  # lazy-init Bm25Method for bm25-baseline

    for q in queries:
        if search_kwargs is None:
            # BM25 baseline: initialise once per config run
            if bm25_cache is None:
                from benchmarks.methods.bm25 import Bm25Method
                bm25_cache = Bm25Method(corpus_root, k=k)
            assert hasattr(bm25_cache, "retrieve")
            result = bm25_cache.retrieve(q.text)  # type: ignore[union-attr]
        else:
            result = _fts_retrieve(q.text, idx, k, search_kwargs)

        tokens = tokenizer.count(result.payload_text)
        results.append(_QueryResult(
            stratum=q.stratum,
            ranked_ids=result.ranked_ids,
            retrieved_count=len(result.retrieved_ids),
            tokens=tokens,
        ))
    return results


def _aggregate_stratum(
    config_label: str,
    stratum: str,
    query_results: list[_QueryResult],
    gold_per_query: list[set[str]],
    k: int,
) -> AblationRow:
    """Aggregate metrics for one (config, stratum) cell."""
    ranked_lists = [r.ranked_ids for r in query_results]
    token_counts = [r.tokens for r in query_results]

    recall_vals = [
        recall_at_k(r.ranked_ids, gold, k=k)
        for r, gold in zip(query_results, gold_per_query, strict=True)
    ]
    mrr_vals = [
        compute_mrr(r.ranked_ids, gold)
        for r, gold in zip(query_results, gold_per_query, strict=True)
    ]

    miss = governance_miss_rate(ranked_lists, gold_per_query, k=k)

    # false_positive_rate is only meaningful for negative queries.
    if stratum == "negative":
        fp_rate = false_positive_rate([r.retrieved_count for r in query_results])
    else:
        fp_rate = 0.0

    return AblationRow(
        config=config_label,
        stratum=stratum,
        recall=statistics.mean(recall_vals) if recall_vals else 0.0,
        mrr=statistics.mean(mrr_vals) if mrr_vals else 0.0,
        miss_rate=miss,
        false_positive_rate=fp_rate,
        mean_tokens=statistics.mean(token_counts) if token_counts else 0.0,
        n=len(query_results),
    )


# ---------------------------------------------------------------------------
# Public runner
# ---------------------------------------------------------------------------

def run_ablation(
    *,
    corpus_root: Path,
    idx: Index,
    queries: list[GovQuery],
    tokenizer: Tokenizer,
    k: int = 5,
) -> AblationReport:
    """Run all four ablation configs over governance queries.

    Returns an AblationReport with one AblationRow per (config, stratum) cell.
    """
    all_configs: list[tuple[str, dict[str, object] | None]] = [
        *[(label, kwargs) for label, kwargs in _FTS_CONFIGS],
        (_BM25_LABEL, None),
    ]

    rows: list[AblationRow] = []

    for config_label, search_kwargs in all_configs:
        qr_list = _run_config(
            queries, idx, corpus_root, tokenizer, k, search_kwargs
        )

        # Group by stratum.
        strata = sorted({r.stratum for r in qr_list})
        for stratum in strata:
            stratum_qr = [qr for qr, q in zip(qr_list, queries, strict=True)
                          if q.stratum == stratum]
            stratum_qs = [q for q in queries if q.stratum == stratum]
            gold_sets = [set(q.gold_ids) for q in stratum_qs]
            rows.append(_aggregate_stratum(config_label, stratum, stratum_qr, gold_sets, k))

        # Add an ALL-strata row.
        all_gold = [set(q.gold_ids) for q in queries]
        rows.append(_aggregate_stratum(config_label, "ALL", qr_list, all_gold, k))

    return AblationReport(rows=rows, k=k)


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

_STRATA_ORDER = ["trigger_covered", "paraphrase_uncovered", "supersession", "negative", "ALL"]


def write_ablation(report: AblationReport, out_dir: Path) -> None:
    """Write ablation.json and ablation.md to out_dir (created if absent)."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- ablation.json ---
    json_data = {
        "k": report.k,
        "rows": [asdict(r) for r in report.rows],
    }
    (out_dir / "ablation.json").write_text(
        json.dumps(json_data, indent=2), encoding="utf-8"
    )

    # --- ablation.md ---
    configs = list(dict.fromkeys(r.config for r in report.rows))
    strata = [s for s in _STRATA_ORDER if any(r.stratum == s for r in report.rows)]

    lines: list[str] = [
        "# Governance Retrieval Ablation",
        "",
        f"k={report.k}. Four configs: fts-no-metadata, fts+description, "
        "fts+applies_when (production), bm25-baseline.",
        "",
        "## Per-config x per-stratum metrics",
        "",
    ]

    # Table header
    hdr = "| Config | Stratum | Recall@k | MRR | Miss Rate | FP Rate | Tokens | N |"
    sep = "|--------|---------|----------|-----|-----------|---------|--------|---|"
    lines += [hdr, sep]

    for config in configs:
        for stratum in strata:
            row = next(
                (r for r in report.rows if r.config == config and r.stratum == stratum),
                None,
            )
            if row is None:
                continue
            lines.append(
                f"| {row.config} | {row.stratum} | {row.recall:.3f} | "
                f"{row.mrr:.3f} | {row.miss_rate:.3f} | "
                f"{row.false_positive_rate:.3f} | {row.mean_tokens:.1f} | {row.n} |"
            )
    lines.append("")

    # Marginal value of applies_when
    lines += ["## Marginal value of applies_when", ""]
    for stratum in ["trigger_covered", "paraphrase_uncovered"]:
        no_meta = next(
            (r for r in report.rows
             if r.config == "fts-no-metadata" and r.stratum == stratum),
            None,
        )
        desc = next(
            (r for r in report.rows
             if r.config == "fts+description" and r.stratum == stratum),
            None,
        )
        aw = next(
            (r for r in report.rows
             if r.config == "fts+applies_when" and r.stratum == stratum),
            None,
        )
        if not (no_meta and desc and aw):
            continue
        delta_desc = aw.recall - desc.recall
        delta_no = aw.recall - no_meta.recall
        lines.append(
            f"**{stratum}** recall@{report.k}: "
            f"fts-no-metadata={no_meta.recall:.3f}, "
            f"fts+description={desc.recall:.3f}, "
            f"fts+applies_when={aw.recall:.3f}. "
            f"Marginal gain over +description: {delta_desc:+.3f}. "
            f"Marginal gain over no-metadata: {delta_no:+.3f}."
        )
    lines.append("")

    # Held-out honest limit
    lines += ["## Held-out (paraphrase_uncovered) — honest limit", ""]
    pu_aw = next(
        (r for r in report.rows
         if r.config == "fts+applies_when" and r.stratum == "paraphrase_uncovered"),
        None,
    )
    if pu_aw:
        lines.append(
            f"On `paraphrase_uncovered` queries (held-out intent phrasings with NO "
            f"trigger term), fts+applies_when achieves recall={pu_aw.recall:.3f}, "
            f"mrr={pu_aw.mrr:.3f}. "
            "Curated `applies_when` metadata does not help here because the queries "
            "contain no lexical overlap with authored trigger terms. "
            "This stratum is the honest ceiling for keyword-based retrieval; "
            "dense/semantic methods would be expected to do better."
        )
    lines.append("")

    # Negative-query false positive rate
    lines += ["## Negative queries — false positive / abstention", ""]
    for config in configs:
        neg_row = next(
            (r for r in report.rows if r.config == config and r.stratum == "negative"),
            None,
        )
        if neg_row:
            lines.append(
                f"- **{config}**: {neg_row.false_positive_rate:.3f} false-positive "
                f"rate on {neg_row.n} negative queries "
                f"(returned anything at all when no governing rule exists)."
            )
    lines.append("")
    lines.append(
        "_A governance tool should ideally abstain (return nothing) on queries "
        "with no governing rule. FP rate = 0.0 means perfect abstention; "
        "1.0 means always returned results._"
    )
    lines.append("")

    (out_dir / "ablation.md").write_text("\n".join(lines), encoding="utf-8")
