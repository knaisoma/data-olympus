"""Checks for the release scope issue form and its documented label set.

GitHub rejects an issue form that breaks its schema only when someone opens
the "New issue" page, so a broken form would go unnoticed in CI. These tests
apply the parts of GitHub's issue form and label rules that a hand edit can
break, and keep the label list in the docs in step with `.github/labels.yml`.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FORM = ROOT / ".github" / "ISSUE_TEMPLATE" / "release-scope.yml"
LABELS = ROOT / ".github" / "labels.yml"
DOC = ROOT / "docs" / "releases" / "release-scope.md"

_ELEMENT_TYPES = {"markdown", "textarea", "input", "dropdown", "checkboxes"}
_ID = re.compile(r"[A-Za-z0-9_-]+")
_COLOR = re.compile(r"[0-9a-f]{6}")


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_form_follows_the_issue_form_schema() -> None:
    form = _load(FORM)
    assert form["name"].strip() and form["description"].strip()
    body = form["body"]
    assert any(element["type"] != "markdown" for element in body)

    ids: list[str] = []
    labels: list[str] = []
    for element in body:
        assert element["type"] in _ELEMENT_TYPES, element
        if element["type"] == "markdown":
            assert element["attributes"]["value"].strip()
            continue
        assert _ID.fullmatch(element["id"]), element["id"]
        ids.append(element["id"])
        labels.append(element["attributes"]["label"].strip())
        assert isinstance(element.get("validations", {}).get("required", False), bool)
    assert len(ids) == len(set(ids)), "duplicate element id"
    assert all(labels) and len(labels) == len(set(labels)), "empty or duplicate label"


def test_approval_fields_cannot_be_filled_by_the_form_author_by_default() -> None:
    """Approval is the operator's act: the form must not demand or prefill it."""
    by_id = {e.get("id"): e for e in _load(FORM)["body"]}
    for field in ("approving_operator", "scope_revision"):
        element = by_id[field]
        assert element["validations"]["required"] is False
        assert not element["attributes"].get("value")


def test_label_file_meets_github_label_limits() -> None:
    labels = _load(LABELS)
    names = [label["name"] for label in labels]
    assert len(names) == len(set(names))
    for label in labels:
        assert 0 < len(label["name"]) <= 50
        assert _COLOR.fullmatch(str(label["color"])), label
        assert 0 < len(label["description"]) <= 100, label["name"]


def test_documented_labels_match_the_label_file() -> None:
    section = DOC.read_text(encoding="utf-8").split("## Labels", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^- `([a-z0-9-]+)`:", section, flags=re.MULTILINE))
    assert documented == {label["name"] for label in _load(LABELS)}

    intro = _load(FORM)["body"][0]["attributes"]["value"]
    for name in documented:
        assert f"`{name}`" in intro, name
