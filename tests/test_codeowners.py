"""Effective code ownership: the admins team owns every path by default,
documentation and tests carry no owner, and the release documents, two
protected test files and every security-sensitive path stay owned.
CODEOWNERS uses last-match-wins, which _owner() reproduces for the only
pattern forms this file uses: `*`, an anchored directory `/dir/` (every
descendant) and an anchored file `/path`. Any other form fails the test, so
a wildcard whose GitHub meaning this model does not reproduce cannot slip in."""
from __future__ import annotations

from pathlib import Path

import pytest

CODEOWNERS = Path(".github/CODEOWNERS")
ADMINS = ["@knaisoma/kn-data-olympus-admins"]
UNOWNED = ("/docs/", "/tests/", "/CHANGELOG.md", "/README.md")
PROTECTED = (
    "/docs/releases/",
    "/tests/okf/reference.json",
    "/tests/test_rc_verify_and_publish.py",
    "/SPEC.md",
    "/src/data_olympus/format/",
    "/src/data_olympus/server.py",
    "/src/data_olympus/rest_api.py",
    "/src/data_olympus/tools_write.py",
    "/src/data_olympus/auth.py",
    "/src/data_olympus/pending.py",
    "/src/data_olympus/push_queue.py",
    "/src/data_olympus/git_ops.py",
    "/deploy/",
    "/scripts/",
    "/release/",
    "/docs/releases/std-u-821-amendment-1.3.md",
    "/docs/releases/std-u-821-amendment-1.5.md",
    "/.github/",
    "/.github/workflows/",
    "/.github/CODEOWNERS",
)


def _rules() -> list[tuple[str, list[str]]]:
    rules = []
    for raw in CODEOWNERS.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            pattern, *owners = line.split()
            rules.append((pattern, owners))
    return rules


def _matches(pattern: str, path: str) -> bool:
    if pattern == "*":
        return True
    if not pattern.startswith("/") or any(c in pattern for c in "*?[]!\\"):
        raise AssertionError(f"unsupported CODEOWNERS pattern form: {pattern!r}")
    anchored = pattern[1:]
    if pattern.endswith("/"):
        return path.startswith(anchored)
    return path == anchored


def _owner(path: str) -> list[str]:
    owners: list[str] = []
    for pattern, rule_owners in _rules():
        if _matches(pattern, path):
            owners = rule_owners
    return owners


def _samples(pattern: str) -> list[str]:
    anchored = pattern[1:]
    return [anchored + "x.md", anchored + "a/b/c.py"] if pattern.endswith("/") else [anchored]


def test_the_rules_are_exactly_the_default_the_unowned_and_the_protected_entries() -> None:
    rules = _rules()
    assert rules[0] == ("*", ADMINS)
    assert {p for p, o in rules if not o} == set(UNOWNED)
    assert {p for p, o in rules[1:] if o} == set(PROTECTED)
    assert all(o == ADMINS for p, o in rules if o)


@pytest.mark.parametrize("pattern", UNOWNED)
def test_documentation_and_tests_have_no_code_owner(pattern: str) -> None:
    for path in _samples(pattern):
        protected_dir = any(path.startswith(p[1:]) for p in PROTECTED if p.endswith("/"))
        if protected_dir or "/" + path in PROTECTED:
            continue
        assert _owner(path) == [], path


@pytest.mark.parametrize("pattern", PROTECTED)
def test_every_protected_entry_and_its_descendants_stay_owned(pattern: str) -> None:
    for path in _samples(pattern):
        assert _owner(path) == ADMINS, path


def test_other_source_dependency_and_root_files_keep_the_default_owner() -> None:
    owned = (
        "src/data_olympus/principals.py", "src/data_olympus/write_gate.py",
        "src/data_olympus/mcp_sanitize.py", "pyproject.toml", "uv.lock", "tomllib.py",
        "conftest.py", "benchmarks/run.py",
    )
    for path in owned:
        assert _owner(path) == ADMINS, path


def test_unsupported_pattern_forms_are_refused() -> None:
    for pattern in ("/tests/*", "tests/", "/docs/**", "*.md"):
        with pytest.raises(AssertionError, match="unsupported"):
            _matches(pattern, "tests/a/b.py")
