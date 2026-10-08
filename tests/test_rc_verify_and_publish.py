"""Offline publication with real Git/artifacts and injected registry clients."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import urllib.error
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import rc_verify_and_publish as stage
from tests.test_sdlc_version import Repo


def digest(data):
    return hashlib.sha256(data).hexdigest()


def tar(path, files):
    with tarfile.open(path, "w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def make_build(tmp_path, branch="release/new"):
    history = tmp_path / "history"
    history.mkdir()
    repo = Repo(history)
    repo.cut()
    if branch == "hotfix/new":
        repo.git("checkout", "-b", "hotfix/new", "v1.4.2")
    repo.commit("fix: corrected export")
    artifacts = tmp_path / "artifacts"
    (artifacts / "dist").mkdir(parents=True)
    version = repo.compute(branch=branch)
    py = version["pypi_version"]
    wheel = artifacts / "dist" / f"data_olympus-{py}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"data_olympus-{py}.dist-info/METADATA",
                         f"Name: data-olympus\nVersion: {py}\n")
    sdist = artifacts / "dist" / f"data_olympus-{py}.tar.gz"
    tar(sdist, {f"data_olympus-{py}/PKG-INFO":
                f"Name: data-olympus\nVersion: {py}\n".encode()})
    config = b'{}'
    manifest = json.dumps({"schemaVersion": 2, "config": {
        "digest": "sha256:" + digest(config), "size": len(config)}, "layers": []}).encode()
    descriptor = {"digest": "sha256:" + digest(manifest), "size": len(manifest)}
    tar(artifacts / "image.oci.tar", {
        "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        "index.json": json.dumps({"schemaVersion": 2, "manifests": [descriptor]}).encode(),
        "blobs/sha256/" + digest(manifest): manifest,
        "blobs/sha256/" + digest(config): config,
    })
    provenance = {**{key: version[key] for key in ("B", "H", "M", "N")},
                  "candidate_tag": version["candidate"], "python_version": py,
                  "source_sha": version["H"], "dry_run": False, "promotable": True,
                  "image_digest": descriptor["digest"],
                  "oci_archive_sha256": digest((artifacts / "image.oci.tar").read_bytes()),
                  "candidate": {"version": py, "source_sha": version["H"],
                                "wheel": wheel.name, "sdist": sdist.name,
                                "wheel_sha256": digest(wheel.read_bytes()),
                                "sdist_sha256": digest(sdist.read_bytes())}}
    (artifacts / "release-provenance.json").write_text(json.dumps(provenance))
    event = {"repository": {"full_name": stage.REPOSITORY}, "workflow_run": {
        "name": "rc-build", "path": stage.BUILD_WORKFLOW,
        "conclusion": "success", "event": "push",
        "head_branch": branch, "head_sha": version["H"],
        "head_repository": {"full_name": stage.REPOSITORY}, "id": 42, "run_attempt": 1}}
    return repo, artifacts, provenance, event


@pytest.fixture
def build(tmp_path):
    return make_build(tmp_path)


def verify(build):
    repo, artifacts, _, event = build
    branch = event["workflow_run"]["head_branch"]
    return stage.verify(artifacts, repo.path, event, main="main", branch_ref=branch)


def mutate(build, key, value):
    build[2][key] = value
    (build[1] / "release-provenance.json").write_text(json.dumps(build[2]))


class Registry:
    def __init__(self):
        self.python = {}
        self.image = None
        self.tag = None
        self.assets = None
        self.writes = []
        self.corrupt_push = False

    def python_hashes(self, _version):
        return self.python.copy()

    def image_digest(self, _reference):
        return self.image

    def tag_head(self, _tag):
        return self.tag

    def release_assets(self, _tag):
        return self.assets

    def reserve(self, verified):
        self.tag = verified.head
        self.assets = verified.assets.copy()
        self.writes.append("reserve")

    def push_image(self, verified):
        self.image = "sha256:" + "0" * 64 if self.corrupt_push else verified.image_digest
        self.writes.append("image")

    def move_channel(self, _verified):
        self.writes.append("rc")


def no_sleep(_seconds):
    return None


def publish(verified, registry, **kwargs):
    return stage.publish(verified, registry, sleep=no_sleep, **kwargs)


def test_verify_happy_path(build):
    checked = verify(build)
    assert checked.head == build[2]["H"]
    assert checked.version == "1.4.3-rc.1"
    assert checked.image_digest == build[2]["image_digest"]


@pytest.mark.parametrize(("key", "value"), [
    ("B", "0" * 40), ("M", "0" * 40), ("H", "0" * 40),
    ("N", 99), ("candidate_tag", "1.4.4-rc.1"), ("python_version", "1.4.4rc1"),
    ("mode", "hotfix"), ("dry_run", True), ("promotable", False),
    ("image_digest", "sha256:" + "0" * 64), ("oci_archive_sha256", "0" * 64),
])
def test_provenance_mismatch(build, key, value):
    mutate(build, key, value)
    with pytest.raises(ValueError):
        verify(build)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_python_hash_mismatch(build, kind):
    (build[1] / "dist" / build[2]["candidate"][kind]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash"):
        verify(build)


@pytest.mark.parametrize(("key", "value"), [
    ("head_branch", "evil\nbranch"), ("event", "workflow_dispatch"),
    ("conclusion", "failure"), ("head_repository", {"full_name": "fork/repo"}),
    ("head_sha", "-malicious"), ("head_branch", "feature/x"), ("head_branch", "main"),
    ("path", ".github/workflows/evil.yml"), ("name", "other"), ("id", 0),
])
def test_admission(build, key, value):
    build[3]["workflow_run"][key] = value
    with pytest.raises(ValueError):
        verify(build)


def test_zero_refused(build):
    build[3]["workflow_run"]["head_sha"] = build[0].git("rev-parse", "main")
    build[0].git("update-ref", "refs/heads/release/new", build[2]["B"])
    with pytest.raises(ValueError, match="N=0"):
        verify(build)


def test_head_moved(build):
    build[0].commit("fix: newer")
    with pytest.raises(ValueError, match="head"):
        verify(build)


def test_base_moved(build):
    build[0].git("branch", "-f", "main", "HEAD")
    with pytest.raises((ValueError, stage.VersionError)):
        verify(build)


def test_happy_publication_and_idempotent_retry(build, tmp_path):
    verified, registry = verify(build), Registry()
    assert stage.reserve(verified, registry) == 2
    assert stage.stage_upload(verified, registry, tmp_path / "upload") == 2
    assert sorted(p.name for p in (tmp_path / "upload").iterdir()) == sorted(
        verified.python_hashes)
    registry.python = verified.python_hashes.copy()
    record = stage.selection(publish(verified, registry), build[0].path)
    assert (record["H"], record["image_digest"], record["selected"]) == (
        verified.head, verified.image_digest, True)
    assert record["image"] == f"{stage.IMAGE}@{verified.image_digest}"
    assert registry.writes == ["reserve", "image", "rc"]
    # A rerun of the same stage-two run reuses every object and writes nothing new.
    assert stage.reserve(verified, registry) == 0
    assert stage.stage_upload(verified, registry, tmp_path / "retry") == 0
    publish(verified, registry)
    assert registry.writes.count("reserve") == registry.writes.count("image") == 1


@pytest.mark.parametrize("surface", ["python", "image", "tag", "assets"])
def test_duplicate_identity_with_different_content(build, surface):
    verified, registry = verify(build), Registry()
    if surface == "python":
        registry.python = {next(iter(verified.python_hashes)): "0" * 64}
    elif surface == "image":
        registry.image = "sha256:" + "0" * 64
    elif surface == "tag":
        registry.tag = "0" * 40
    else:
        registry.tag = verified.head
        registry.assets = {"release-provenance.json": b"other H"}
    with pytest.raises(ValueError, match="collision"):
        stage.reserve(verified, registry)
    assert not registry.writes


def test_rebuilt_artifacts_cannot_reuse_published_identity(build, tmp_path):
    """A rerun of stage one yields new bytes; published PyPI files then collide."""
    verified, registry = verify(build), Registry()
    stage.reserve(verified, registry)
    registry.python = {n: "f" * 64 for n in verified.python_hashes}
    with pytest.raises(ValueError, match="PyPI hash collision"):
        stage.stage_upload(verified, registry, tmp_path / "upload")


def test_partial_retry_uploads_only_missing(build, tmp_path):
    verified, registry = verify(build), Registry()
    stage.reserve(verified, registry)
    name, value = next(iter(verified.python_hashes.items()))
    registry.python[name] = value
    assert stage.reserve(verified, registry) == 1
    assert stage.stage_upload(verified, registry, tmp_path / "retry") == 1
    assert not (tmp_path / "retry" / name).exists()


def test_stage_upload_requires_reservation(build, tmp_path):
    verified, registry = verify(build), Registry()
    with pytest.raises(ValueError, match="reservation"):
        stage.stage_upload(verified, registry, tmp_path / "upload")
    assert not (tmp_path / "upload").exists()


def test_existing_bytes_without_head_proof_refused(build):
    verified, registry = verify(build), Registry()
    registry.python = verified.python_hashes.copy()
    with pytest.raises(ValueError, match="same-H"):
        stage.reserve(verified, registry)
    assert not registry.writes


def test_remote_digest_mismatch(build):
    verified, registry = verify(build), Registry()
    stage.reserve(verified, registry)
    registry.python = verified.python_hashes.copy()
    registry.corrupt_push = True
    with pytest.raises(ValueError, match="digest"):
        publish(verified, registry)
    assert "rc" not in registry.writes


def test_incomplete_python_blocks_image(build):
    verified, registry = verify(build), Registry()
    stage.reserve(verified, registry)
    sleeps = []
    with pytest.raises(ValueError, match="PyPI publication is incomplete"):
        stage.publish(verified, registry, sleep=sleeps.append, attempts=3, delay=2.0)
    assert sleeps == [2.0, 2.0]
    assert registry.writes == ["reserve"]


def test_pypi_readback_is_polled_until_consistent(build):
    verified, registry = verify(build), Registry()
    stage.reserve(verified, registry)
    names = sorted(verified.python_hashes)
    views = [{}, {names[0]: verified.python_hashes[names[0]]}, verified.python_hashes.copy()]
    registry.python_hashes = lambda _version: views.pop(0) if len(views) > 1 else views[0]
    publish(verified, registry)
    assert registry.writes == ["reserve", "image", "rc"]


def test_rc_channel_and_selection_for_hotfix(build):
    repo = build[0]
    verified, registry = verify(build), Registry()
    stage.reserve(verified, registry)
    registry.python = verified.python_hashes.copy()
    # An open hotfix wins staging; a release/new candidate is not selected.
    repo.git("update-ref", "refs/heads/hotfix/new", repo.git("rev-parse", "main"))
    record = stage.selection(publish(verified, registry), repo.path)
    assert (record["selected"], record["selection_branch"]) == (False, "hotfix/new")
    assert "rc" in registry.writes
    hotfix = replace(verified, branch="hotfix/new", head=repo.git("rev-parse", "main"))
    registry = Registry()
    stage.reserve(hotfix, registry)
    registry.python = hotfix.python_hashes.copy()
    record = stage.selection(publish(hotfix, registry), repo.path)
    assert (record["selected"], record["selection_branch"]) == (True, "hotfix/new")
    assert registry.writes == ["reserve", "image"]
    repo.git("update-ref", "refs/heads/hotfix/new", verified.head)
    with pytest.raises(ValueError, match="hotfix head moved"):
        stage.selection(publish(hotfix, registry), repo.path)


def test_hotfix_candidate_end_to_end(tmp_path):
    hotfix = make_build(tmp_path, "hotfix/new")
    verified, registry = verify(hotfix), Registry()
    assert (verified.version, verified.python_version) == ("1.4.3-hotfix.rc.1", "1.4.3.dev1")
    assert stage.reserve(verified, registry) == 2
    registry.python = verified.python_hashes.copy()
    record = stage.selection(publish(verified, registry), hotfix[0].path)
    assert (record["selected"], record["branch"]) == (True, "hotfix/new")
    assert "rc" not in registry.writes


def test_hotfix_provenance_cannot_claim_release_identity(tmp_path):
    hotfix = make_build(tmp_path, "hotfix/new")
    mutate(hotfix, "candidate_tag", "1.4.3-rc.1")
    with pytest.raises(ValueError, match="candidate_tag"):
        verify(hotfix)


def test_wrong_metadata_even_with_matching_hash(build):
    path = build[1] / "dist" / build[2]["candidate"]["wheel"]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("data_olympus-1.4.3rc1.dist-info/METADATA",
                         "Name: data-olympus\nVersion: 9.9.9\n")
    build[2]["candidate"]["wheel_sha256"] = digest(path.read_bytes())
    mutate(build, "candidate", build[2]["candidate"])
    with pytest.raises(ValueError, match="metadata"):
        verify(build)


@pytest.mark.parametrize("name", ["../../pyproject.toml", None, 7])
def test_no_path_escape(build, name):
    build[2]["candidate"]["wheel"] = name
    mutate(build, "candidate", build[2]["candidate"])
    with pytest.raises(ValueError, match="unsafe"):
        verify(build)


def test_extra_distribution_file_refused(build):
    (build[1] / "dist" / "evil-0.0.0-py3-none-any.whl").write_bytes(b"x")
    with pytest.raises(ValueError, match="unexpected distribution"):
        verify(build)


def test_non_object_provenance_refused(build):
    (build[1] / "release-provenance.json").write_text("[]")
    with pytest.raises(ValueError, match="candidate receipt"):
        verify(build)


def test_oci_blob_corruption_with_matching_archive_hash(build):
    archive = build[1] / "image.oci.tar"
    with tarfile.open(archive) as stream:
        contents = {m.name: stream.extractfile(m).read() for m in stream if m.isfile()}
    contents["blobs/sha256/" + digest(b'{}')] = b'[]'
    tar(archive, contents)
    mutate(build, "oci_archive_sha256", digest(archive.read_bytes()))
    with pytest.raises(ValueError, match="blob hash"):
        verify(build)


def test_oci_unexpected_entry_refused(build):
    archive = build[1] / "image.oci.tar"
    with tarfile.open(archive) as stream:
        contents = {m.name: stream.extractfile(m).read() for m in stream if m.isfile()}
    contents["run.sh"] = b"#!/bin/sh\n"
    tar(archive, contents)
    mutate(build, "oci_archive_sha256", digest(archive.read_bytes()))
    with pytest.raises(ValueError, match="unexpected OCI entry"):
        verify(build)


@pytest.mark.parametrize(("env", "message"), [
    ({}, "disabled"),
    ({"SDLC_PIPELINE": "enabled", "GITHUB_EVENT_NAME": "push"}, "workflow_run"),
    ({"SDLC_PIPELINE": "enabled", "GITHUB_EVENT_NAME": "workflow_run",
      "GITHUB_REF": "refs/heads/release/new"}, "main"),
])
def test_cli_gates_never_contact_anything(monkeypatch, tmp_path, capsys, env, message):
    for key in ("SDLC_PIPELINE", "GITHUB_EVENT_NAME", "GITHUB_REF"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(stage, "command", lambda *_a, **_kw: pytest.fail("external call"))
    assert stage.main(["reserve", "--artifacts", str(tmp_path),
                       "--history", str(tmp_path / "history")]) == 1
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("diagnostic", [b"HTTP 403", b"network failed", b"not found"])
def test_registry_errors_are_not_absence(monkeypatch, diagnostic):
    monkeypatch.setenv("GITHUB_ACTOR", "machine")
    monkeypatch.setenv("GH_TOKEN", "fake")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=1, stdout=b"", stderr=diagnostic))
    with pytest.raises(ValueError):
        stage.Registries().image_digest("1.4.3-rc.1")
    with pytest.raises(ValueError):
        stage.Registries().tag_head("1.4.3-rc.1")


def test_pypi_outage_is_not_absence(monkeypatch):
    def outage(url, **_kwargs):
        raise urllib.error.HTTPError(url, 503, "unavailable", {}, None)

    monkeypatch.setattr(stage.urllib.request, "urlopen", outage)
    with pytest.raises(ValueError, match="unavailable"):
        stage.Registries().python_hashes("1.4.3rc1")


def test_production_clients_with_fake_gh_skopeo_and_pypi(build, tmp_path, monkeypatch):
    verified = verify(build)
    state = {"tag": None, "release": False, "assets": {}, "python": {}, "images": {}}
    writes = []
    token = "fake-token-value"
    with tarfile.open(verified.archive) as archive:
        manifest = archive.extractfile("blobs/sha256/" + verified.image_digest[7:]).read()

    def response(value=None, error=None):
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        return SimpleNamespace(returncode=int(error is not None), stdout=data,
                               stderr=error or b"")

    def fake_run(args, **kwargs):
        assert all(token not in arg for arg in args), "token leaked into argv"
        if args[:2] == ("skopeo", "login"):
            assert "--password-stdin" in args and kwargs["input"] == token.encode()
            return response(b"")
        if args[:2] == ("gh", "api"):
            endpoint = args[2].removeprefix(f"repos/{stage.REPOSITORY}/")
            payload = json.loads(kwargs["input"]) if kwargs.get("input") else None
            if endpoint == "git/tags" and payload:
                assert payload["object"] == verified.head and payload["type"] == "commit"
                writes.append("tag-object")
                return response({"sha": "a" * 40})
            if endpoint == "git/refs" and payload:
                state["tag"] = verified.head
                writes.append("tag-ref")
                return response({})
            if endpoint.startswith("git/ref/tags/"):
                return (response({"object": {"type": "tag", "sha": "a" * 40}})
                        if state["tag"] else response(error=b"gh: Not Found (HTTP 404)"))
            if endpoint.startswith("git/tags/"):
                return response({"object": {"type": "commit", "sha": state["tag"]}})
            if endpoint.startswith("releases/tags/"):
                return (response({"prerelease": True, "draft": False,
                                  "assets": [{"name": n} for n in state["assets"]]})
                        if state["release"] else response(error=b"gh: Not Found (HTTP 404)"))
        if args[:3] == ("gh", "release", "create"):
            assert "--verify-tag" in args and "--prerelease" in args
            state["release"] = True
            writes.append("release")
            return response(b"")
        if args[:3] == ("gh", "release", "upload"):
            path = Path(args[4])
            assert path.name not in state["assets"] and "--clobber" not in args
            state["assets"][path.name] = path.read_bytes()
            writes.append("asset")
            return response(b"")
        if args[:3] == ("gh", "release", "download"):
            return response(state["assets"][args[args.index("--pattern") + 1]])
        if args[:2] == ("skopeo", "inspect"):
            assert "--authfile" in args
            tag = args[-1].rsplit(":", 1)[1]
            return (response(manifest) if tag in state["images"]
                    else response(error=b"manifest unknown"))
        if args[:2] == ("skopeo", "copy"):
            assert "--all" in args and "--preserve-digests" in args and "--authfile" in args
            if args[-1].endswith(":rc"):
                assert args[-2] == f"docker://{stage.IMAGE}@{verified.image_digest}"
            else:
                assert args[-2].startswith("oci-archive:")
            state["images"][args[-1].rsplit(":", 1)[1]] = manifest
            writes.append("image")
            return response(b"")
        pytest.fail(f"unexpected adapter call {args[:3]}")

    def fake_pypi(url, **_kwargs):
        assert url.endswith("/1.4.3rc1/json")
        if not state["python"]:
            raise urllib.error.HTTPError(url, 404, "missing", {}, None)
        return io.BytesIO(json.dumps({"urls": [
            {"filename": n, "digests": {"sha256": h}} for n, h in state["python"].items()
        ]}).encode())

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(stage.urllib.request, "urlopen", fake_pypi)
    monkeypatch.setenv("GITHUB_ACTOR", "machine")
    monkeypatch.setenv("GH_TOKEN", token)
    client = stage.Registries()
    try:
        assert stage.reserve(verified, client) == 2
        assert writes == ["tag-object", "tag-ref", "release", "asset", "asset", "asset"]
        assert stage.stage_upload(verified, client, tmp_path / "first") == 2
        state["python"] = verified.python_hashes.copy()
        publish(verified, client)
        assert state["images"].keys() == {verified.version, "rc"}
        first_writes = writes.copy()
        assert stage.reserve(verified, client) == 0
        assert stage.stage_upload(verified, client, tmp_path / "retry") == 0
        publish(verified, client)
        assert writes == first_writes
    finally:
        client.close()


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/rc-publish-stage.yml"


def test_workflow_security_contract():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert workflow.get("on", workflow.get(True)) == {
        "workflow_run": {"workflows": ["rc-build"], "types": ["completed"]}}
    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {
        "group": "data-olympus-promotion", "cancel-in-progress": False}
    jobs = workflow["jobs"]
    assert set(jobs) == {"reserve", "pypi", "publish"}
    for gate in ("vars.SDLC_PIPELINE == 'enabled'", "conclusion == 'success'",
                 "head_branch == 'release/new'", "head_branch == 'hotfix/new'",
                 "event == 'push'", "head_repository.full_name == github.repository"):
        assert gate in jobs["reserve"]["if"]
    assert jobs["pypi"]["needs"] == "reserve"
    assert jobs["publish"]["needs"] == ["reserve", "pypi"]
    assert "needs.reserve.result == 'success'" in jobs["publish"]["if"]
    assert jobs["reserve"]["permissions"] == {
        "actions": "read", "contents": "write", "packages": "read"}
    assert jobs["pypi"]["permissions"] == {
        "actions": "read", "contents": "read", "packages": "read", "id-token": "write"}
    assert jobs["publish"]["permissions"] == {
        "actions": "read", "attestations": "write", "contents": "read",
        "id-token": "write", "packages": "write"}
    assert [n for n, j in jobs.items() if "attestations" in j["permissions"]] == ["publish"]
    assert [n for n, j in jobs.items() if "id-token" in j["permissions"]] == ["pypi", "publish"]
    assert [name for name, job in jobs.items() if "environment" in job] == ["pypi"]
    assert jobs["pypi"]["environment"] == "pypi-rc"
    text = WORKFLOW.read_text()
    assert "secrets." not in text and "pull_request" not in text
    for name, job in jobs.items():
        steps = job["steps"]
        checkout = steps[0]
        assert checkout["uses"].startswith("actions/checkout")
        assert checkout["with"] == {"ref": "${{ github.sha }}", "persist-credentials": False}
        download = next(s for s in steps if s.get("uses", "").startswith(
            "actions/download-artifact"))
        assert download["with"]["run-id"] == "${{ github.event.workflow_run.id }}"
        assert download["with"]["name"] == (
            "rc-build-${{ github.event.workflow_run.head_sha }}-"
            "${{ github.event.workflow_run.run_attempt }}")
        for step in steps:
            assert "${{" not in step.get("run", ""), (name, step.get("name"))
            if "scripts/rc_verify_and_publish.py" in step.get("run", ""):
                assert step["env"]["SDLC_PIPELINE"] == "${{ vars.SDLC_PIPELINE }}"
                assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
                assert "uv run --no-project python" in step["run"]
        uses = [s["uses"] for s in steps if "uses" in s]
        assert any(u.startswith("pypa/") for u in uses) == (name == "pypi")
        assert any(u.startswith("actions/attest") for u in uses) == (name == "publish")
    publish_step = next(s for s in jobs["pypi"]["steps"]
                        if s.get("uses", "").startswith("pypa/gh-action-pypi-publish"))
    assert set(publish_step["with"]) == {"packages-dir"}


def test_attestations_follow_verified_publication():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["publish"]["steps"]
    names = [s.get("id") or s.get("uses", "").split("@")[0] for s in steps]
    publish_at = names.index("publish")
    attest = [i for i, s in enumerate(steps)
              if s.get("uses", "").startswith("actions/attest-build-provenance@")]
    assert len(attest) == 2 and min(attest) > publish_at
    image, files = (steps[i]["with"] for i in attest)
    assert image == {"subject-name": stage.IMAGE,
                     "subject-digest": "${{ steps.publish.outputs.image_digest }}",
                     "push-to-registry": True}
    assert files == {"subject-path": "to-delete/rc-input/dist/*.whl\n"
                                     "to-delete/rc-input/dist/*.tar.gz\n"}


def test_secrets_live_only_in_declared_environments():
    """Bot or App credentials must be environment secrets on main-only environments."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert "secrets" not in json.dumps(workflow.get("env", {}))
    for name, job in workflow["jobs"].items():
        if "secrets." in json.dumps(job):
            assert job.get("environment") in ("pypi-rc",), name


def test_publish_cli_emits_validated_digest(build, tmp_path, monkeypatch):
    verified = verify(build)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(build[3]))
    output, summary = tmp_path / "output", tmp_path / "summary"
    for key, value in {"SDLC_PIPELINE": "enabled", "GITHUB_EVENT_NAME": "workflow_run",
                       "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_PATH": str(event_path),
                       "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)
                       }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(stage, "fetch_history", lambda *_a: None)
    monkeypatch.setattr(stage, "verify", lambda *_a, **_kw: verified)
    monkeypatch.setattr(stage, "publish", lambda *_a, **_kw: {"H": verified.head,
                                                                "branch": verified.branch})
    monkeypatch.setattr(stage, "selection", lambda record, _h: record | {"selected": True})
    selection = tmp_path / "out" / "staging-selection.json"
    assert stage.main(["publish", "--artifacts", str(build[1]), "--history",
                       str(build[0].path), "--selection", str(selection)]) == 0
    assert output.read_text() == f"image_digest={verified.image_digest}\n"
    assert json.loads(selection.read_text())["selected"] is True
    assert verified.head in summary.read_text()
