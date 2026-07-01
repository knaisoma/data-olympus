# tests/test_should_tag.py
"""Pure logic for the tag-on-version-bump decision."""
from __future__ import annotations

import pytest

from scripts.should_tag import project_version, tag_to_create

PYPROJECT = '[project]\nname = "data-olympus"\nversion = "0.1.2"\n'


def test_project_version_parsed() -> None:
    assert project_version(PYPROJECT) == "0.1.2"


def test_project_version_missing_raises() -> None:
    with pytest.raises(ValueError):
        project_version('[project]\nname = "x"\n')


def test_tag_created_when_missing() -> None:
    assert tag_to_create("0.1.2", {"v0.1.0", "v0.1.1"}) == "v0.1.2"


def test_no_tag_when_present() -> None:
    assert tag_to_create("0.1.1", {"v0.1.0", "v0.1.1"}) is None


def test_no_tag_on_empty_repo_when_version_already_tagged() -> None:
    assert tag_to_create("0.1.1", {"v0.1.1"}) is None
