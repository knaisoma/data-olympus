"""Tests for the MCP registry declaration at `server.json` (issue #111).

The registry resolves the entry's `packages[0].version` against PyPI and reads
the `mcp-name` ownership marker out of the description PyPI serves for THAT
version. A version field left behind at an earlier release therefore points the
entry at a description that does not carry the marker, and nothing else in CI
notices: `version-free-guard` only reads `[project].version` from
`pyproject.toml`, and `doc-consistency-guard` only checks the SPEC/adoption
enums. These tests close that gap.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

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
    expected = _declared_version()
    assert entry["version"] == expected
    assert entry["packages"][0]["version"] == expected


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
