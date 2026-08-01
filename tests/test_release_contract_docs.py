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
    text = "\n".join(_read(path) for path in RULES)

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
    text = "\n".join(_read(path) for path in RULES)

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

    assert "`chore/release-vX.Y.Z-RUN`" in versioning
    assert "`chore/release-vX.Y.Z-RUN`" in routine
    assert "linear history" in versioning
    assert "squash merged" in versioning
    assert "feature/<release-epic-id>" not in versioning
    assert "feature/<release-epic-id>" not in routine


def test_release_planning_is_outcome_based() -> None:
    planning = _read(".rules/release-planning.md")

    assert "already\nmerged, reviewed, green, and unreleased" in planning
    assert "does not select future issues" in planning
    assert "No action" in planning
    assert "three to five" not in planning
    assert "strict 1-week" not in planning
