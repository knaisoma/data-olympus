"""Tests for the MCP registry declaration at `server.json` (issue #111).

The registry resolves the entry's `packages[0].version` against PyPI and reads
the `mcp-name` ownership marker out of the description PyPI serves for THAT
version. A version field left behind at an earlier release therefore points the
entry at a description that does not carry the marker, and nothing else in CI
notices: `version-free-guard` only reads `[project].version` from
`pyproject.toml`, and `doc-consistency-guard` only checks the SPEC/adoption
enums. These tests close that gap.

On `release/new` and `hotfix/new` the declared version is the placeholder
`0.0.0+unreleased` (docs/releases/placeholder-version.md). There the equality
check is waived and `server.json` must instead keep a concrete stable `X.Y.Z`,
the entry the registry currently serves. Outside that branch context the
placeholder fails these tests.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

import pytest

from scripts.placeholder_version import (
    PLACEHOLDER_VERSION,
    branch_context,
    placeholder_permitted,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _server_json() -> dict:
    return json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))


def _declared_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        version: str = tomllib.load(fh)["project"]["version"]
    return version


def test_both_version_fields_track_the_declared_release() -> None:
    """Both `server.json` versions must equal `[project].version`.

    They are two separate fields and the registry uses the package one, so an
    equality check on only the top-level field would miss the case that actually
    breaks verification.
    """
    entry = _server_json()
    _assert_server_versions(entry, _declared_version(), branch_context(os.environ))


_STABLE = re.compile(r"^\d+\.\d+\.\d+$")


def _assert_server_versions(entry: dict, declared: str, branch: str | None) -> None:
    """Apply the server.json version rule for one declared version and branch.

    A concrete declared version must equal both fields, as before. The
    placeholder is accepted only in the release/new or hotfix/new context, and
    then both fields must be one identical concrete stable `X.Y.Z`: the
    placeholder itself, a candidate or a range must never reach the registry.
    """
    versions = (entry["version"], entry["packages"][0]["version"])
    if declared != PLACEHOLDER_VERSION:
        assert versions == (declared, declared)
        return
    assert placeholder_permitted(branch), (
        f"[project].version is the placeholder {PLACEHOLDER_VERSION}, which is only "
        f"accepted on release/new or hotfix/new (branch context: {branch!r}); set "
        "GITHUB_BASE_REF=release/new to run locally on a cycle branch"
    )
    assert versions[0] == versions[1]
    assert _STABLE.match(versions[0]), versions[0]


def _entry(version: str, package_version: str | None = None) -> dict:
    return {
        "version": version,
        "packages": [{"version": version if package_version is None else package_version}],
    }


@pytest.mark.parametrize("branch", ["release/new", "hotfix/new"])
def test_placeholder_waives_equality_on_cycle_branches(branch: str) -> None:
    _assert_server_versions(_entry("0.11.0"), PLACEHOLDER_VERSION, branch)


@pytest.mark.parametrize(
    "branch", ["main", "feature/x", "release/new-2", "hotfix/new/x", "", None]
)
def test_placeholder_is_refused_outside_cycle_branches(branch: str | None) -> None:
    with pytest.raises(AssertionError, match="only accepted"):
        _assert_server_versions(_entry("0.11.0"), PLACEHOLDER_VERSION, branch)


@pytest.mark.parametrize(
    "entry",
    [
        _entry(PLACEHOLDER_VERSION),
        _entry("0.11.0-rc.1"),
        _entry("0.11.0", "0.10.0"),
        _entry("0.11.0+local"),
    ],
)
def test_placeholder_still_requires_one_concrete_stable_entry(entry: dict) -> None:
    with pytest.raises(AssertionError):
        _assert_server_versions(entry, PLACEHOLDER_VERSION, "release/new")


@pytest.mark.parametrize("branch", ["release/new", "hotfix/new", "main", None])
def test_other_local_versions_never_waive_equality(branch: str | None) -> None:
    with pytest.raises(AssertionError):
        _assert_server_versions(_entry("0.11.0"), "0.0.0+other", branch)
    with pytest.raises(AssertionError):
        _assert_server_versions(_entry("0.11.0"), "0.12.0+unreleased", branch)


@pytest.mark.parametrize("branch", ["release/new", "main", None])
def test_concrete_declared_version_requires_equality_everywhere(branch: str | None) -> None:
    _assert_server_versions(_entry("0.12.0"), "0.12.0", branch)
    with pytest.raises(AssertionError):
        _assert_server_versions(_entry("0.11.0"), "0.12.0", branch)
    with pytest.raises(AssertionError):
        _assert_server_versions(_entry("0.12.0", "0.11.0"), "0.12.0", branch)


def test_readme_carries_the_ownership_marker_for_the_declared_name() -> None:
    """The marker in README.md must name exactly the server in `server.json`.

    PyPI serves README.md as the package description; the registry looks for
    `mcp-name: <name>` there to prove ownership. A rename in one file alone
    silently fails verification.
    """
    name = _server_json()["name"]
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"<!-- mcp-name: {name} -->" in readme


def test_package_identifier_matches_the_distribution_name() -> None:
    """The declared PyPI identifier must be the distribution we actually publish."""
    entry = _server_json()
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        dist_name = tomllib.load(fh)["project"]["name"]
    assert entry["packages"][0]["identifier"] == dist_name


def test_version_fields_are_concrete_releases() -> None:
    """Guard against a placeholder or range reaching the registry."""
    entry = _server_json()
    semver = re.compile(r"^\d+\.\d+\.\d+([.-][0-9A-Za-z.]+)?$")
    assert semver.match(entry["version"])
    assert semver.match(entry["packages"][0]["version"])
