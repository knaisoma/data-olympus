"""Tests for the `data-olympus import` command and the importer package.

Fixture-driven: exercises flat-file splitting (headings + heading-less bullet
clusters), frontmatter stamping, unique ids, draft-status invariants, ADR
supersedes-chain mapping, OKF normalization, lint-cleanliness, report contents,
the --json shape, and refuse-on-rerun semantics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_olympus.cli.main import main
from data_olympus.format import Document, discover_bundle_files, lint_files
from data_olympus.format.validate import STATUSES, TIERS, TYPES
from data_olympus.importer import ImportError_, run_import
from data_olympus.importer.flat import MIN_BODY_CHARS, split_flat

FIXTURES = Path(__file__).parent / "importer-fixtures"


def _load_drafts(out_dir: Path) -> dict[str, Document]:
    """Return {filename: Document} for every non-reserved .md written."""
    return {p.name: Document.load(p) for p in discover_bundle_files(out_dir)}


def _assert_lint_clean(out_dir: Path) -> None:
    findings = lint_files(discover_bundle_files(out_dir))
    errors = {
        p: [f for f in fs if f.severity == "error"] for p, fs in findings.items()
    }
    errors = {p: fs for p, fs in errors.items() if fs}
    assert not errors, f"expected lint-clean output, got errors: {errors}"


# --------------------------------------------------------------------------- #
# Flat file: CLAUDE.md (headings + preamble + heading-less bullet tail)         #
# --------------------------------------------------------------------------- #


def test_flat_claude_md_splits_on_headings(tmp_path):
    report = run_import(
        source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=tmp_path / "out"
    )
    # Preamble + 5 real headings = 6 candidates; the "TODO" stub is too short.
    assert "TODO" in [s.heading for s in report.skipped]
    titles = {Document.load(tmp_path / "out" / n).frontmatter["title"] for n in report.created}
    assert "Writing style" in titles
    assert "Git workflow" in titles
    assert "Security defaults" in titles
    # Preamble becomes its own draft (it clears the length threshold).
    assert "Project agent rules" in titles


def test_flat_stamps_required_frontmatter_and_draft_status(tmp_path):
    report = run_import(
        source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=tmp_path / "out"
    )
    for doc in _load_drafts(tmp_path / "out").values():
        assert doc.frontmatter["status"] == "draft"
        assert doc.frontmatter["type"] == "standard"
        assert doc.frontmatter["tier"] == "T3"
        assert doc.frontmatter["id"]
        # recommended fields present so lint stays warning-free
        assert doc.frontmatter["title"]
        assert doc.frontmatter["description"]
        assert isinstance(doc.frontmatter["tags"], list) and doc.frontmatter["tags"]
    assert report.lint_clean


def test_flat_ids_are_unique_and_non_colliding(tmp_path):
    run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=tmp_path / "out")
    ids = [d.frontmatter["id"] for d in _load_drafts(tmp_path / "out").values()]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"
    assert all(i.startswith("CLAUDE-") for i in ids)


def test_flat_body_preserved_verbatim(tmp_path):
    run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=tmp_path / "out")
    doc = next(
        d for d in _load_drafts(tmp_path / "out").values()
        if d.frontmatter["title"] == "Writing style"
    )
    assert "Do not use em-dashes." in doc.body
    # The words are never rewritten by the importer.
    assert "em-dashes" in doc.body


def test_flat_output_is_lint_clean(tmp_path):
    run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=tmp_path / "out")
    _assert_lint_clean(tmp_path / "out")


def test_id_prefix_override(tmp_path):
    run_import(
        source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3",
        out=tmp_path / "out", id_prefix="STD-ACME",
    )
    ids = [d.frontmatter["id"] for d in _load_drafts(tmp_path / "out").values()]
    assert all(i.startswith("STD-ACME-") for i in ids)
    # No colons in generated ids (the index rejects ':' in an id).
    assert all(":" not in i for i in ids)


def test_category_stamped_when_flag_given(tmp_path):
    run_import(
        source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3",
        out=tmp_path / "out", category="agent-rules",
    )
    for doc in _load_drafts(tmp_path / "out").values():
        assert doc.frontmatter["category"] == "agent-rules"


# --------------------------------------------------------------------------- #
# Heading-less file: .cursorrules -> bullet clusters                            #
# --------------------------------------------------------------------------- #


def test_cursorrules_without_headings_splits_into_clusters(tmp_path):
    report = run_import(
        source=FIXTURES / ".cursorrules", kind="cursorrules", tier="T2", out=tmp_path / "out"
    )
    # Three prose clusters clear the threshold; the trailing "x" cluster is short.
    assert len(report.created) == 3
    assert "x" in [s.heading for s in report.skipped]
    _assert_lint_clean(tmp_path / "out")


def test_cursorrules_derives_titles_from_first_line(tmp_path):
    run_import(
        source=FIXTURES / ".cursorrules", kind="cursorrules", tier="T2", out=tmp_path / "out"
    )
    titles = {d.frontmatter["title"] for d in _load_drafts(tmp_path / "out").values()}
    assert any("test" in t.lower() for t in titles)


def test_split_flat_headingless_min_body():
    text = "short\n\nthis is a much longer line that clears the minimum body threshold easily.\n"
    secs = split_flat(text)
    long_secs = [s for s in secs if s.body_len >= MIN_BODY_CHARS]
    assert len(long_secs) == 1


# --------------------------------------------------------------------------- #
# Depth-aware heading split (nested headings stay with their parent concept)    #
# --------------------------------------------------------------------------- #


def test_nested_headings_split_at_h2_not_h3(tmp_path):
    # One H1 title over several H2 concepts, each with H3 detail. The importer
    # splits at H2 (not the lone H1, which would collapse into one concept, and
    # not H3, which would over-split) and keeps each H2's H3 subsections in body.
    report = run_import(
        source=FIXTURES / "AGENTS-nested.md", kind="agents-md", tier="T3", out=tmp_path / "out"
    )
    titles = {d.frontmatter["title"] for d in _load_drafts(tmp_path / "out").values()}
    assert "Testing policy" in titles
    assert "Release policy" in titles
    # The H3 subsections are NOT their own concepts.
    assert "Unit tests" not in titles
    assert "Versioning" not in titles
    testing = next(
        d for d in _load_drafts(tmp_path / "out").values()
        if d.frontmatter["title"] == "Testing policy"
    )
    # Nested H3 content is preserved inside the parent concept body verbatim.
    assert "### Unit tests" in testing.body
    assert "must not touch the network" in testing.body
    assert report.lint_clean


def test_boundary_heading_line_kept_in_body(tmp_path):
    run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=tmp_path / "out")
    doc = next(
        d for d in _load_drafts(tmp_path / "out").values()
        if d.frontmatter["title"] == "Writing style"
    )
    # The concept keeps its own heading line so no source text is dropped.
    assert "## Writing style" in doc.body


# --------------------------------------------------------------------------- #
# ADR directory: supersedes chain                                              #
# --------------------------------------------------------------------------- #


def test_adr_maps_number_and_title(tmp_path):
    run_import(source=FIXTURES / "adr-tools", kind="adr", tier="meta", out=tmp_path / "out")
    docs = _load_drafts(tmp_path / "out")
    by_id = {d.frontmatter["id"]: d for d in docs.values()}
    assert set(by_id) == {"ADR-0001", "ADR-0002", "ADR-0004"}
    # Title strips the leading "N. " ordinal.
    assert by_id["ADR-0002"].frontmatter["title"] == "Use PostgreSQL for persistence"
    assert by_id["ADR-0002"].frontmatter["type"] == "decision"


def test_adr_supersedes_chain(tmp_path):
    run_import(source=FIXTURES / "adr-tools", kind="adr", tier="meta", out=tmp_path / "out")
    by_id = {d.frontmatter["id"]: d for d in _load_drafts(tmp_path / "out").values()}
    # Draft-status invariant: imported ADRs land as draft, with the parsed
    # adr-tools status preserved in source_status. supersedes chain is mapped.
    assert by_id["ADR-0002"].frontmatter["status"] == "draft"
    assert by_id["ADR-0002"].frontmatter["source_status"] == "superseded"
    assert by_id["ADR-0002"].frontmatter["superseded_by"] == "ADR-0004"
    assert by_id["ADR-0004"].frontmatter["status"] == "draft"
    assert by_id["ADR-0004"].frontmatter["source_status"] == "accepted"
    assert by_id["ADR-0004"].frontmatter["supersedes"] == "ADR-0002"


def test_adr_always_draft_status():
    # No ADR may ever be written with an in-force status: the invariant is that
    # imports never auto-activate.
    from data_olympus.importer.run import _adr_drafts

    drafts, _ = _adr_drafts(FIXTURES / "adr-tools", tier="meta", category=None, existing=set())
    assert all(d.frontmatter["status"] == "draft" for d in drafts)


def test_adr_source_status_flagged_for_review(tmp_path):
    report = run_import(
        source=FIXTURES / "adr-tools", kind="adr", tier="meta", out=tmp_path / "out"
    )
    joined = "\n".join(report.needs_review)
    assert "source ADR status" in joined
    # Every non-draft source status is surfaced for the reviewer.
    assert "adr-0002" in joined.lower()


def test_adr_id_collision_refused(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    # Pre-seed the output dir with a doc carrying an id an import would produce.
    (out / "ADR-0001.md").write_text(
        "---\nid: ADR-0001\ntype: decision\nstatus: active\ntier: meta\n---\nx\n",
        encoding="utf-8",
    )
    with pytest.raises(ImportError_, match="ADR id collision"):
        run_import(source=FIXTURES / "adr-tools", kind="adr", tier="meta", out=out, force=True)


def test_adr_output_lint_clean(tmp_path):
    run_import(source=FIXTURES / "adr-tools", kind="adr", tier="meta", out=tmp_path / "out")
    _assert_lint_clean(tmp_path / "out")


def test_adr_no_files_raises(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ImportError_, match="no adr-tools files"):
        run_import(source=tmp_path / "empty", kind="adr", tier="meta", out=tmp_path / "out")


# --------------------------------------------------------------------------- #
# OKF normalization                                                            #
# --------------------------------------------------------------------------- #


def test_okf_normalizes_aliases_and_downgrades_status(tmp_path):
    report = run_import(source=FIXTURES / "okf", kind="okf", tier="T2", out=tmp_path / "out")
    doc = next(iter(_load_drafts(tmp_path / "out").values()))
    assert doc.frontmatter["id"] == "OKF-RETRY"
    assert doc.frontmatter["type"] == "standard"  # from alias 'kind'
    assert doc.frontmatter["status"] == "draft"  # 'active' downgraded
    assert doc.frontmatter["tier"] == "T2"  # filled from --tier
    assert doc.frontmatter["title"] == "Retry policy for outbound calls"  # from 'name'
    assert set(doc.frontmatter["tags"]) == {"reliability", "networking"}  # from 'keywords'
    joined = "\n".join(report.inferences)
    assert "downgraded to 'draft'" in joined
    assert "renamed field 'kind'" in joined


def test_okf_missing_id_synthesized_and_flagged(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "no-id.md").write_text(
        "---\ntype: standard\n---\nA concept with no id at all in its frontmatter block.\n",
        encoding="utf-8",
    )
    report = run_import(source=src, kind="okf", tier="T3", out=tmp_path / "out")
    doc = next(iter(_load_drafts(tmp_path / "out").values()))
    assert doc.frontmatter["id"] == "no-id"
    assert any("synthesized" in n for n in report.inferences)
    assert any("synthesized from the filename" in n for n in report.needs_review)


def test_okf_missing_status_reports_inference(tmp_path):
    # A required field synthesized with a default must be reported, not invented
    # silently.
    src = tmp_path / "src"
    src.mkdir()
    (src / "c.md").write_text(
        "---\nid: OKF-C\ntype: standard\ntier: T2\n---\nA concept with no status field set.\n",
        encoding="utf-8",
    )
    report = run_import(source=src, kind="okf", tier="T2", out=tmp_path / "out")
    assert any("missing status" in n for n in report.inferences)
    doc = next(iter(_load_drafts(tmp_path / "out").values()))
    assert doc.frontmatter["status"] == "draft"


def test_okf_id_collision_refused(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text(
        "---\nid: DUP\ntype: standard\ntier: T2\n---\nFirst concept sharing the duplicate id.\n",
        encoding="utf-8",
    )
    (src / "b.md").write_text(
        "---\nid: DUP\ntype: standard\ntier: T2\n---\nSecond concept sharing the duplicate id.\n",
        encoding="utf-8",
    )
    with pytest.raises(ImportError_, match="OKF id collision"):
        run_import(source=src, kind="okf", tier="T2", out=tmp_path / "out")


def test_okf_output_lint_clean(tmp_path):
    run_import(source=FIXTURES / "okf", kind="okf", tier="T2", out=tmp_path / "out")
    _assert_lint_clean(tmp_path / "out")


# --------------------------------------------------------------------------- #
# OKF v0.2 provenance: `generated`, legacy `timestamp`, aliases (issue #173)    #
# --------------------------------------------------------------------------- #


def _import_one_okf(tmp_path: Path, frontmatter: str) -> tuple[object, Document]:
    """Import a single OKF doc whose frontmatter lines are given verbatim."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "c.md").write_text(
        "---\nid: OKF-P\ntype: standard\ntier: T2\n" + frontmatter
        + "---\nA concept used to check provenance handling on import.\n",
        encoding="utf-8",
    )
    report = run_import(source=src, kind="okf", tier="T2", out=tmp_path / "out")
    doc = next(iter(_load_drafts(tmp_path / "out").values()))
    return report, doc


def test_okf_v02_quoted_generated_is_preserved_without_a_timestamp(tmp_path):
    report, doc = _import_one_okf(
        tmp_path,
        'generated: { by: reference_agent/gemini-2.5-pro, at: "2026-07-10T23:16:06+00:00" }\n',
    )
    assert doc.frontmatter["generated"] == {
        "by": "reference_agent/gemini-2.5-pro", "at": "2026-07-10T23:16:06+00:00",
    }
    assert "timestamp" not in doc.frontmatter
    assert not any("defaulted" in n and "generated" in n for n in report.inferences)


def test_okf_v02_unquoted_generated_at_is_preserved_as_parsed(tmp_path):
    import datetime

    _, doc = _import_one_okf(
        tmp_path, "generated: { by: reference_agent/x, at: 2026-06-20T22:53:05Z }\n",
    )
    at = doc.frontmatter["generated"]["at"]
    assert at == datetime.datetime(2026, 6, 20, 22, 53, 5, tzinfo=datetime.UTC)
    assert "timestamp" not in doc.frontmatter


def test_okf_generated_and_legacy_timestamp_are_both_kept(tmp_path):
    import datetime

    _, doc = _import_one_okf(
        tmp_path,
        'timestamp: "2026-05-28"\n'
        'generated: { by: reference_agent/x, at: "2026-07-10T23:16:06Z" }\n',
    )
    assert doc.frontmatter["timestamp"] == "2026-05-28"
    assert doc.frontmatter["generated"]["at"] == "2026-07-10T23:16:06Z"
    assert not isinstance(doc.frontmatter["timestamp"], datetime.date)


def test_okf_without_any_change_time_stamps_the_tool_actor(tmp_path):
    import re

    from data_olympus import __version__

    report, doc = _import_one_okf(tmp_path, "")
    generated = doc.frontmatter["generated"]
    assert generated["by"] == f"data-olympus/{__version__}"
    assert isinstance(generated["at"], str)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", generated["at"])
    assert "timestamp" not in doc.frontmatter
    assert any("missing generated" in n for n in report.inferences)


@pytest.mark.parametrize(
    "raw",
    ["generated: null\n", "generated: yesterday\n", "generated: [a, b]\n"],
)
def test_okf_null_or_malformed_generated_is_preserved_and_flagged(tmp_path, raw):
    import yaml

    report, doc = _import_one_okf(tmp_path, raw)
    assert "generated" in doc.frontmatter
    assert doc.frontmatter["generated"] == yaml.safe_load(raw)["generated"]
    assert "timestamp" not in doc.frontmatter
    assert any("generated" in n for n in report.needs_review)


@pytest.mark.parametrize(
    "aliases",
    ["date: 2026-01-01\nupdated: 2026-02-02\n", "updated: 2026-02-02\ndate: 2026-01-01\n"],
)
def test_okf_alias_collision_is_order_independent_and_reported(tmp_path, aliases):
    import datetime

    report, doc = _import_one_okf(tmp_path, aliases)
    assert doc.frontmatter["timestamp"] == datetime.date(2026, 2, 2)
    dropped = [n for n in report.inferences if "dropped alias field 'date'" in n]
    assert dropped and "2026-01-01" in dropped[0]


def test_okf_canonical_timestamp_beats_its_aliases(tmp_path):
    import datetime

    report, doc = _import_one_okf(
        tmp_path, "updated: 2026-02-02\ntimestamp: 2026-03-03\n",
    )
    assert doc.frontmatter["timestamp"] == datetime.date(2026, 3, 3)
    dropped = [n for n in report.inferences if "dropped alias field 'updated'" in n]
    assert dropped and "2026-02-02" in dropped[0]


def test_okf_alias_timestamp_coexists_with_generated(tmp_path):
    import datetime

    _, doc = _import_one_okf(
        tmp_path,
        'updated: 2026-02-02\ngenerated: { by: reference_agent/x, at: "2026-07-10T23:16:06Z" }\n',
    )
    assert doc.frontmatter["timestamp"] == datetime.date(2026, 2, 2)
    assert doc.frontmatter["generated"]["at"] == "2026-07-10T23:16:06Z"


def _import_raw_okf(tmp_path: Path, frontmatter: str) -> tuple[object, Document]:
    """Import one OKF doc whose ENTIRE frontmatter is given verbatim."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "c.md").write_text(
        "---\n" + frontmatter + "---\nA concept used to check alias precedence.\n",
        encoding="utf-8",
    )
    report = run_import(source=src, kind="okf", tier="T2", out=tmp_path / "out")
    doc = next(iter(_load_drafts(tmp_path / "out").values()))
    return report, doc


@pytest.mark.parametrize(
    ("frontmatter", "field", "winner", "dropped"),
    [
        ("identifier: A\nuid: B\ntype: standard\n", "id", "A", "uid"),
        ("uid: B\nidentifier: A\ntype: standard\n", "id", "A", "uid"),
        ("id: C\nuid: B\nidentifier: A\ntype: standard\n", "id", "C", "identifier"),
        ("id: X-1\nkind: standard\ndoctype: decision\n", "type", "standard", "doctype"),
        ("id: X-1\ndoctype: decision\nkind: standard\n", "type", "standard", "doctype"),
    ],
    ids=["identifier-first", "uid-first", "canonical-id-wins", "kind-first", "doctype-first"],
)
def test_okf_alias_precedence_is_fixed_for_every_family(
    tmp_path, frontmatter, field, winner, dropped,
):
    """One key supplies each field whatever the source order: the canonical key,
    then the alias declared first. Previously the later key in the source won."""
    report, doc = _import_raw_okf(tmp_path, frontmatter)
    assert doc.frontmatter[field] == winner
    assert any(f"dropped alias field {dropped!r}" in n for n in report.inferences)


def test_okf_malformed_generated_mapping_is_preserved_and_flagged(tmp_path):
    report, doc = _import_one_okf(
        tmp_path, 'generated: { by: "", at: "2026-07-10T23:16:06Z" }\n',
    )
    assert doc.frontmatter["generated"] == {"by": "", "at": "2026-07-10T23:16:06Z"}
    assert "timestamp" not in doc.frontmatter
    assert any("generated.by" in n for n in report.needs_review)


def test_okf_v02_families_are_preserved_uninterpreted(tmp_path):
    import yaml

    families = (
        "sources:\n"
        "  - id: ga4-schema\n"
        "    resource: https://developers.google.com/analytics/bigquery/export-schema\n"
        "    last_modified: 2026-05-30T00:00:00Z\n"
        "verified: { by: human:ahormati, at: 2026-06-25T09:00:00Z }\n"
        "stale_after: 2026-09-23T00:00:00Z\n"
        "usage_window: { from: 2026-06-01T00:00:00Z, to: 2026-06-30T00:00:00Z }\n"
    )
    _, doc = _import_one_okf(tmp_path, families)
    expected = yaml.safe_load(families)
    for key in ("sources", "verified", "stale_after", "usage_window"):
        assert doc.frontmatter[key] == expected[key], key


# --------------------------------------------------------------------------- #
# Cross-cutting invariants                                                     #
# --------------------------------------------------------------------------- #


def test_everything_lands_as_draft(tmp_path):
    # Flat, OKF, AND ADR all land as draft: nothing auto-activates.
    run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=tmp_path / "flat")
    run_import(source=FIXTURES / "okf", kind="okf", tier="T2", out=tmp_path / "okf")
    run_import(source=FIXTURES / "adr-tools", kind="adr", tier="meta", out=tmp_path / "adr")
    for bundle in ("flat", "okf", "adr"):
        for doc in _load_drafts(tmp_path / bundle).values():
            assert doc.frontmatter["status"] == "draft", f"{bundle}/{doc.path.name} not draft"


def test_flat_import_stamps_generated_with_the_tool_actor(tmp_path):
    """Issue #173: synthesized drafts record OKF v0.2 `generated`, not a legacy
    `timestamp` of the import date."""
    import re

    from data_olympus import __version__

    run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=tmp_path / "flat")
    docs = list(_load_drafts(tmp_path / "flat").values())
    assert docs
    for doc in docs:
        assert "timestamp" not in doc.frontmatter, doc.path.name
        generated = doc.frontmatter["generated"]
        assert generated["by"] == f"data-olympus/{__version__}"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", generated["at"])


def test_stamped_vocab_is_single_sourced():
    # The importer must only stamp values the schema knows.
    from data_olympus.importer.stamp import DEFAULT_TYPE, DRAFT_STATUS

    assert DEFAULT_TYPE in TYPES
    assert DRAFT_STATUS in STATUSES
    # And a bad tier is rejected against the real TIERS set.
    from data_olympus.importer.stamp import normalize_tier

    assert normalize_tier("2") == "T2"
    assert "T2" in TIERS
    with pytest.raises(ValueError):
        normalize_tier("T9")


def test_unknown_kind_raises(tmp_path):
    with pytest.raises(ImportError_, match="unknown --kind"):
        run_import(source=FIXTURES / "CLAUDE.md", kind="bogus", tier="T3", out=tmp_path / "out")


def test_source_not_found_raises(tmp_path):
    with pytest.raises(ImportError_, match="source not found"):
        run_import(source=tmp_path / "nope.md", kind="claude-md", tier="T3", out=tmp_path / "out")


# --------------------------------------------------------------------------- #
# Re-run semantics: REFUSE by default, --force overwrites                       #
# --------------------------------------------------------------------------- #


def test_rerun_refused_without_force(tmp_path):
    out = tmp_path / "out"
    run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=out)
    with pytest.raises(ImportError_, match="refusing to re-import"):
        run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=out)


def test_rerun_with_force_is_deterministic(tmp_path):
    out = tmp_path / "out"
    first = run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=out)
    first_ids = sorted(d.frontmatter["id"] for d in _load_drafts(out).values())
    second = run_import(
        source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=out, force=True
    )
    second_ids = sorted(d.frontmatter["id"] for d in _load_drafts(out).values())
    # Same files, no duplicates, and ids do NOT drift on a forced re-run: the
    # prior import's files are cleared before allocation, so ids restart at 1.
    assert sorted(first.created) == sorted(second.created)
    assert first_ids == second_ids
    assert len(second_ids) == len(set(second_ids))
    assert second_ids[0].endswith("-001")


def test_force_fails_closed_on_unusable_marker(tmp_path):
    # If the marker cannot tell us which files were imported, --force must refuse
    # rather than silently proceed (which would churn ids or hit a confusing
    # collision error later).
    out = tmp_path / "out"
    run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=out)
    (out / ".data-olympus-import").write_text("not json at all", encoding="utf-8")
    with pytest.raises(ImportError_, match="unreadable"):
        run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=out, force=True)


def test_force_preserves_hand_added_file(tmp_path):
    out = tmp_path / "out"
    run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=out)
    # A file the operator adds next to the drafts must survive a forced re-run.
    (out / "manual.md").write_text(
        "---\nid: MANUAL-1\ntype: standard\nstatus: draft\ntier: T3\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n---\nkeep\n",
        encoding="utf-8",
    )
    run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=out, force=True)
    assert (out / "manual.md").exists()
    assert (out / "manual.md").read_text(encoding="utf-8").endswith("keep\n")


def test_refuses_to_write_into_existing_bundle(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "hand-authored.md").write_text(
        "---\nid: HAND-1\ntype: standard\nstatus: active\ntier: T1\n---\nkeep me\n",
        encoding="utf-8",
    )
    with pytest.raises(ImportError_, match="refusing to write into non-empty bundle"):
        run_import(source=FIXTURES / "CLAUDE.md", kind="claude-md", tier="T3", out=out)
    # The pre-existing file is untouched.
    assert (out / "hand-authored.md").read_text(encoding="utf-8").endswith("keep me\n")


# --------------------------------------------------------------------------- #
# CLI wiring + --json shape                                                    #
# --------------------------------------------------------------------------- #


def test_cli_import_human_output(tmp_path, capsys):
    code = main([
        "import", str(FIXTURES / "CLAUDE.md"),
        "--kind", "claude-md", "--tier", "T3", "--out", str(tmp_path / "out"),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "Imported claude-md" in out
    assert "created 5 draft(s)" in out
    assert "next steps" in out
    assert "kb_cleanup_plan" in out  # dedup seam pointer present


def test_cli_import_json_shape(tmp_path, capsys):
    code = main([
        "import", str(FIXTURES / "adr-tools"),
        "--kind", "adr", "--tier", "meta", "--out", str(tmp_path / "out"), "--json",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "adr"
    assert set(payload) == {
        "kind", "source", "out_dir", "created", "skipped", "inferences",
        "needs_review", "lint", "lint_clean", "next_steps",
    }
    assert payload["lint_clean"] is True
    assert isinstance(payload["created"], list) and payload["created"]
    assert any("kb_cleanup_plan" in s for s in payload["next_steps"])


def test_cli_import_bad_tier_exits_2(tmp_path, capsys):
    code = main([
        "import", str(FIXTURES / "CLAUDE.md"),
        "--kind", "claude-md", "--tier", "T9", "--out", str(tmp_path / "out"),
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert "invalid tier" in err


def test_cli_import_rerun_exits_2(tmp_path, capsys):
    args = [
        "import", str(FIXTURES / "CLAUDE.md"),
        "--kind", "claude-md", "--tier", "T3", "--out", str(tmp_path / "out"),
    ]
    assert main(args) == 0
    capsys.readouterr()
    assert main(args) == 2
    assert "refusing to re-import" in capsys.readouterr().err
