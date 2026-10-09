"""Security and sequencing contract for the disabled promotion workflow."""

import re
from pathlib import Path

import yaml


def workflow():
    return yaml.safe_load(Path(".github/workflows/promote-release.yml").read_text())


def scripts(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_dispatch_gate_and_shared_lock():
    data = workflow()
    assert set(data.get("on", data.get(True))) == {"workflow_dispatch"}
    # Only admitted runs take the shared lock (tests/test_promotion_concurrency.py
    # evaluates the expression); a refused dispatch gets a run-scoped group.
    assert data["concurrency"]["cancel-in-progress"] is False
    assert "'data-olympus-promotion'" in data["concurrency"]["group"]
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
    assert "scripts/release_record.py release" in release
    assert "--stable-provenance" in release
    assert "--verify-tag" in release
    assert "--clobber" not in release
    # Immutable releases: the release is created as a draft with no assets and
    # published by release_record.py only after every asset is verified.
    create = next(line for line in release.splitlines() if "gh release create" in line)
    assert "--draft" in create
    create_command = re.search(r"gh release create(.*?)\n\s*fi\n", release, re.S)
    assert create_command and "ASSETS" not in create_command.group(1)
    assert "--draft=false" not in release
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


PERMISSIONS = {
    "prove": {"contents": "read", "checks": "read", "packages": "read",
              "attestations": "read"},
    "build-stable": {"contents": "read"},
    "publish-pypi": {"contents": "read", "id-token": "write"},
    "create-tag": {"contents": "read"},
    "promote-image": {"contents": "read", "packages": "write"},
    "release": {"contents": "write"},
    "publish-mcp-registry": {"contents": "read", "id-token": "write"},
}


def test_job_permissions_are_exactly_the_reviewed_table():
    jobs = workflow()["jobs"]
    assert {name: job["permissions"] for name, job in jobs.items()} == PERMISSIONS
    assert {name for name, job in jobs.items() if "id-token" in job["permissions"]} == {
        "publish-pypi", "publish-mcp-registry",
    }


def test_stable_tag_is_pushed_with_the_app_token_only():
    """R6: no GITHUB_TOKEN or human token may push the stable tag."""
    job = workflow()["jobs"]["create-tag"]
    app = next(step for step in job["steps"]
               if "create-github-app-token@" in step.get("uses", ""))
    assert app["id"] == "app"
    assert app["with"]["permission-contents"] == "write"
    checkout = next(step for step in job["steps"] if "checkout@" in step.get("uses", ""))
    assert checkout["with"]["token"] == "${{ steps.app.outputs.token }}"
    assert checkout["with"].get("persist-credentials", True) is True
    assert job["steps"].index(app) < job["steps"].index(checkout)
    assert "github.token" not in str(job)
    assert "GITHUB_TOKEN" not in str(job)
    assert "secrets.GH_" not in str(job)
    text = scripts(job)
    assert 'git push origin "refs/tags/$TAG:refs/tags/$TAG"' in text
    assert "git tag -a --cleanup=verbatim" in text
    assert 'git config user.name "$BOT_NAME"' in text
    assert "github-actions[bot]" not in text


def test_existing_tag_is_verified_as_the_apps_own_tag():
    jobs = workflow()["jobs"]
    for name in ("publish-pypi", "create-tag"):
        text = scripts(jobs[name])
        assert '--tagger "$BOT_NAME <$BOT_EMAIL>"' in text
        identity = next(step for step in jobs[name]["steps"]
                        if step.get("name") == "Resolve the release App bot identity")
        token = identity["env"]["GH_TOKEN"]
        assert token in ("${{ steps.app.outputs.token }}",
                         "${{ steps.security-app.outputs.token }}")
        assert "[[ \"$BOT_ID\" =~ ^[1-9][0-9]*$ ]]" in identity["run"]
    create = scripts(jobs["create-tag"])
    push = create.index("git push origin")
    assert "recheck" in create[push:]


def test_app_key_is_only_an_environment_secret_restricted_to_main_jobs():
    """The App key and OIDC publication live only in main-gated environments."""
    jobs = workflow()["jobs"]
    environments = {name: job.get("environment") for name, job in jobs.items()}
    assert environments["prove"] == "sdlc-bot"
    assert environments["create-tag"] == "sdlc-bot"
    assert environments["publish-pypi"]["name"] == "pypi"
    for name, job in jobs.items():
        if "secrets." in str(job):
            assert environments[name] in ("sdlc-bot", {
                "name": "pypi", "url": "https://pypi.org/p/data-olympus",
            }), name
            assert "secrets.SDLC_APP_PRIVATE_KEY" in str(job)
            assert str(job).count("secrets.") == 1, name
    assert environments["build-stable"] is None
    assert environments["promote-image"] is None


def test_both_digest_checks_are_present():
    jobs = workflow()["jobs"]
    prove = scripts(jobs["prove"])
    assert ('test "$(docker buildx imagetools inspect "$REPO:$RC_TAG" '
            "--format '{{.Manifest.Digest}}')\" = \"$IMAGE_DIGEST\"") in prove
    image = scripts(jobs["promote-image"])
    assert 'for channel in "$TAG" stable latest; do' in image
    assert ('test "$(docker buildx imagetools inspect "$REPO:$channel" '
            "--format '{{.Manifest.Digest}}')\" = \"$IMAGE_DIGEST\"") in image
    assert image.index("imagetools create --tag \"$REPO:stable\"") < image.index(
        'for channel in "$TAG" stable latest; do')


PINNED = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv": "37802adc94f370d6bfd71619e3f0bf239e1f3b78",
    "actions/create-github-app-token": "fee1f7d63c2ff003460e3d139729b119787bc349",
    "actions/attest-build-provenance": "977bb373ede98d70efdf65b84cb5f73e068dcc2a",
    "pypa/gh-action-pypi-publish": "dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
}


def test_privileged_actions_are_pinned_by_full_commit_sha():
    seen = set()
    for job in workflow()["jobs"].values():
        for step in job["steps"]:
            action, _, ref = step.get("uses", "").partition("@")
            if action in PINNED:
                seen.add(action)
                assert ref == PINNED[action], action
    assert {"actions/checkout", "astral-sh/setup-uv", "actions/create-github-app-token",
            "pypa/gh-action-pypi-publish"} <= seen


SIGNER = "knaisoma/data-olympus/.github/workflows/rc-publish-stage.yml"


def step_named(job, prefix):
    return next(step for step in job["steps"] if step.get("name", "").startswith(prefix))


def test_attestations_are_verified_in_prove_before_any_publication():
    jobs = workflow()["jobs"]
    prove = jobs["prove"]
    step = step_named(prove, "Verify stage-two attestations")
    text = step["run"]
    assert "${{" not in text
    assert step["env"] == {"IMAGE_DIGEST": "${{ steps.proof.outputs.image_digest }}"}
    command = re.search(r"verify_attestation\(\) \{(.*?)\n\s*\}", text, re.S).group(1)
    flags = " ".join(command.replace("\\\n", " ").split())
    assert flags == ('gh attestation verify "$1" --repo knaisoma/data-olympus '
                     f"--signer-workflow {SIGNER} --source-ref refs/heads/main "
                     "--deny-self-hosted-runners")
    calls = [line.strip() for line in text.splitlines()
             if line.strip().startswith("verify_attestation ")]
    assert calls == ['verify_attestation "${WHEELS[0]}"',
                     'verify_attestation "${SDISTS[0]}"',
                     'verify_attestation "oci://ghcr.io/knaisoma/data-olympus@$IMAGE_DIGEST"']
    assert "WHEELS=(to-delete/promotion/candidate/*.whl)" in text
    assert "SDISTS=(to-delete/promotion/candidate/*.tar.gz)" in text
    assert 'test "${#WHEELS[@]}" = 1' in text and 'test "${#SDISTS[@]}" = 1' in text
    assert "[[ \"$IMAGE_DIGEST\" =~ ^sha256:[0-9a-f]{64}$ ]]" in text
    assert text.startswith("set -euo pipefail")
    # The exact files verified against provenance and PyPI, and the registry
    # login, come first; the promotion inputs are published to later jobs after.
    steps = prove["steps"]
    order = [steps.index(s) for s in (
        next(s for s in steps if "docker/login-action@" in s.get("uses", "")),
        step_named(prove, "Verify candidate publication"),
        step,
        next(s for s in steps if "upload-artifact@" in s.get("uses", "")),
    )]
    assert order == sorted(order)
    assert prove["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert prove["permissions"]["attestations"] == "read"
    # gh refuses --cert-identity together with --signer-workflow.
    assert "--cert-identity" not in text
    # Every publishing job depends directly on prove.
    for name, job in jobs.items():
        if name != "prove":
            needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
            assert "prove" in needs, name
    for name, job in jobs.items():
        if name != "prove":
            assert "gh attestation verify" not in scripts(job), name


def test_codeql_is_required_on_h_and_analyses_on_s():
    step = step_named(workflow()["jobs"]["prove"],
                      "Require exact-source CI and CodeQL on H and CI and analyses on S")
    text = step["run"]
    assert step["env"] == {"REQUIRED_ANALYSES": "${{ vars.SDLC_REQUIRED_CODEQL_CHECKS }}"}
    assert 'test -n "$REQUIRED_ANALYSES"' in text
    assert text.index('test -n "$REQUIRED_ANALYSES"') < text.index("gh api")
    assert 'gh api --paginate --slurp "repos/$GITHUB_REPOSITORY/commits/$2/check-runs"' in text
    assert "python scripts/release_record.py checks --role \"$1\" --sha \"$2\"" in text
    assert '--analyses "$REQUIRED_ANALYSES"' in text
    assert 'gate head "$REVIEWED_HEAD"' in text
    assert 'gate squash "$SQUASH"' in text
    assert "python - <<" not in text
