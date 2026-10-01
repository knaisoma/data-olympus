"""Keep agent runbooks aligned with the executable release workflows."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES = (
    ".rules/versioning.md",
    ".rules/release-rollback.md",
    ".rules/release-routine.md",
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_local_agent_rules_describe_complete_candidates() -> None:
    text = " ".join("\n".join(_read(path) for path in RULES).split())

    for required in (
        "wheel",
        "sdist",
        "release-provenance.json",
        "PyPI",
        "GHCR",
        "GitHub prerelease",
        "exact source SHA",
    ):
        assert required in text
    assert "image-only" not in text
    assert "nothing external shipped" not in text


def test_local_agent_rules_require_explicit_stable_promotion() -> None:
    text = " ".join("\n".join(_read(path) for path in RULES).split())

    assert "workflow_dispatch" in text
    assert "candidate_tag" in text
    assert "highest complete candidate" in text
    assert "protected `pypi` environment" in text
    assert "on merge (detected via" not in text
    assert "Merge the release PR -> `tag-release.yml`" not in text


def test_release_notes_cover_the_expanded_060_contract() -> None:
    changelog = _read("CHANGELOG.md")
    notes = _read("docs/releases/v0.6.0.md")
    combined = changelog + notes

    for required in (
        "searchable",
        "OKF",
        "PyPI",
        "provenance",
        "Trusted Publishing",
        "installed wheel",
    ):
        assert required in combined


def test_versioning_rule_uses_one_linear_release_change() -> None:
    versioning = _read(".rules/versioning.md")
    routine = _read(".rules/release-routine.md")

    assert "`codex/release-YYYY-MM-DD`" in versioning
    assert "`codex/release-YYYY-MM-DD`" in routine
    assert "linear history" in versioning
    assert "squash merged" in versioning
    assert "feature/<release-epic-id>" not in versioning
    assert "feature/<release-epic-id>" not in routine


def test_release_routine_reviews_before_merge_and_proves_content_transfer() -> None:
    routine = _read(".rules/release-routine.md")
    normalized = " ".join(routine.split())

    for required in (
        "Keep it open and unmerged",
        "GitHub Code Quality is not a dependency",
        "aggregate `CodeQL` check",
        "review of `H`",
        "expected head `H`",
        "sole parent `B`",
        "tree exactly equals reviewed tree `T`",
        "entire GitHub ruleset",
        "direct continuation",
        "Candidate fields remain `H`",
    ):
        assert required in normalized
    assert "Code Quality must be configured" not in normalized
    assert "review of the exact final SHA" not in normalized
    assert "bypass only the native approval" not in normalized


def test_release_planning_selects_a_coherent_issue_batch() -> None:
    planning = " ".join(_read(".rules/release-planning.md").split())
    assert "three to five" in planning
    assert "before implementation" in planning
    assert "No action" in planning
    assert "fixes, new capabilities, and improvements" in planning


def test_derived_from_is_documented_on_every_surface() -> None:
    """Issue #300, acceptance check 16."""
    spec = _read("SPEC.md")
    section_42 = spec.split("### 4.2", 1)[1].split("### 4.3", 1)[0]
    section_9 = spec.split("## 9.", 1)[1].split("## 10.", 1)[0]
    section_10 = spec.split("## 10.", 1)[1].split("## 11.", 1)[0]
    for text in (section_42, section_9, section_10):
        assert "derived_from" in text
    for field in ("derived_from_retired", "dependents_to_review"):
        assert field in section_42
    assert "**Version:** 0.5" in spec
    assert "`0.5`" in section_10
    assert "rejected" in section_42.split("derived_from_retired", 1)[0] or (
        "`rejected`" in section_42)

    for doc in ("docs/serving.md", "docs/okf-profile.md"):
        text = _read(doc)
        for field in ("derived_from", "derived_from_retired", "dependents_to_review"):
            assert field in text, (doc, field)
    serving = _read("docs/serving.md")
    assert "never filtered" in serving or "nothing is filtered" in serving.lower()
