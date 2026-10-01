"""Parity between the index taxonomy and the CLI fallback's copy (issue #304).

The default path taxonomy and its classifier exist twice: in
src/data_olympus/index.py, which the server indexes with, and in
bin/_kb_fallback.py, the dependency-free local search the CLI falls back to
when the server is unreachable. The fallback cannot import the package, so the
copy stays, and these tests are what keep it honest: if either copy drifts, the
server and the fallback would classify the same document into different tiers.

Each parity check is a helper so the drift tests below can prove the check
actually fails when one copy is mutated, rather than passing vacuously.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from data_olympus import index

if TYPE_CHECKING:
    from types import ModuleType

# Representative paths covering every branch of both classifiers.
REPRESENTATIVE_PATHS: tuple[str, ...] = (
    "universal/foundation/STD-U-001.md",
    "universal/services/x.md",
    "universal/unlisted/x.md",
    "universal/README.md",
    "universal/",
    # T4: projects/<name>/components/<component>/...
    "projects/acme/components/billing/rules.md",
    "projects/acme/components/billing/deep/nested.md",
    # A loose file directly under components/ stays T3.
    "projects/acme/components/README.md",
    "projects/acme/overview.md",
    "projects/index.md",
    "projects/",
    # T2: tech-stacks/<stack>/...
    "tech-stacks/backend-go/STD-BG-001.md",
    "tech-stacks/index.md",
    "tech-stacks/",
    "decisions/GDEC-001.md",
    "workflows/WF-001.md",
    "memory/inbox/2026-01-01-x.md",
    "memory/accepted/x.md",
    "memory/x.md",
    "tooling/x.md",
    "templates/x.md",
    # Windows separators are normalised by both.
    "projects\\acme\\components\\billing\\rules.md",
    # Unmatched.
    "operator/memory/inbox/x.md",
    "README.md",
    "",
)

# A custom table that keeps the two dynamic prefixes, reorders them, and adds
# deployment-specific prefixes, so the custom-table parity exercises the same
# T3/T4 and stack branches under a non-default ordering.
CUSTOM_TABLE: list[list[str]] = [
    ["operator/memory/inbox/", "operator", "memory-inbox"],
    ["operator/", "operator", "operator"],
    ["projects/", "T3", "project"],
    ["standards/", "T1", "standards"],
    ["tech-stacks/", "T2", "stack"],
]
CUSTOM_PATHS: tuple[str, ...] = REPRESENTATIVE_PATHS + (
    "operator/agent-overrides/claude.md",
    "standards/api.md",
)


def _load_fallback() -> ModuleType:
    repo_root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "_kb_fallback_parity", repo_root / "bin" / "_kb_fallback.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fallback(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.delenv("KB_TAXONOMY_PATH", raising=False)
    return _load_fallback()


def _assert_default_rules_match(fb: ModuleType) -> None:
    # Entry for entry and in order: the first matching prefix wins.
    assert [tuple(r) for r in fb._DEFAULT_PATH_RULES] == list(index._DEFAULT_PATH_RULES)


def _assert_classifiers_agree(fb: ModuleType, paths: tuple[str, ...]) -> None:
    mismatches = {
        p: (index._classify_by_path(p), fb._classify(p))
        for p in paths
        if index._classify_by_path(p) != fb._classify(p)
    }
    assert mismatches == {}, f"index vs fallback disagree: {mismatches}"


def _use_custom_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    taxonomy = tmp_path / "taxonomy.json"
    taxonomy.write_text(json.dumps(CUSTOM_TABLE), encoding="utf-8")
    monkeypatch.setenv("KB_TAXONOMY_PATH", str(taxonomy))


# ---- parity ----------------------------------------------------------------

def test_fallback_default_rules_equal_index_rules_in_order(fallback: ModuleType) -> None:
    _assert_default_rules_match(fallback)


def test_classifiers_agree_on_representative_paths(fallback: ModuleType) -> None:
    _assert_classifiers_agree(fallback, REPRESENTATIVE_PATHS)


def test_classifiers_agree_with_custom_taxonomy_path(
    fallback: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_custom_table(tmp_path, monkeypatch)
    # Guard against a vacuous pass: the custom table must actually be in force.
    assert index._classify_by_path("standards/api.md") == ("T1", "standards")
    assert index._classify_by_path("decisions/GDEC-001.md") == ("meta", "meta")
    _assert_classifiers_agree(fallback, CUSTOM_PATHS)


def test_both_loaders_reject_a_malformed_taxonomy(
    fallback: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([["universal/", "T1"]]), encoding="utf-8")
    monkeypatch.setenv("KB_TAXONOMY_PATH", str(bad))
    with pytest.raises(ValueError, match="KB_TAXONOMY_PATH"):
        index._classify_by_path("universal/foundation/x.md")
    with pytest.raises(ValueError, match="KB_TAXONOMY_PATH"):
        fallback._classify("universal/foundation/x.md")


# ---- the parity checks detect drift ------------------------------------------

def _reordered(rules: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Move the broad memory/ rule ahead of memory/inbox/, a plausible edit slip."""
    out = [r for r in rules if r[0] != "memory/"]
    at = next(i for i, r in enumerate(out) if r[0] == "memory/inbox/")
    return out[:at] + [("memory/", "memory", "memory")] + out[at:]


def test_rule_parity_detects_a_reordered_fallback_copy(
    fallback: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fallback, "_DEFAULT_PATH_RULES", _reordered(fallback._DEFAULT_PATH_RULES))
    with pytest.raises(AssertionError):
        _assert_default_rules_match(fallback)
    with pytest.raises(AssertionError, match="memory/inbox/"):
        _assert_classifiers_agree(fallback, REPRESENTATIVE_PATHS)


def test_rule_parity_detects_an_edited_index_copy(
    fallback: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    edited = tuple(
        (p, t, "base") if p == "universal/foundation/" else (p, t, c)
        for p, t, c in index._DEFAULT_PATH_RULES
    )
    monkeypatch.setattr(index, "_DEFAULT_PATH_RULES", edited)
    with pytest.raises(AssertionError):
        _assert_default_rules_match(fallback)
    with pytest.raises(AssertionError, match="universal/foundation/"):
        _assert_classifiers_agree(fallback, REPRESENTATIVE_PATHS)


def test_classifier_parity_detects_drifted_component_logic(
    fallback: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fallback classifier that forgot the T4 branch keeps the rules equal
    # but classifies component paths as T3; only the classifier check sees it.
    original = fallback._classify

    def no_t4(rel: str) -> tuple[str, str]:
        tier, category = original(rel)
        if tier == "T4":
            return "T3", "project:" + category.split(":", 1)[1].split("/", 1)[0]
        return tier, category

    monkeypatch.setattr(fallback, "_classify", no_t4)
    _assert_default_rules_match(fallback)
    with pytest.raises(AssertionError, match="components/billing"):
        _assert_classifiers_agree(fallback, REPRESENTATIVE_PATHS)


def test_custom_table_parity_detects_a_loader_ignoring_the_env(
    fallback: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_custom_table(tmp_path, monkeypatch)
    monkeypatch.setattr(fallback, "_load_path_rules", lambda: fallback._DEFAULT_PATH_RULES)
    with pytest.raises(AssertionError, match="standards/api.md"):
        _assert_classifiers_agree(fallback, CUSTOM_PATHS)
