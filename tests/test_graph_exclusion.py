"""Tests for issue #110 slice 2: supersession edges become executable
retrieval policy.

Covers the graph-exclusion rule (in_force=true additionally excludes any doc
targeted by a supersedes edge whose SOURCE is itself in-force), the
graph_excluded_docs health counter, kb_get/compact-hit surfacing
(superseded_by / contradicts / contradicted_by), kb_consult never returning a
graph-excluded doc, and the contradicts-has-no-ranking-effect invariant.

All date logic is exercised with an explicit, injected ``today`` so nothing
here depends on the wall clock (mirrors tests/test_validity_metadata.py).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.index import Index
from data_olympus.tools_enforce import kb_consult_fn
from data_olympus.tools_read import kb_get_fn, kb_health_fn, kb_search_fn

if TYPE_CHECKING:
    from pathlib import Path

TODAY = "2026-07-08"


def _write(
    kb: Path,
    rel: str,
    *,
    id_: str,
    status: str,
    body: str = "widget body",
    extra: str = "",
    tier: str = "T1",
) -> None:
    p = kb / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nid: {id_}\ntype: standard\nstatus: {status}\ntier: {tier}\n"
        f"category: foundation\ntitle: {id_}\n"
        f"{extra}"
        "---\n"
        f"# {id_}\n\n{body}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Scenario 1: forgotten status flip -- A (active) supersedes B (active); B's
# own frontmatter never sets superseded_by. B must be excluded from
# in_force=true but still present in default search. kb_get(B) surfaces the
# computed retirement via the reverse edge.
# ---------------------------------------------------------------------------


def _forgotten_flip_kb(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    _write(
        kb, "universal/foundation/a.md", id_="DOC-A", status="active",
        body="widget content A", extra="supersedes: DOC-B\n",
    )
    _write(kb, "universal/foundation/b.md", id_="DOC-B", status="active", body="widget content B")
    return kb


def test_forgotten_flip_excluded_from_in_force(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = _forgotten_flip_kb(tmp_path)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert "DOC-B" not in ids
    assert "DOC-A" in ids


def test_forgotten_flip_present_in_default_search(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = _forgotten_flip_kb(tmp_path)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    ids = {h.id for h in idx.search("widget", limit=20, today=TODAY)}
    assert "DOC-B" in ids


def test_kb_get_surfaces_computed_superseded_by_from_reverse_edge(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = _forgotten_flip_kb(tmp_path)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    resp = kb_get_fn(idx=idx, id="DOC-B", today=TODAY)
    assert resp.superseded_by == ["DOC-A"]


def test_kb_search_fn_compact_hit_emits_superseded_by_when_set(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = _forgotten_flip_kb(tmp_path)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    resp = kb_search_fn(idx=idx, query="widget", today=TODAY)
    hit_b = next(h for h in resp.hits if h.id == "DOC-B")
    hit_a = next(h for h in resp.hits if h.id == "DOC-A")
    assert hit_b.superseded_by == ["DOC-A"]
    dump_b = hit_b.compact_dump()
    assert dump_b.get("superseded_by") == ["DOC-A"]
    dump_a = hit_a.compact_dump()
    assert "superseded_by" not in dump_a


# ---------------------------------------------------------------------------
# Scenario 2: a draft source's supersedes edge does NOT exclude the target.
# ---------------------------------------------------------------------------


def test_draft_source_does_not_exclude_target(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = tmp_path / "kb"
    _write(
        kb, "universal/foundation/a.md", id_="DOC-A", status="draft",
        body="widget content A", extra="supersedes: DOC-B\n",
    )
    _write(kb, "universal/foundation/b.md", id_="DOC-B", status="active", body="widget content B")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert "DOC-B" in ids


# ---------------------------------------------------------------------------
# Scenario 3: an expired source's supersedes edge does NOT exclude the target.
# ---------------------------------------------------------------------------


def test_expired_source_does_not_exclude_target(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = tmp_path / "kb"
    _write(
        kb, "universal/foundation/a.md", id_="DOC-A", status="active",
        body="widget content A",
        extra="supersedes: DOC-B\nvalidity:\n  valid_until: 2026-07-01\n",
    )
    _write(kb, "universal/foundation/b.md", id_="DOC-B", status="active", body="widget content B")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert "DOC-B" in ids
    # The expired source itself is excluded from in_force too (unrelated to
    # the graph rule -- its own status-class-AND-window predicate fails).
    assert "DOC-A" not in ids


def test_upcoming_source_does_not_exclude_target(tmp_path: Path, tmp_index_path: Path) -> None:
    """An in-force-status source whose valid_from is in the FUTURE is not yet
    in force, so its supersedes edge does not retire the target (codex review
    suggestion: the valid_from half of the in-force-source guard)."""
    kb = tmp_path / "kb"
    _write(
        kb, "universal/foundation/a.md", id_="DOC-A", status="active",
        body="widget content A",
        extra="supersedes: DOC-B\nvalidity:\n  valid_from: 2026-07-09\n",
    )
    _write(kb, "universal/foundation/b.md", id_="DOC-B", status="active", body="widget content B")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert "DOC-B" in ids
    # The upcoming source itself is excluded from in_force (its own window).
    assert "DOC-A" not in ids
    assert idx.graph_excluded_count(today=TODAY) == 0


def test_boundary_day_source_still_excludes_target(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """Validity boundaries are INCLUSIVE (same as the single-doc predicate):
    a source with valid_until == today or valid_from == today is still in
    force on that day, so its edge still retires the target."""
    kb = tmp_path / "kb"
    _write(
        kb, "universal/foundation/a.md", id_="DOC-A", status="active",
        body="widget content A",
        extra=(
            "supersedes: DOC-B\n"
            f"validity:\n  valid_from: {TODAY}\n  valid_until: {TODAY}\n"
        ),
    )
    _write(kb, "universal/foundation/b.md", id_="DOC-B", status="active", body="widget content B")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert "DOC-B" not in ids
    assert "DOC-A" in ids
    assert idx.graph_excluded_count(today=TODAY) == 1


def test_graph_excluded_count_tracks_date_not_build_time(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """The counter is evaluated LIVE per read, not frozen at build time (codex
    review blocker): a source whose validity window opens/closes between
    rebuilds must move the counter in lockstep with the retrieval filter, with
    NO rebuild in between."""
    kb = tmp_path / "kb"
    _write(
        kb, "universal/foundation/a.md", id_="DOC-A", status="active",
        body="widget content A",
        extra="supersedes: DOC-B\nvalidity:\n  valid_from: 2026-08-01\n",
    )
    _write(kb, "universal/foundation/b.md", id_="DOC-B", status="active", body="widget content B")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    # Before the source's window opens: no exclusion, counter 0.
    before = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert "DOC-B" in before
    assert idx.graph_excluded_count(today=TODAY) == 0
    # After the window opens (same build!): the edge takes effect and the
    # counter follows the retrieval filter to 1.
    later = "2026-08-02"
    after = {h.id for h in idx.search("widget", limit=20, in_force=True, today=later)}
    assert "DOC-B" not in after
    assert idx.graph_excluded_count(today=later) == 1


# ---------------------------------------------------------------------------
# Scenario 4: a dangling edge (target id not in the bundle) excludes nothing
# and does not crash the query.
# ---------------------------------------------------------------------------


def test_dangling_edge_excludes_nothing_and_does_not_crash(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    _write(
        kb, "universal/foundation/a.md", id_="DOC-A", status="active",
        body="widget content A", extra="supersedes: DOC-GHOST\n",
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert ids == {"DOC-A"}


def test_dangling_edge_not_counted_in_graph_excluded_docs(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    _write(
        kb, "universal/foundation/a.md", id_="DOC-A", status="active",
        body="widget content A", extra="supersedes: DOC-GHOST\n",
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    assert idx.graph_excluded_count(today=TODAY) == 0
    assert idx.health()["graph_excluded_docs"] == 0


# ---------------------------------------------------------------------------
# Scenario 5 + 6: an in-force mutual-supersession cycle excludes BOTH docs;
# the health counter reads 2. Not special-cased.
# ---------------------------------------------------------------------------


def _cycle_kb(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    _write(
        kb, "universal/foundation/a.md", id_="DOC-A", status="active",
        body="widget content A", extra="supersedes: DOC-B\n",
    )
    _write(
        kb, "universal/foundation/b.md", id_="DOC-B", status="active",
        body="widget content B", extra="supersedes: DOC-A\n",
    )
    return kb


def test_in_force_cycle_excludes_both_docs(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = _cycle_kb(tmp_path)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert ids == set()


def test_in_force_cycle_health_counter_is_two(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = _cycle_kb(tmp_path)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    assert idx.graph_excluded_count(today=TODAY) == 2
    # The cycle docs carry no validity metadata, so the wall-clock health()
    # path reports the same count on any calendar day.
    assert idx.health()["graph_excluded_docs"] == 2


# ---------------------------------------------------------------------------
# Scenario 6 (continued): the counter is exposed through health.snapshot() and
# the kb_health MCP tool response, alongside malformed_frontmatter/
# malformed_validity.
# ---------------------------------------------------------------------------


def test_kb_health_fn_surfaces_graph_excluded_docs(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = _cycle_kb(tmp_path)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    resp = kb_health_fn(idx=idx, last_git_pull_at=1000.0, staleness_degraded_sec=600)
    assert resp.graph_excluded_docs == 2


# ---------------------------------------------------------------------------
# Scenario 7: kb_consult never returns a graph-excluded doc.
# ---------------------------------------------------------------------------


def test_kb_consult_never_returns_graph_excluded_doc(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier

    kb = _forgotten_flip_kb(tmp_path)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    classifier = IntentClassifier()
    ledger = ConsultationLedger()
    resp = kb_consult_fn(
        idx=idx, classifier=classifier, ledger=ledger,
        workspace="ws", intent="schema convention for widget docs",
        source_session="s1", agent_identity="tester", ttl_sec=3600, now=1000.0,
    )
    ids = {h.id for h in resp.rules}
    assert "DOC-B" not in ids
    # Positive control (codex review suggestion): the consult DID return the
    # in-force rule, so the exclusion above is not just an empty result.
    assert "DOC-A" in ids


# ---------------------------------------------------------------------------
# Scenario 8: contradicts never filters or ranks; kb_get surfaces contradicts
# and computed contradicted_by (reverse edges).
# ---------------------------------------------------------------------------


def test_contradicts_has_no_filtering_or_ranking_effect(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    _write(
        kb, "universal/foundation/a.md", id_="DOC-A", status="active",
        body="widget content A", extra="contradicts: DOC-B\n",
    )
    _write(kb, "universal/foundation/b.md", id_="DOC-B", status="active", body="widget content B")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert ids == {"DOC-A", "DOC-B"}


def test_kb_get_surfaces_contradicts_and_contradicted_by(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    _write(
        kb, "universal/foundation/a.md", id_="DOC-A", status="active",
        body="widget content A", extra="contradicts: DOC-B\n",
    )
    _write(kb, "universal/foundation/b.md", id_="DOC-B", status="active", body="widget content B")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    resp_a = kb_get_fn(idx=idx, id="DOC-A", today=TODAY)
    resp_b = kb_get_fn(idx=idx, id="DOC-B", today=TODAY)
    assert resp_a.contradicts == ["DOC-B"]
    assert resp_a.contradicted_by == []
    assert resp_b.contradicts == []
    assert resp_b.contradicted_by == ["DOC-A"]


def test_compact_hit_omits_contradicts(tmp_path: Path, tmp_index_path: Path) -> None:
    kb = tmp_path / "kb"
    _write(
        kb, "universal/foundation/a.md", id_="DOC-A", status="active",
        body="widget content A", extra="contradicts: DOC-B\n",
    )
    _write(kb, "universal/foundation/b.md", id_="DOC-B", status="active", body="widget content B")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    resp = kb_search_fn(idx=idx, query="widget", today=TODAY)
    for hit in resp.hits:
        dump = hit.compact_dump()
        assert "contradicts" not in dump
        assert "contradicted_by" not in dump


def test_compact_hit_omits_superseded_by_when_absent(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    _write(kb, "universal/foundation/a.md", id_="DOC-A", status="active", body="widget content A")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    resp = kb_search_fn(idx=idx, query="widget", today=TODAY)
    hit = next(h for h in resp.hits if h.id == "DOC-A")
    assert "superseded_by" not in hit.compact_dump()


def test_kb_get_omits_superseded_by_contradicts_contradicted_by_when_absent(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    _write(kb, "universal/foundation/a.md", id_="DOC-A", status="active", body="widget content A")
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="x")
    resp = kb_get_fn(idx=idx, id="DOC-A", today=TODAY)
    dump = resp.compact_dump()
    assert "superseded_by" not in dump
    assert "contradicts" not in dump
    assert "contradicted_by" not in dump


# ---------------------------------------------------------------------------
# Scenario 9: the example-bundle live fixture. STD-U-003 (superseded, own
# status flipped) must be excluded from in-force via BOTH its own status AND
# the graph rule (STD-U-004 is active and supersedes it).
# ---------------------------------------------------------------------------


def test_example_bundle_std_u_003_excluded_via_status_and_graph(
    tmp_index_path: Path,
) -> None:
    from pathlib import Path as _Path

    bundle = _Path(__file__).resolve().parent.parent / "example-bundle"
    idx = Index(tmp_index_path)
    idx.build(bundle, source_commit="x")
    doc = idx.get("STD-U-003")
    assert doc is not None
    assert doc.status == "superseded"
    assert tuple(doc.superseded_by) == ("STD-U-004",)
    ids = {h.id for h in idx.search("commit message", limit=20, in_force=True)}
    assert "STD-U-003" not in ids
    assert "STD-U-004" in ids
