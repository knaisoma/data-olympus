"""Syntactic and structural checks on the packaging and release workflows.

Keeps the release wiring honest without needing a live GitHub Actions run:
- the workflow YAML parses;
- the pull request packaging dry run builds, checks and never uploads;
- stable promotion publishes the MCP registry entry last, with OIDC only.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((_WF / name).read_text())


def test_all_workflow_yaml_parses():
    for wf in _WF.glob("*.y*ml"):
        assert yaml.safe_load(wf.read_text()) is not None, wf.name


def test_reusable_build_runs_twine_check():
    doc = _load("publish-python-build.yml")
    build = doc["jobs"]["build"]
    cmds = " ".join(str(s.get("run", "")) for s in build["steps"])
    assert "twine check" in cmds
    assert "uv build" in cmds


def test_package_dry_run_does_not_upload():
    doc = _load("package-dry-run.yml")
    triggers = doc.get("on", doc.get(True))  # PyYAML 1.1 may parse `on` as true.
    assert set(triggers) == {"pull_request"}
    assert doc["permissions"] == {}
    assert set(doc["jobs"]) == {"dry-run"}
    dry = doc["jobs"]["dry-run"]
    assert dry["uses"] == "./.github/workflows/publish-python-build.yml"
    assert dry["permissions"] == {"contents": "read"}
    # The ref comes from the event's commit, never from event body fields.
    assert dry["with"] == {"ref": "${{ github.sha }}"}
    build_only = _load("publish-python-build.yml")
    assert set(build_only["jobs"]) == {"build"}
    assert "id-token" not in str(build_only)
    assert "pypa/gh-action-pypi-publish" not in str(build_only)


def test_stable_promotion_publishes_the_mcp_registry_entry_last() -> None:
    """Issue #303: the registry entry is published by stable promotion with
    GitHub OIDC, after the GitHub release, from the released source, with a
    pinned and hash-verified publisher and a read-back of the listed version."""
    doc = _load("promote-release.yml")
    job = doc["jobs"]["publish-mcp-registry"]
    # Runs after everything else, so a registry outage cannot block a release.
    assert {"publish-pypi", "release"} <= set(job["needs"])
    assert "publish-mcp-registry" not in str(
        [j.get("needs") for name, j in doc["jobs"].items() if name != "publish-mcp-registry"]
    )
    # OIDC only: no secrets, no token, and nothing beyond read and id-token.
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert "secrets." not in str(job)
    assert "environment" not in job
    checkout = next(s for s in job["steps"] if "actions/checkout" in str(s.get("uses", "")))
    assert checkout["with"]["ref"] == "${{ needs.prove.outputs.source_sha }}"
    commands = "\n".join(str(s.get("run", "")) for s in job["steps"])
    # server.json must describe the released version, in both fields.
    assert "'.version' server.json" in commands
    assert "'.packages[0].version' server.json" in commands
    assert "mcp-name: ${SERVER_NAME}" in commands
    # Pinned publisher, verified by SHA256, never `latest`.
    assert "releases/latest" not in commands
    assert "sha256sum -c" in commands
    assert len(job["env"]["MCP_PUBLISHER_SHA256"]) == 64
    assert job["env"]["MCP_PUBLISHER_VERSION"].startswith("v")
    assert "login github-oidc" in commands
    assert "mcp-publisher publish" in commands
    # Re-runnable and read back: skip an existing version, then confirm it is listed.
    assert "already in the registry" in commands
    assert "registry does not list" in commands
    assert "continue-on-error" not in str(job)


def test_version_guard_allows_only_newly_integrated_release_history() -> None:
    doc = _load("ci.yaml")
    steps = doc["jobs"]["version-free-guard"]["steps"]
    commands = "\n".join(str(step.get("run", "")) for step in steps)

    assert 'TAG="v$HEAD_VERSION"' in commands
    assert 'git show-ref --verify --quiet "refs/tags/$TAG"' in commands
    assert 'git merge-base --is-ancestor "$TAG" HEAD' in commands
    assert '! git merge-base --is-ancestor "$TAG" "origin/$BASE_REF"' in commands
    assert 'git show "$TAG:pyproject.toml"' in commands
    assert 'TAG_VERSION" != "$HEAD_VERSION' in commands
    assert commands.index('git merge-base --is-ancestor "$TAG" HEAD') < (
        commands.index("scripts/check_version_free.py")
    )


def test_rc_build_concurrency_and_permissions() -> None:
    build = _load("rc-build.yml")
    assert build["concurrency"] == {
        "group": "rc-build-${{ github.ref }}", "cancel-in-progress": True,
    }
    assert build["permissions"] == {"contents": "read"}


def test_retired_old_path_workflows_stay_absent() -> None:
    """The old release path is retired; its publishers must not come back."""
    for name in (
        "rc-publish.yml", "tag-release.yml", "set-channel.yml", "release-image.yml",
        "release-image-reusable.yml", "publish-pypi.yml", "publish-pypi-reusable.yml",
    ):
        assert not (_WF / name).exists(), name
