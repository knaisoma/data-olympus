"""Embeddings hybrid-vs-lexical governance ablation (issue #42).

Builds two indexes over the committed governance corpus:
- a LEXICAL index (embeddings off), and
- a HYBRID index (KB_EMBEDDINGS_MODE=on so build() stores per-doc vectors, with
  its reranker set to the dense blend),
then runs the governance ablation with both so the marginal value of embeddings
is measured on the same held-out queries. Writes
benchmarks/governance_results/embeddings/ablation.{json,md}.

Requires the optional `embeddings` extra and a local model (fetched once, cached).
Run as:
    KB_EMBEDDINGS_MODE=on uv run python -m benchmarks.generate_embeddings_ablation
(the script sets the flag itself for the hybrid build regardless).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_CORPUS_DIR = _REPO_ROOT / "benchmarks" / "governance"
_QUERIES_PATH = _REPO_ROOT / "benchmarks" / "governance_queries.yaml"
_RESULTS_DIR = _REPO_ROOT / "benchmarks" / "governance_results" / "embeddings"


def main() -> None:
    from benchmarks.ablate import run_ablation, write_ablation
    from benchmarks.governance_queries import load_governance_queries
    from benchmarks.tokenizer import SimpleTokenizer
    from data_olympus.cooccurrence import (
        DEFAULT_MAX_TERMS,
        compose_expanders,
        cooccurrence_enabled,
    )
    from data_olympus.embeddings import build_embedder, embeddings_config
    from data_olympus.index import Index
    from data_olympus.query_expansion import default_query_expander

    if not _CORPUS_DIR.exists() or not _QUERIES_PATH.exists():
        raise SystemExit(
            "Missing committed governance artifacts; run "
            "`uv run python -m benchmarks.generate_governance_artifacts` first."
        )

    queries = load_governance_queries(_QUERIES_PATH)
    print(f"Loaded {len(queries)} governance queries.")

    def _expanders(index: Index):
        # Mirror build_app: synonym expansion then co-occurrence expansion.
        cooc = index.cooccurrence_expander() if cooccurrence_enabled() else None
        return compose_expanders(
            default_query_expander(), cooc, max_terms=DEFAULT_MAX_TERMS
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Lexical DB (embeddings off): serves the pure-FTS baseline and the full
        # lexical stack (which only adds query expanders, no vectors).
        os.environ.pop("KB_EMBEDDINGS_MODE", None)
        lex = Index(tmp_path / "lex.db")
        r = lex.build(_CORPUS_DIR, source_commit="emb-ablation-lex")
        print(f"Lexical index: {r.docs_indexed} docs.")

        # Dense DB (embeddings on): stores per-doc vectors for the hybrid configs.
        os.environ["KB_EMBEDDINGS_MODE"] = "on"
        cfg = embeddings_config()
        Index(tmp_path / "hyb.db").build(_CORPUS_DIR, source_commit="emb-ablation-hyb")
        embedder = build_embedder(cfg)
        print(f"Dense index: vectors built, model={cfg.model_name}, weight={cfg.weight}.")

        def _hybrid(path: Path) -> Index:
            i = Index(path)
            i.reranker = i.make_hybrid_reranker(embedder, weight=cfg.weight)
            return i

        # pure FTS + embeddings (ceiling of the dense lift over bare FTS)
        hyb_pure = _hybrid(tmp_path / "hyb.db")
        # full lexical stack (synonym + co-occurrence expansion) = shipping default
        full_lex = Index(tmp_path / "lex.db")
        full_lex.query_expander = _expanders(full_lex)
        # full lexical stack + embeddings = proposed enhanced default
        hyb_stack = _hybrid(tmp_path / "hyb.db")
        hyb_stack.query_expander = _expanders(hyb_stack)

        report = run_ablation(
            corpus_root=_CORPUS_DIR,
            idx=lex,
            queries=queries,
            tokenizer=SimpleTokenizer(),
            k=5,
            extra_configs=[
                ("fts+applies_when+embeddings", {}, hyb_pure),
                ("lexical-stack", {}, full_lex),
                ("lexical-stack+embeddings", {}, hyb_stack),
            ],
        )

    write_ablation(report, _RESULTS_DIR)
    print(f"Wrote {_RESULTS_DIR / 'ablation.md'}")

    # Console summary: the two head-to-heads that matter.
    def _row(cfg_label: str, stratum: str):
        return next((x for x in report.rows
                     if x.config == cfg_label and x.stratum == stratum), None)

    for base_label, emb_label in (
        ("fts+applies_when", "fts+applies_when+embeddings"),
        ("lexical-stack", "lexical-stack+embeddings"),
    ):
        print(f"\n== {emb_label} vs {base_label} ==")
        for stratum in ("trigger_covered", "paraphrase_uncovered", "supersession",
                        "negative", "ALL"):
            base = _row(base_label, stratum)
            hyb_r = _row(emb_label, stratum)
            if base and hyb_r:
                print(
                    f"  {stratum:22} recall {base.recall:.3f} -> {hyb_r.recall:.3f} "
                    f"({hyb_r.recall - base.recall:+.3f}) | mrr {base.mrr:.3f} -> "
                    f"{hyb_r.mrr:.3f} | fp {base.false_positive_rate:.3f} -> "
                    f"{hyb_r.false_positive_rate:.3f}"
                )


if __name__ == "__main__":
    main()
