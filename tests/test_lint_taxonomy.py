"""`kb lint` warns when frontmatter tier/category disagrees with the path
taxonomy (issue #304). Always a warning: indexing keeps honouring the
frontmatter override, and the exit code is unchanged."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from data_olympus.cli.main import main
from data_olympus.format import discover_bundle_files, lint_bundle, lint_files

if TYPE_CHECKING:
    from pathlib import Path


def _write(root: Path, rel: str, *, tier: str | None, category: str | None = None,
           doc_id: str | None = None) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"id: {doc_id or p.stem}", "type: standard", "status: active"]
    if tier is not None:
        lines.append(f"tier: {tier}")
    if category is not None:
        lines.append(f"category: {category}")
    lines += ["title: t", "description: d", "tags: [x]", "timestamp: 2026-01-01", "---", "ok", ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _taxonomy(findings: dict[Path, list], path: Path) -> list:
    return [f for f in findings.get(path, []) if f.field in ("tier", "category")]


@pytest.fixture(autouse=True)
def _no_custom_taxonomy(monkeypatch):
    monkeypatch.delenv("KB_TAXONOMY_PATH", raising=False)


def test_tier_disagreeing_with_path_is_a_warning_naming_the_path_tier(tmp_path):
    p = _write(tmp_path, "universal/foundation/STD-U-001.md", tier="T4")
    found = _taxonomy(lint_bundle(tmp_path), p)
    assert len(found) == 1
    f = found[0]
    assert f.severity == "warning"
    assert f.field == "tier"
    assert "'T4'" in f.message
    assert "'T1'" in f.message
    assert "KB_WRITE_BLOCK_TIERS" in f.message


def test_matching_tier_and_category_produce_no_warning(tmp_path):
    p = _write(tmp_path, "universal/security/STD-U-601.md", tier="T1", category="security")
    assert _taxonomy(lint_bundle(tmp_path), p) == []


def test_category_disagreeing_with_path_is_a_warning(tmp_path):
    p = _write(tmp_path, "universal/security/STD-U-601.md", tier="T1", category="foundation")
    found = _taxonomy(lint_bundle(tmp_path), p)
    assert [f.field for f in found] == ["category"]
    assert found[0].severity == "warning"
    assert "'security'" in found[0].message


def test_absent_category_is_not_compared(tmp_path):
    p = _write(tmp_path, "universal/security/STD-U-601.md", tier="T1")
    assert _taxonomy(lint_bundle(tmp_path), p) == []


def test_component_path_implies_t4_and_component_category(tmp_path):
    p = _write(tmp_path, "projects/acme/components/api/README.md", tier="T3",
               category="project:acme")
    found = _taxonomy(lint_bundle(tmp_path), p)
    assert [f.field for f in found] == ["tier", "category"]
    assert "'T4'" in found[0].message
    assert "'component:acme/api'" in found[1].message


def test_dynamic_stack_and_project_categories_match(tmp_path):
    s = _write(tmp_path, "tech-stacks/python/STD-PY-001.md", tier="T2", category="stack:python")
    p = _write(tmp_path, "projects/acme/README.md", tier="T3", category="project:acme")
    findings = lint_bundle(tmp_path)
    assert _taxonomy(findings, s) == []
    assert _taxonomy(findings, p) == []


@pytest.mark.parametrize("rel", [
    "decisions/ADR-001.md",
    "workflows/WF-001.md",
    "memory/accepted/m.md",
    "reference/unmatched.md",
])
def test_meta_tier_satisfies_path_tiers_outside_the_frontmatter_vocabulary(tmp_path, rel):
    # The path taxonomy assigns tiers such as 'decisions' or 'memory' that the
    # frontmatter vocabulary cannot express; 'meta' is the only valid spelling.
    p = _write(tmp_path, rel, tier="meta")
    assert _taxonomy(lint_bundle(tmp_path), p) == []


def test_scoped_tier_under_a_meta_path_is_a_warning(tmp_path):
    p = _write(tmp_path, "decisions/ADR-001.md", tier="T1")
    found = _taxonomy(lint_bundle(tmp_path), p)
    assert [f.field for f in found] == ["tier"]
    assert "'decisions'" in found[0].message


def test_invalid_tier_value_is_left_to_the_enum_error(tmp_path):
    p = _write(tmp_path, "universal/foundation/STD-U-001.md", tier="T9")
    findings = lint_bundle(tmp_path)[p]
    assert [f.severity for f in findings if f.field == "tier"] == ["error"]


def test_custom_taxonomy_path_is_honoured(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    p = _write(bundle, "standards/STD-1.md", tier="T1", category="standards")
    assert [f.field for f in _taxonomy(lint_bundle(bundle), p)] == ["tier", "category"]

    table = tmp_path / "taxonomy.json"
    table.write_text(json.dumps([["standards/", "T1", "standards"]]), encoding="utf-8")
    monkeypatch.setenv("KB_TAXONOMY_PATH", str(table))
    assert _taxonomy(lint_bundle(bundle), p) == []


def test_lint_files_without_root_skips_the_taxonomy_check(tmp_path):
    p = _write(tmp_path, "universal/foundation/STD-U-001.md", tier="T4")
    assert _taxonomy(lint_files(discover_bundle_files(tmp_path)), p) == []


def test_cli_prints_the_warning_and_keeps_exit_zero(tmp_path, capsys):
    _write(tmp_path, "universal/foundation/STD-U-001.md", tier="T4")
    code = main(["lint", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "warning: tier:" in out
    assert "0 errors" in out


def test_cli_malformed_taxonomy_skips_the_check_without_failing(tmp_path, monkeypatch, capsys):
    bundle = tmp_path / "bundle"
    _write(bundle, "universal/foundation/STD-U-001.md", tier="T4")
    bad = tmp_path / "taxonomy.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("KB_TAXONOMY_PATH", str(bad))
    code = main(["lint", str(bundle)])
    captured = capsys.readouterr()
    assert code == 0
    assert "warning: tier:" not in captured.out
    assert "KB_TAXONOMY_PATH" in captured.err
