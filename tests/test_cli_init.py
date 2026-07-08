"""Tests for `data-olympus init <dir>`: bundle scaffold (issue #66).

Covers: conformant scaffold (zero lint errors/warnings, index builds), full
SPEC type coverage including memory/reference, a symmetric supersession pair
that `Index.search(in_force=...)` filters correctly, `applies_when` metadata,
root format-version frontmatter, `template.md`, `--tiers` subsetting, and the
non-empty-directory refusal.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from data_olympus.cli.main import main
from data_olympus.format import Document, discover_bundle_files, lint_files
from data_olympus.format.validate import TYPES
from data_olympus.index import Index
from data_olympus.scaffold import ALL_TIERS

if TYPE_CHECKING:
    from pathlib import Path


def _lint(root: Path) -> dict:
    return lint_files(discover_bundle_files(root))


def _docs(root: Path) -> list[Document]:
    return [Document.load(p) for p in discover_bundle_files(root)]


# --- scenario 1: empty/nonexistent dir, lint clean, index builds -----------


def test_init_into_nonexistent_dir_creates_conformant_bundle(tmp_path):
    dest = tmp_path / "new-bundle"
    assert not dest.exists()

    code = main(["init", str(dest)])
    assert code == 0
    assert dest.is_dir()

    findings = _lint(dest)
    all_findings = [f for flist in findings.values() for f in flist]
    errors = [f for f in all_findings if f.severity == "error"]
    warnings = [f for f in all_findings if f.severity == "warning"]
    assert errors == []
    assert warnings == []

    lint_code = main(["lint", str(dest)])
    assert lint_code == 0

    index_code = main(["index", str(dest)])
    assert index_code == 0


def test_init_under_skip_named_ancestor_still_generates_indexes(tmp_path):
    # Regression (companion review, issue #66 branch): regenerate_indexes
    # matched skip-dir names against the ABSOLUTE path components, so a
    # bundle whose ancestor path passed through e.g. `.venv` silently
    # generated zero per-directory indexes. Skip matching must be relative
    # to the bundle root, exactly like discover_bundle_files in lint.py.
    dest = tmp_path / ".venv" / "kb"

    code = main(["init", str(dest)])
    assert code == 0

    tier_indexes = [p for p in dest.rglob("index.md") if p.parent != dest]
    assert tier_indexes, "no per-directory index.md was generated"
    assert (dest / "decisions" / "index.md").is_file()
    assert (dest / "universal" / "foundation" / "index.md").is_file()


def test_index_command_under_skip_named_ancestor_regenerates(tmp_path):
    # Same root cause, pre-existing `index` subcommand path: `data-olympus
    # index <dir>` must regenerate indexes even when <dir>'s absolute path
    # passes through a skip-named ancestor.
    dest = tmp_path / "node_modules" / "kb"
    assert main(["init", str(dest)]) == 0
    for stale in dest.rglob("index.md"):
        if stale.parent != dest:
            stale.unlink()

    assert main(["index", str(dest)]) == 0
    assert (dest / "decisions" / "index.md").is_file()


def test_init_into_existing_empty_dir_succeeds(tmp_path):
    dest = tmp_path / "already-here"
    dest.mkdir()
    code = main(["init", str(dest)])
    assert code == 0
    assert main(["lint", str(dest)]) == 0


# --- scenario 2: every SPEC type has an example, incl. memory/reference ----


def test_init_covers_every_spec_type_including_memory_and_reference(tmp_path):
    dest = tmp_path / "kb"
    assert main(["init", str(dest)]) == 0

    docs = _docs(dest)
    seen_types = {d.type for d in docs}
    assert seen_types == set(TYPES)
    assert "memory" in seen_types
    assert "reference" in seen_types

    for d in docs:
        assert d.id, d.path
        assert d.type, d.path
        assert d.status, d.path


# --- scenario 3: symmetric supersession pair + in_force filtering ----------


def test_init_supersession_pair_is_symmetric_and_status_consistent(tmp_path):
    dest = tmp_path / "kb"
    assert main(["init", str(dest)]) == 0

    docs = _docs(dest)
    superseded = [d for d in docs if d.status == "superseded"]
    assert len(superseded) == 1
    old = superseded[0]
    successor_id = old.frontmatter.get("superseded_by")
    assert successor_id

    new = next(d for d in docs if d.id == successor_id)
    assert new.status == "active"
    supersedes = new.frontmatter.get("supersedes")
    assert supersedes is not None
    supersedes_list = supersedes if isinstance(supersedes, list) else [supersedes]
    assert old.id in supersedes_list


def test_init_in_force_search_excludes_superseded_doc(tmp_path, tmp_index_path):
    dest = tmp_path / "kb"
    assert main(["init", str(dest)]) == 0

    idx = Index(tmp_index_path)
    idx.build(dest, source_commit="test")

    unfiltered = idx.search("example standard", limit=20)
    unfiltered_ids = {h.id for h in unfiltered}
    assert "STD-INIT-001" in unfiltered_ids
    assert "STD-INIT-002" in unfiltered_ids

    in_force = idx.search("example standard", limit=20, in_force=True)
    in_force_ids = {h.id for h in in_force}
    assert "STD-INIT-002" in in_force_ids
    assert "STD-INIT-001" not in in_force_ids


# --- scenario 4: applies_when, root version frontmatter, template.md ------


def test_init_has_applies_when_root_version_and_template(tmp_path):
    dest = tmp_path / "kb"
    assert main(["init", str(dest)]) == 0

    docs = _docs(dest)
    assert any(d.frontmatter.get("applies_when") for d in docs)

    root_index = Document.load(dest / "index.md")
    assert root_index.frontmatter.get("spec_version")
    assert root_index.frontmatter.get("okf_version")

    assert (dest / "template.md").is_file()


# --- scenario 5: --tiers subset creates only the selected tier dirs -------


def test_init_tiers_subset_creates_only_selected_dirs(tmp_path):
    dest = tmp_path / "kb"
    code = main(["init", str(dest), "--tiers", "decisions,workflows"])
    assert code == 0

    assert (dest / "decisions").is_dir()
    assert (dest / "workflows").is_dir()
    for unselected in set(ALL_TIERS) - {"decisions", "workflows"}:
        assert not (dest / unselected).exists(), unselected

    # still conformant: lint clean over the subset that was generated
    assert main(["lint", str(dest)]) == 0


def test_init_rejects_unknown_tier(tmp_path):
    dest = tmp_path / "kb"
    code = main(["init", str(dest), "--tiers", "bogus"])
    assert code != 0
    assert not dest.exists()


# --- scenario 6: refuses to scaffold into a non-empty directory ----------


def test_init_refuses_nonempty_dir_and_changes_nothing(tmp_path, capsys):
    dest = tmp_path / "kb"
    dest.mkdir()
    (dest / "existing-file.txt").write_text("do not touch\n", encoding="utf-8")

    code = main(["init", str(dest)])
    assert code != 0
    err = capsys.readouterr().err
    assert "not empty" in err.lower() or "non-empty" in err.lower()

    entries = sorted(p.name for p in dest.iterdir())
    assert entries == ["existing-file.txt"]
    assert (dest / "existing-file.txt").read_text(encoding="utf-8") == "do not touch\n"


def test_init_missing_path_argument_returns_nonzero():
    # argparse itself enforces `dir` is required; keep this in the same
    # module as the other init error-handling tests for discoverability.
    with pytest.raises(SystemExit):
        main(["init"])


def test_scaffold_spec_version_tracks_current_format() -> None:
    """The scaffold's SPEC_VERSION must match both SPEC.md's header and
    example-bundle/index.md, so `data-olympus init` never generates a fresh
    bundle declaring a stale format version (v0.4.0 release-gate blocker:
    the scaffold shipped stamping 0.1 after the format moved to 0.2)."""
    import re
    from pathlib import Path

    from data_olympus.scaffold import SPEC_VERSION

    root = Path(__file__).resolve().parents[1]
    spec_header = re.search(
        r"^\*\*Version:\*\*\s+(\S+)$", (root / "SPEC.md").read_text(), re.MULTILINE
    )
    assert spec_header is not None
    assert spec_header.group(1) == SPEC_VERSION
    bundle_index = (root / "example-bundle" / "index.md").read_text()
    m = re.search(r'^spec_version:\s*"([^"]+)"', bundle_index, re.MULTILINE)
    assert m is not None
    assert m.group(1) == SPEC_VERSION
