"""Pure logic for the changelog CI guard."""
from __future__ import annotations

from scripts.check_changelog import needs_changelog


def test_functional_change_without_changelog_fails() -> None:
    assert needs_changelog(["src/data_olympus/server.py"], label_skip=False) is True


def test_functional_change_with_changelog_passes() -> None:
    assert needs_changelog(["src/data_olympus/server.py", "CHANGELOG.md"],
                           label_skip=False) is False


def test_docs_only_change_passes() -> None:
    assert needs_changelog(["docs/enforcement.md", "README.md"], label_skip=False) is False


def test_tests_only_change_passes() -> None:
    assert needs_changelog(["tests/test_x.py"], label_skip=False) is False


def test_label_skip_overrides() -> None:
    assert needs_changelog(["src/data_olympus/server.py"], label_skip=True) is False
