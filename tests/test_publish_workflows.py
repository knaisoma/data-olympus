"""Syntactic + structural checks on the PyPI publish workflows and packaging.

Keeps the release wiring honest without needing a live GitHub Actions run:
- the workflow YAML parses;
- the reusable upload job is trusted-publishing + inert-until-setup shaped;
- the normal release path (tag-release.yml) calls the reusable publish workflow;
- the PR dry-run does not upload.
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


def test_reusable_publish_uses_trusted_publishing():
    doc = _load("publish-pypi-reusable.yml")
    upload = doc["jobs"]["upload"]
    # OIDC trusted publishing: id-token write, pypi environment, no token.
    assert upload["permissions"]["id-token"] == "write"
    assert upload["environment"]["name"] == "pypi"
    steps = upload["steps"]
    publish = next(s for s in steps if "pypa/gh-action-pypi-publish" in str(s.get("uses", "")))
    # No password/token: trusted publishing only.
    assert "password" not in (publish.get("with") or {})
    # skip-existing makes a partial-failure rerun idempotent.
    assert publish["with"]["skip-existing"] is True


def test_reusable_upload_gated_on_input():
    doc = _load("publish-pypi-reusable.yml")
    # upload job only runs when the caller asks for upload=true. GitHub accepts a
    # bare expression at job-level `if`, so YAML loads it without the ${{ }} wrap.
    assert doc["jobs"]["upload"]["if"] == "inputs.upload"


def test_reusable_build_runs_twine_check():
    doc = _load("publish-pypi-reusable.yml")
    build = doc["jobs"]["build"]
    cmds = " ".join(str(s.get("run", "")) for s in build["steps"])
    assert "twine check" in cmds
    assert "uv build" in cmds


def test_publish_pr_dry_run_does_not_upload():
    doc = _load("publish-pypi.yml")
    dry = doc["jobs"]["dry-run"]
    assert dry["with"]["upload"] is False
    # dry-run fires on pull_request only.
    assert "pull_request" in dry["if"]


def test_tag_release_calls_publish_reusable():
    doc = _load("tag-release.yml")
    pub = doc["jobs"]["publish-pypi"]
    assert pub["uses"].endswith("publish-pypi-reusable.yml")
    assert pub["with"]["upload"] is True
    assert pub["permissions"]["id-token"] == "write"
    # It keys off the same decided tag as the image build.
    assert pub["with"]["ref"] == "${{ needs.decide.outputs.tag }}"


def test_publish_step_inert_until_setup():
    """The upload step (not the caller job) carries continue-on-error, so a
    pre-setup PyPI failure never blocks the release. continue-on-error is invalid
    on a `uses:` job, so it MUST live on the step."""
    doc = _load("publish-pypi-reusable.yml")
    steps = doc["jobs"]["upload"]["steps"]
    publish = next(s for s in steps if "pypa/gh-action-pypi-publish" in str(s.get("uses", "")))
    assert publish["continue-on-error"] is True


def test_manual_dispatch_uploads_only_for_validated_release_tag():
    """workflow_dispatch must not let an arbitrary branch/SHA upload to PyPI: the
    manual job's upload flag is gated on a validate step that only allows v* tags."""
    doc = _load("publish-pypi.yml")
    jobs = doc["jobs"]
    assert "validate-manual" in jobs
    validate = jobs["validate-manual"]
    # It computes an `upload` output from the input ref.
    assert "upload" in validate["outputs"]
    check = " ".join(str(s.get("run", "")) for s in validate["steps"])
    # Guards on a v-semver tag pattern and an existing tag object.
    assert "v[0-9]" in check
    assert "refs/tags/" in check
    # The manual publish job keys its upload off the validate output, not a bare true.
    manual = jobs["manual"]
    assert manual["needs"] == "validate-manual"
    assert "validate-manual.outputs.upload" in manual["with"]["upload"]


def test_publish_pr_dry_run_ref_is_not_untrusted():
    """The reusable workflow's ref must come from a trusted source, never event
    body fields (injection guard)."""
    doc = _load("publish-pypi.yml")
    for job in ("dry-run", "release", "manual"):
        ref = doc["jobs"][job]["with"]["ref"]
        assert ref in (
            "${{ github.sha }}",
            "${{ github.ref_name }}",
            "${{ inputs.ref }}",
        ), (job, ref)
