"""Effective code ownership: the admins team owns every path by default,
documentation and tests carry no owner, and the release documents, the two
test files that privileged workflows read and every security-sensitive path
stay owned. CODEOWNERS uses last-match-wins, which _owner() reproduces."""
from __future__ import annotations

import fnmatch
from pathlib import Path

CODEOWNERS = Path(".github/CODEOWNERS")
ADMINS = ["@knaisoma/kn-data-olympus-admins"]


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
    anchored = pattern.lstrip("/")
    if pattern.endswith("/"):
        return path.startswith(anchored)
    return path == anchored or fnmatch.fnmatchcase(path, anchored)


def _owner(path: str) -> list[str]:
    owners: list[str] = []
    for pattern, rule_owners in _rules():
        if _matches(pattern, path):
            owners = rule_owners
    return owners


def test_the_catch_all_default_stays() -> None:
    assert _rules()[0] == ("*", ADMINS)


def test_documentation_and_tests_have_no_code_owner() -> None:
    for path in ("docs/serving.md", "docs/guide/x.md", "tests/test_x.py", "tests/fixtures/a.json",
                 "CHANGELOG.md", "README.md"):
        assert _owner(path) == [], path


def test_release_documents_and_privileged_test_inputs_stay_owned() -> None:
    for path in ("docs/releases/reviewed-promotion.md", "docs/releases/std-u-821-amendment-1.3.md",
                 "tests/okf/reference.json", "tests/test_rc_verify_and_publish.py"):
        assert _owner(path) == ADMINS, path


def test_source_and_security_sensitive_paths_stay_owned() -> None:
    for path in ("src/data_olympus/principals.py", "src/data_olympus/write_gate.py",
                 "src/data_olympus/mcp_sanitize.py", "src/data_olympus/auth.py", "SPEC.md",
                 "pyproject.toml", "uv.lock", "scripts/release_record.py", "deploy/docker/Dockerfile",
                 "release/exceptions.json", ".github/workflows/ci.yaml", ".github/CODEOWNERS",
                 "tomllib.py", "conftest.py"):
        assert _owner(path) == ADMINS, path
