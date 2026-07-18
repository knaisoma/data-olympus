"""Syntactic and structural checks on the release workflows and packaging.

Keeps the release wiring honest without needing a live GitHub Actions run:
- the workflow YAML parses;
- every upload job uses fail-closed Trusted Publishing and hash readback;
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
    # Checks out the exact source SHA carried by the complete RC.
    checkout = next(s for s in pub["steps"] if "actions/checkout" in str(s.get("uses", "")))
    assert checkout["with"]["ref"] == "${{ needs.resolve-rc.outputs.source_sha }}"
    publish = next(
        s for s in pub["steps"] if "pypa/gh-action-pypi-publish" in str(s.get("uses", ""))
    )
    # Trusted publishing only (no token) + idempotent rerun.
    assert "password" not in (publish.get("with") or {})
    assert publish["with"]["skip-existing"] is True
    # The publisher is configured now, so a real failure must be visible.
    assert "continue-on-error" not in publish
    # A stable GitHub release is complete only after Python and OCI promotion.
    assert "publish-pypi" in doc["jobs"]["release"]["needs"]
    assert "promote-image" in doc["jobs"]["release"]["needs"]


def test_stable_promotion_is_explicit_and_tag_follows_approved_pypi() -> None:
    doc = _load("tag-release.yml")
    triggers = doc.get("on", doc.get(True))
    assert "push" not in triggers
    candidate = triggers["workflow_dispatch"]["inputs"]["candidate_tag"]
    assert candidate["required"] is True
    assert "publish-pypi" in doc["jobs"]["create-tag"]["needs"]
    assert "create-tag" not in doc["jobs"]["publish-pypi"]["needs"]


def test_reusable_publish_fails_closed_and_verifies_remote_hashes() -> None:
    doc = _load("publish-pypi-reusable.yml")
    steps = doc["jobs"]["upload"]["steps"]
    publish = next(s for s in steps if "pypa/gh-action-pypi-publish" in str(s.get("uses", "")))
    assert "continue-on-error" not in publish
    commands = "\n".join(str(step.get("run", "")) for step in steps)
    assert "pypi.org/pypi/data-olympus" in commands
    assert "sha256" in commands


def test_every_pypi_publisher_fails_closed_and_reads_back_hashes() -> None:
    publishers = (
        ("publish-pypi-reusable.yml", "upload"),
        ("rc-publish.yml", "publish-pypi"),
        ("tag-release.yml", "publish-pypi"),
    )
    for workflow, job_name in publishers:
        steps = _load(workflow)["jobs"][job_name]["steps"]
        publish = next(
            step
            for step in steps
            if "pypa/gh-action-pypi-publish" in str(step.get("uses", ""))
        )
        assert "continue-on-error" not in publish, workflow
        commands = "\n".join(str(step.get("run", "")) for step in steps)
        assert "pypi.org/pypi/data-olympus" in commands, workflow
        assert "sha256" in commands, workflow
        assert 'glob("*.whl")' in commands, workflow
        assert 'glob("*.tar.gz")' in commands, workflow
        assert 'Path("dist").iterdir()' not in commands, workflow


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
    assert build["id"] == "build"
    assert "org.opencontainers.image.revision=${{ steps.source.outputs.sha }}" in build["with"][
        "labels"
    ]
    assert doc["jobs"]["build-push"]["outputs"]["digest"] == "${{ steps.build.outputs.digest }}"


def test_every_reusable_image_caller_passes_a_ref():
    for name in ("rc-publish.yml", "release-image.yml"):
        doc = _load(name)
        callers = [
            job
            for job in doc["jobs"].values()
            if str(job.get("uses", "")).endswith("release-image-reusable.yml")
        ]
        assert callers, name
        for caller in callers:
            assert caller["with"].get("ref"), (name, caller)
    stable = _load("tag-release.yml")
    assert all(
        not str(job.get("uses", "")).endswith("release-image-reusable.yml")
        for job in stable["jobs"].values()
    )


def test_rc_resolves_requested_ref_once_and_reuses_exact_sha():
    doc = _load("rc-publish.yml")
    decide = doc["jobs"]["decide"]
    assert decide["outputs"]["source_sha"] == "${{ steps.source.outputs.sha }}"
    source = next(s for s in decide["steps"] if s.get("id") == "source")
    assert "git rev-parse HEAD" in source["run"]

    assert doc["jobs"]["build-image"]["with"]["ref"] == (
        "${{ needs.decide.outputs.source_sha }}"
    )
    assert doc["jobs"]["build-image"]["with"].get("channel", "") == ""
    prerelease = doc["jobs"]["finalize"]
    checkout = next(
        s for s in prerelease["steps"] if "actions/checkout" in str(s.get("uses", ""))
    )
    assert checkout["with"]["ref"] == "${{ needs.decide.outputs.source_sha }}"
    create = next(s for s in prerelease["steps"] if s.get("name") == "Create GitHub pre-release")
    assert '--target "$SOURCE_SHA"' in create["run"]
    assert "targetCommitish" not in create["run"]
    assert "git rev-list -n 1" in create["run"]


def test_rc_publishes_python_inline_before_moving_channels() -> None:
    doc = _load("rc-publish.yml")
    publish = doc["jobs"]["publish-pypi"]
    assert "uses" not in publish
    assert publish["permissions"]["id-token"] == "write"
    assert publish["environment"]["name"] == "pypi"
    assert "build-python" in publish["needs"]
    action = next(
        step
        for step in publish["steps"]
        if "pypa/gh-action-pypi-publish" in str(step.get("uses", ""))
    )
    assert action["with"]["skip-existing"] is True
    assert "password" not in (action.get("with") or {})
    commands = "\n".join(str(step.get("run", "")) for step in publish["steps"])
    assert "pypi.org/pypi/data-olympus" in commands
    assert "sha256" in commands


def test_rc_builds_provenance_and_finalizes_only_after_complete_publication() -> None:
    doc = _load("rc-publish.yml")
    triggers = doc.get("on", doc.get(True))
    assert triggers["workflow_dispatch"]["inputs"]["number"]["required"] is True
    build = doc["jobs"]["build-python"]
    commands = "\n".join(str(step.get("run", "")) for step in build["steps"])
    assert "scripts/release_artifacts.py candidate" in commands
    upload = next(
        step for step in build["steps"] if "actions/upload-artifact" in str(step.get("uses", ""))
    )
    paths = str(upload["with"]["path"])
    assert "dist/" in paths
    assert "release-provenance.json" in paths

    finalize = doc["jobs"]["finalize"]
    assert set(finalize["needs"]) == {
        "decide",
        "inspect-image",
        "build-image",
        "build-python",
        "publish-pypi",
    }
    final_commands = "\n".join(str(step.get("run", "")) for step in finalize["steps"])
    assert "imagetools create" in final_commands
    assert ":rc" in final_commands
    assert "scripts/release_artifacts.py finalize-candidate" in final_commands
    assert "jq --arg candidate_tag" not in final_commands
    assert "gh release create" in final_commands

    inspect_commands = "\n".join(
        str(step.get("run", "")) for step in doc["jobs"]["inspect-image"]["steps"]
    )
    assert "org.opencontainers.image.revision" in inspect_commands
    assert "SOURCE_SHA" in inspect_commands
    assert "manifest unknown" in inspect_commands
    assert "exit 1" in inspect_commands
    assert "inspect-image.outputs.present != 'true'" in doc["jobs"]["build-image"]["if"]


def test_stable_requires_highest_complete_rc_and_never_rebuilds_image() -> None:
    doc = _load("tag-release.yml")
    assert "build-image" not in doc["jobs"]
    resolve = doc["jobs"]["resolve-rc"]
    commands = "\n".join(str(step.get("run", "")) for step in resolve["steps"])
    assert "isPrerelease" in commands
    assert "release-provenance.json" in commands
    assert "image_digest" in commands
    assert "merge-base --is-ancestor" in commands
    assert "pypi.org/pypi/data-olympus" in commands
    assert "sha256" in commands
    assert "REQUESTED_RC" in commands
    assert "time.sleep" in commands
    assert 'git show "$SOURCE_SHA:pyproject.toml"' in commands
    assert "SOURCE_VERSION" in commands
    assert commands.index('git fetch origin "refs/tags/$RC_TAG:refs/tags/$RC_TAG"') < (
        commands.index('git show "$SOURCE_SHA:pyproject.toml"')
    )

    tag = next(
        step
        for step in doc["jobs"]["create-tag"]["steps"]
        if step.get("name") == "Create and push annotated tag"
    )
    assert 'git tag -a "$TAG" "$SOURCE_SHA"' in tag["run"]

    promote = doc["jobs"]["promote-image"]
    promote_commands = "\n".join(str(step.get("run", "")) for step in promote["steps"])
    assert "imagetools create" in promote_commands
    assert ":stable" in promote_commands
    assert ":latest" in promote_commands
    assert "IMAGE_DIGEST" in promote_commands
    assert "docker/build-push-action" not in str(promote)
    assert "manifest unknown" in promote_commands
    assert "VERSION_DIGEST" in promote_commands


def test_stable_compares_same_source_wheels_before_upload() -> None:
    doc = _load("tag-release.yml")
    publish = doc["jobs"]["publish-pypi"]
    assert set(publish["needs"]) >= {"decide", "resolve-rc"}
    commands = "\n".join(str(step.get("run", "")) for step in publish["steps"])
    assert "gh release download" in commands
    assert "scripts/release_artifacts.py stable" in commands
    assert "check_version_free.py" not in commands
    compare_index = commands.index("scripts/release_artifacts.py stable")
    publish_index = next(
        index
        for index, step in enumerate(publish["steps"])
        if "pypa/gh-action-pypi-publish" in str(step.get("uses", ""))
    )
    stable_step_index = next(
        index
        for index, step in enumerate(publish["steps"])
        if "scripts/release_artifacts.py stable" in str(step.get("run", ""))
    )
    assert compare_index >= 0
    assert stable_step_index < publish_index
    release_commands = "\n".join(
        str(step.get("run", "")) for step in doc["jobs"]["release"]["steps"]
    )
    assert "targetCommitish" not in release_commands
    assert "git rev-list -n 1" in release_commands


def test_stable_version_is_derived_from_requested_candidate_not_main() -> None:
    doc = _load("tag-release.yml")
    decide_commands = "\n".join(
        str(step.get("run", "")) for step in doc["jobs"]["decide"]["steps"]
    )
    assert "scripts/should_tag.py" not in decide_commands
    assert "CANDIDATE_TAG" in decide_commands
    assert "-rc." in decide_commands
