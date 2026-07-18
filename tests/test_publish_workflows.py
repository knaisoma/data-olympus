"""Syntactic + structural checks on the PyPI publish workflows and packaging.

Keeps the release wiring honest without needing a live GitHub Actions run:
- the workflow YAML parses;
- the reusable upload job is trusted-publishing + inert-until-setup shaped;
- the normal release path (tag-release.yml) publishes to PyPI inline (not via the
  reusable workflow, which PyPI trusted publishing does not match through);
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


def test_tag_release_publishes_pypi_inline():
    """The primary release path publishes to PyPI with the steps INLINE in
    tag-release.yml, not through a reusable workflow. PyPI trusted publishing does
    not reliably match a publisher when the upload runs inside a called reusable
    workflow, so the entry workflow must be the publishing workflow."""
    doc = _load("tag-release.yml")
    pub = doc["jobs"]["publish-pypi"]
    # Inline job: no `uses:` delegation to a reusable workflow.
    assert "uses" not in pub
    assert pub["permissions"]["id-token"] == "write"
    assert pub["environment"]["name"] == "pypi"
    # Checks out the same decided tag as the image build.
    checkout = next(s for s in pub["steps"] if "actions/checkout" in str(s.get("uses", "")))
    assert checkout["with"]["ref"] == "${{ needs.decide.outputs.tag }}"
    publish = next(
        s for s in pub["steps"] if "pypa/gh-action-pypi-publish" in str(s.get("uses", ""))
    )
    # Trusted publishing only (no token) + idempotent rerun.
    assert "password" not in (publish.get("with") or {})
    assert publish["with"]["skip-existing"] is True
    # The publisher is configured now, so a real failure must be visible.
    assert "continue-on-error" not in publish
    # PyPI publish must not gate the GitHub Release (release needs only build-image).
    assert "publish-pypi" not in doc["jobs"]["release"]["needs"]


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


def test_reusable_image_build_checks_out_and_labels_explicit_ref():
    doc = _load("release-image-reusable.yml")
    triggers = doc.get("on", doc.get(True))  # PyYAML 1.1 may parse `on` as true.
    ref_input = triggers["workflow_call"]["inputs"]["ref"]
    assert ref_input["required"] is True

    steps = doc["jobs"]["build-push"]["steps"]
    checkout = next(s for s in steps if "actions/checkout" in str(s.get("uses", "")))
    assert checkout["with"]["ref"] == "${{ inputs.ref }}"

    source = next(s for s in steps if s.get("id") == "source")
    assert "git rev-parse HEAD" in source["run"]

    build = next(s for s in steps if "docker/build-push-action" in str(s.get("uses", "")))
    assert "org.opencontainers.image.revision=${{ steps.source.outputs.sha }}" in build["with"][
        "labels"
    ]


def test_every_reusable_image_caller_passes_a_ref():
    for name in ("rc-publish.yml", "release-image.yml", "tag-release.yml"):
        doc = _load(name)
        callers = [
            job
            for job in doc["jobs"].values()
            if str(job.get("uses", "")).endswith("release-image-reusable.yml")
        ]
        assert callers, name
        for caller in callers:
            assert caller["with"].get("ref"), (name, caller)


def test_rc_resolves_requested_ref_once_and_reuses_exact_sha():
    doc = _load("rc-publish.yml")
    decide = doc["jobs"]["decide"]
    assert decide["outputs"]["source_sha"] == "${{ steps.source.outputs.sha }}"
    source = next(s for s in decide["steps"] if s.get("id") == "source")
    assert "git rev-parse HEAD" in source["run"]

    assert doc["jobs"]["build-image"]["with"]["ref"] == (
        "${{ needs.decide.outputs.source_sha }}"
    )
    prerelease = doc["jobs"]["prerelease"]
    checkout = next(
        s for s in prerelease["steps"] if "actions/checkout" in str(s.get("uses", ""))
    )
    assert checkout["with"]["ref"] == "${{ needs.decide.outputs.source_sha }}"
    create = next(s for s in prerelease["steps"] if s.get("name") == "Create GitHub pre-release")
    assert '--target "$SOURCE_SHA"' in create["run"]
