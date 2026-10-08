"""Security and sequencing contract for the disabled promotion workflow."""

from pathlib import Path

import yaml


def workflow():
    return yaml.safe_load(Path(".github/workflows/promote-release.yml").read_text())


def scripts(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_dispatch_gate_and_shared_lock():
    data = workflow()
    assert set(data.get("on", data.get(True))) == {"workflow_dispatch"}
    assert data["concurrency"] == {
        "group": "data-olympus-promotion", "cancel-in-progress": False,
    }
    assert data["permissions"] == {"contents": "read"}
    for job in data["jobs"].values():
        assert "vars.SDLC_PIPELINE == 'enabled'" in job["if"]
        assert "github.ref == 'refs/heads/main'" in job["if"]
        assert "${{" not in scripts(job)
        assert "kubectl" not in scripts(job)


def test_build_is_unprivileged_and_compares_both_distributions():
    job = workflow()["jobs"]["build-stable"]
    assert job["permissions"] == {"contents": "read"}
    text = scripts(job)
    assert "stable-promotion" in text
    assert "--candidate-sdist" in text
    assert "--candidate-wheel" in text
    assert "smoke_installed_wheel.py" in text
    assert "secrets." not in str(job)
    assert all(not step.get("with", {}).get("persist-credentials", False)
               for step in job["steps"] if "checkout@" in step.get("uses", ""))


def test_proof_and_protected_publication_recheck_current_state():
    jobs = workflow()["jobs"]
    for name in ("prove", "publish-pypi", "create-tag"):
        text = scripts(jobs[name])
        assert "git fetch" in text
        assert "scripts/release_record.py prove" in text
    assert jobs["publish-pypi"]["environment"]["name"] == "pypi"
    assert jobs["publish-pypi"]["permissions"]["id-token"] == "write"
    assert "build-stable" in jobs["publish-pypi"]["needs"]
    assert "publish-pypi" in jobs["create-tag"]["needs"]
    assert "git tag -a" in scripts(jobs["create-tag"])
    assert "create-github-app-token@" in str(jobs["create-tag"])
    assert "SDLC_APP_PRIVATE_KEY" in str(jobs["create-tag"])
    assert "|| secrets." not in str(jobs)


def test_channels_reuse_digest_and_mcp_is_last():
    jobs = workflow()["jobs"]
    text = scripts(jobs["promote-image"])
    assert '--tag "$REPO:$TAG"' in text
    assert '--tag "$REPO:stable" --tag "$REPO:latest"' in text
    assert '"$REPO@$IMAGE_DIGEST"' in text
    assert "buildx build" not in text
    assert "create-tag" in jobs["promote-image"]["needs"]
    registry = jobs["publish-mcp-registry"]
    assert "release" in registry["needs"]
    assert registry["permissions"]["id-token"] == "write"
    assert "login github-oidc" in scripts(registry)
    assert "sha256sum -c" in scripts(registry)
    assert "python" in scripts(registry)


def test_security_gate_uses_machine_app_alert_permissions():
    for name in ("prove", "publish-pypi"):
        job = workflow()["jobs"][name]
        app = next(step for step in job["steps"]
                   if "create-github-app-token@" in step.get("uses", ""))
        assert app["with"]["permission-vulnerability-alerts"] == "read"
        assert app["with"]["permission-security-events"] == "read"
        gate = next(step for step in job["steps"]
                    if "scripts/security_alerts.py" in step.get("run", ""))
        assert gate["env"]["GH_TOKEN"] == "${{ steps.security-app.outputs.token }}"


def test_initial_proof_is_strict_and_later_rechecks_are_resumable():
    jobs = workflow()["jobs"]
    prove = scripts(jobs["prove"])
    assert "--phase" not in prove  # Default initial phase: main head is S, tag absent.
    assert 'test "$SQUASH" = "$GITHUB_SHA"' in prove
    assert "scripts/version_free.py" in prove
    for name in ("publish-pypi", "create-tag"):
        text = scripts(jobs[name])
        assert "prove --phase resume" in text
        assert "cmp to-delete/promotion/release-record.json" in text
        assert "version_free.py" not in text


def test_publication_retries_never_replace_published_bytes():
    jobs = workflow()["jobs"]
    publish = next(step for step in jobs["publish-pypi"]["steps"]
                   if "gh-action-pypi-publish@" in step.get("uses", ""))
    assert publish["with"]["skip-existing"] is True
    assert "local == remote" in scripts(jobs["publish-pypi"])
    release = scripts(jobs["release"])
    assert "scripts/release_upload.py" in release
    assert "--verify-tag" in release
    assert "git push --force" not in scripts(jobs["create-tag"])
    assert "tag -f" not in scripts(jobs["create-tag"])


def test_rc_image_labels_are_checked_before_promotion():
    text = scripts(workflow()["jobs"]["prove"])
    assert "org.opencontainers.image.version" in text
    assert "org.opencontainers.image.revision" in text
    assert "$REVIEWED_HEAD" in text


def test_no_workflow_starts_on_tag_push():
    """An App-token tag push starts workflows, unlike GITHUB_TOKEN (R9)."""
    for path in Path(".github/workflows").glob("*.y*ml"):
        data = yaml.safe_load(path.read_text())
        triggers = data.get("on", data.get(True))
        if isinstance(triggers, dict) and isinstance(triggers.get("push"), dict):
            assert "tags" not in triggers["push"], path
            assert "tags-ignore" not in triggers["push"], path
