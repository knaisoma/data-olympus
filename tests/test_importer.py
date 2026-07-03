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
