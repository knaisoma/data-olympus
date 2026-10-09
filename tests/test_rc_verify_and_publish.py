"""Offline publication with real Git/artifacts and injected registry clients."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
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
    provenance, event = write_artifacts(tmp_path / "artifacts", repo.compute(branch=branch),
                                        branch)
    return repo, tmp_path / "artifacts", provenance, event


def write_artifacts(artifacts, version, branch="release/new"):
    """Write stage-one artifacts and the rc-build event for an engine result."""
    (artifacts / "dist").mkdir(parents=True)
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
    return provenance, event


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
        self.tag_text = None
        self.assets = None
        self.draft = True
        # Production read-only jobs (contents: read) cannot list draft releases.
        self.hide_drafts = False
        self.writes = []
        self.corrupt_push = False

    def python_hashes(self, _version):
        return self.python.copy()

    def image_digest(self, _reference):
        return self.image

    def tag_head(self, _tag):
        return self.tag

    def tag_message(self, _tag):
        return self.tag_text if self.tag is not None else None

    def release(self, _tag):
        if self.assets is None or (self.draft and self.hide_drafts):
            return None
        return stage.Release(self.draft, dict(self.assets))

    def reserve(self, verified):
        assert self.draft, "assets uploaded to a published (immutable) release"
        if self.tag is None:
            bind_tag(self, verified)
        self.assets = dict(self.assets or {})
        for name, data in verified.assets.items():
            self.assets.setdefault(name, data)
        self.writes.append("reserve")

    def publish_release(self, _verified):
        assert self.draft
        self.draft = False
        self.writes.append("finalize")

    def push_image(self, verified):
        self.image = "sha256:" + "0" * 64 if self.corrupt_push else verified.image_digest
        self.writes.append("image")

    def move_channel(self, _verified):
        self.writes.append("rc")


def bind_tag(registry, verified):
    """The annotated tag a correct reserve writes: H plus every asset hash."""
    registry.tag, registry.tag_text = verified.head, stage.tag_message(verified)


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
    record = stage.selection(publish(verified, registry, move_rc=True), build[0].path)
    assert (record["H"], record["image_digest"], record["selected"]) == (
        verified.head, verified.image_digest, True)
    assert record["image"] == f"{stage.IMAGE}@{verified.image_digest}"
    assert record["rc_channel_moved"] is True
    assert registry.writes == ["reserve", "image", "rc"]
    assert registry.draft is True
    stage.finalize(verified, registry)
    assert registry.writes == ["reserve", "image", "rc", "finalize"]
    assert registry.draft is False
    # A rerun of the same stage-two run reuses every object and writes nothing new.
    assert stage.reserve(verified, registry) == 0
    assert stage.stage_upload(verified, registry, tmp_path / "retry") == 0
    publish(verified, registry)
    stage.finalize(verified, registry)
    assert registry.writes == ["reserve", "image", "rc", "finalize"]


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
        bind_tag(registry, verified)
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
    assert registry.writes == ["reserve", "image"]


def test_rc_channel_and_selection_for_hotfix(build):
    repo = build[0]
    verified, registry = verify(build), Registry()
    stage.reserve(verified, registry)
    registry.python = verified.python_hashes.copy()
    # An open hotfix wins staging; a release/new candidate is not selected.
    repo.git("update-ref", "refs/heads/hotfix/new", repo.git("rev-parse", "main"))
    record = stage.selection(publish(verified, registry, move_rc=True), repo.path)
    assert (record["selected"], record["selection_branch"]) == (False, "hotfix/new")
    assert "rc" in registry.writes
    hotfix = replace(verified, branch="hotfix/new", head=repo.git("rev-parse", "main"))
    registry = Registry()
    stage.reserve(hotfix, registry)
    registry.python = hotfix.python_hashes.copy()
    record = stage.selection(publish(hotfix, registry, move_rc=True), repo.path)
    assert (record["selected"], record["selection_branch"]) == (True, "hotfix/new")
    assert registry.writes == ["reserve", "image"]
    assert record["rc_channel_moved"] is False
    repo.git("update-ref", "refs/heads/hotfix/new", verified.head)
    with pytest.raises(ValueError, match="hotfix head moved"):
        stage.selection(publish(hotfix, registry), repo.path)


def test_hotfix_candidate_end_to_end(tmp_path):
    hotfix = make_build(tmp_path, "hotfix/new")
    verified, registry = verify(hotfix), Registry()
    assert (verified.version, verified.python_version) == ("1.4.3-hotfix.rc.1", "1.4.3.dev1")
    assert stage.reserve(verified, registry) == 2
    registry.python = verified.python_hashes.copy()
    record = stage.selection(publish(verified, registry, move_rc=True), hotfix[0].path)
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


def test_rc_channel_is_not_moved_by_default(build):
    """W6 invariant: rc stays with set-channel.yml until SDLC_RC_CHANNEL is enabled."""
    verified, registry = verify(build), Registry()
    stage.reserve(verified, registry)
    registry.python = verified.python_hashes.copy()
    record = publish(verified, registry)
    assert registry.writes == ["reserve", "image"]
    assert record["rc_channel_moved"] is False


@pytest.mark.parametrize(("key", "value"), [
    ("dry_run", 0), ("promotable", 1), ("N", 1.0), ("N", True),
])
def test_provenance_types_are_strict(build, key, value):
    """M05: values that compare equal but have another JSON type are refused."""
    if key == "N":
        assert build[2]["N"] == 1
    mutate(build, key, value)
    with pytest.raises(ValueError, match=f"recompute mismatch: {key}"):
        verify(build)


@pytest.mark.parametrize(("key", "value"), [
    ("source_sha", "0" * 40), ("version", "9.9.9rc1"), ("source_sha", None),
])
def test_candidate_receipt_mismatch(build, key, value):
    """M11: the stage-one receipt must name H and the computed Python version."""
    build[2]["candidate"][key] = value
    mutate(build, "candidate", build[2]["candidate"])
    with pytest.raises(ValueError, match="candidate receipt mismatch"):
        verify(build)


def test_artifact_symlink_refused_even_inside_its_directory(build):
    """M20: a symlink to a sibling regular file is still not an artifact."""
    provenance = build[1] / "release-provenance.json"
    provenance.rename(build[1] / "elsewhere.json")
    provenance.symlink_to("elsewhere.json")
    with pytest.raises(ValueError, match="regular file"):
        verify(build)


def rebuild_oci(build, *, manifest_edit=None, extra=(), layout=True):
    """Rewrite the OCI archive (optionally editing the manifest) and re-bind provenance."""
    artifacts, provenance = build[1], build[2]
    config = b'{}'
    manifest_data = {"schemaVersion": 2, "config": {
        "digest": "sha256:" + digest(config), "size": len(config)}, "layers": []}
    if manifest_edit:
        manifest_edit(manifest_data)
    manifest = json.dumps(manifest_data).encode()
    descriptor = {"digest": "sha256:" + digest(manifest), "size": len(manifest)}
    path = artifacts / "image.oci.tar"
    with tarfile.open(path, "w") as archive:
        for name, data in {
            **({"oci-layout": b'{"imageLayoutVersion":"1.0.0"}'} if layout else {}),
            "index.json": json.dumps({"schemaVersion": 2, "manifests": [descriptor]}).encode(),
            "blobs/sha256/" + digest(manifest): manifest,
            "blobs/sha256/" + digest(config): config,
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info, data in extra:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    provenance["image_digest"] = descriptor["digest"]
    mutate(build, "oci_archive_sha256", digest(path.read_bytes()))


def test_rebuilt_oci_archive_still_verifies(build):
    rebuild_oci(build)
    assert verify(build).image_digest == build[2]["image_digest"]


def test_oci_non_regular_member_refused(build):
    """M22: oci-layout as a symlink to a valid blob is refused, not followed.

    Without the regular-file check this archive verifies, because tarfile
    follows the internal link and every hash still matches.
    """
    layout = b'{"imageLayoutVersion":"1.0.0"}'
    blob = tarfile.TarInfo("blobs/sha256/" + digest(layout))
    blob.size = len(layout)
    link = tarfile.TarInfo("oci-layout")
    link.type, link.linkname = tarfile.SYMTYPE, "blobs/sha256/" + digest(layout)
    rebuild_oci(build, extra=[(blob, layout), (link, None)], layout=False)
    with pytest.raises(ValueError, match="unsafe or duplicate OCI entry"):
        verify(build)


def test_oci_descriptor_size_mismatch_refused(build):
    """M23: a descriptor must state the exact blob size."""
    rebuild_oci(build, manifest_edit=lambda m: m["config"].update(size=1))
    with pytest.raises(ValueError, match="size mismatch"):
        verify(build)


def test_oci_external_urls_refused(build):
    """M24: descriptors may not point outside the archive."""
    rebuild_oci(build, manifest_edit=lambda m: m["config"].update(
        urls=["https://example.invalid/blob"]))
    with pytest.raises(ValueError, match="external OCI URLs"):
        verify(build)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "blobs/../../escape"])
def test_oci_directory_traversal_refused(build, name):
    """M25: directory members are checked for traversal too."""
    directory = tarfile.TarInfo(name)
    directory.type = tarfile.DIRTYPE
    rebuild_oci(build, extra=[(directory, None)])
    with pytest.raises(ValueError, match="unsafe OCI path"):
        verify(build)


def test_publish_requires_complete_reservation(build):
    """M34: a partial release (provenance only) never reaches the image push."""
    verified, registry = verify(build), Registry()
    bind_tag(registry, verified)
    registry.assets = {stage.PROVENANCE: verified.assets[stage.PROVENANCE]}
    registry.python = verified.python_hashes.copy()
    with pytest.raises(ValueError, match="incomplete GitHub reservation"):
        publish(verified, registry)
    assert not registry.writes


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_oversized_distribution_metadata_refused(build, monkeypatch, kind):
    """METADATA and PKG-INFO are read with a cap; the other file stays small."""
    candidate, py = build[2]["candidate"], build[2]["python_version"]
    path = build[1] / "dist" / candidate[kind]
    text = f"Name: data-olympus\nVersion: {py}\nSummary: {'x' * 200}\n"
    if kind == "wheel":
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"data_olympus-{py}.dist-info/METADATA", text)
    else:
        tar(path, {f"data_olympus-{py}/PKG-INFO": text.encode()})
    candidate[f"{kind}_sha256"] = digest(path.read_bytes())
    mutate(build, "candidate", candidate)
    monkeypatch.setattr(stage, "METADATA_LIMIT", 128)
    with pytest.raises(ValueError, match="oversized metadata"):
        verify(build)
    monkeypatch.setattr(stage, "METADATA_LIMIT", 1024)
    assert verify(build).python_version == py


def test_oversized_provenance_refused(build, monkeypatch):
    monkeypatch.setattr(stage, "PROVENANCE_LIMIT", 8)
    with pytest.raises(ValueError, match="oversized metadata"):
        verify(build)


def test_deeply_nested_json_fails_closed_without_traceback(build, tmp_path, monkeypatch,
                                                           capsys):
    (build[1] / "release-provenance.json").write_text("[" * 200_000 + "]" * 200_000)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(build[3]))
    for key, value in {"SDLC_PIPELINE": "enabled", "GITHUB_EVENT_NAME": "workflow_run",
                       "GITHUB_REF": "refs/heads/main",
                       "GITHUB_EVENT_PATH": str(event_path)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(stage, "fetch_history", lambda *_a: None)
    real_verify = stage.verify
    monkeypatch.setattr(stage, "verify", lambda artifacts, history, event: real_verify(
        artifacts, history, event, main="main", branch_ref="release/new"))
    assert stage.main(["reserve", "--artifacts", str(build[1]),
                       "--history", str(build[0].path)]) == 1
    err = capsys.readouterr().err
    assert "RecursionError" in err and "Traceback" not in err


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


def fake_gh(monkeypatch, responses):
    def run(args, **_kwargs):
        endpoint = args[2].removeprefix(f"repos/{stage.REPOSITORY}/")
        return SimpleNamespace(returncode=0, stdout=json.dumps(responses[endpoint]).encode(),
                               stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize("release", [
    {"tag_name": "1.4.3-rc.1", "prerelease": False, "draft": False, "assets": []},
    {"tag_name": "1.4.3-rc.1", "prerelease": False, "draft": True, "assets": []},
    {"tag_name": "1.4.3-rc.1", "prerelease": True, "draft": None, "assets": []},
])
def test_production_release_must_be_a_prerelease(monkeypatch, release):
    """M36: a full release is never accepted as the reservation."""
    fake_gh(monkeypatch, {"releases?per_page=100&page=1": [release]})
    with pytest.raises(ValueError, match="not a prerelease"):
        stage.Registries().release("1.4.3-rc.1")


def test_production_lightweight_tag_refused(monkeypatch):
    """M37: a lightweight tag (ref straight to a commit) is refused before dereference."""
    fake_gh(monkeypatch, {"git/ref/tags/1.4.3-rc.1": {
        "object": {"type": "commit", "sha": "a" * 40}}})
    with pytest.raises(ValueError, match="annotated"):
        stage.Registries().tag_head("1.4.3-rc.1")


def test_production_clients_with_fake_gh_skopeo_and_pypi(build, tmp_path, monkeypatch):
    verified = verify(build)
    state = {"tag": None, "message": None, "release": None, "assets": {}, "python": {},
             "images": {}}
    # 150 foreign releases first, so the candidate is only found on page 2.
    foreign = [{"tag_name": f"0.0.{n}", "id": 1000 + n, "draft": False, "prerelease": False,
                "assets": []} for n in range(150)]
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
        if args[:4] == ("gh", "api", "-H", "Accept: application/octet-stream"):
            asset_id = int(args[4].rsplit("/", 1)[1])
            return response(list(state["assets"].values())[asset_id - 1])
        if args[:2] == ("gh", "api"):
            endpoint = args[2].removeprefix(f"repos/{stage.REPOSITORY}/")
            payload = json.loads(kwargs["input"]) if kwargs.get("input") else None
            if endpoint == "git/tags" and payload:
                assert payload["object"] == verified.head and payload["type"] == "commit"
                assert payload["message"] == stage.tag_message(verified)
                state["message"] = payload["message"] + "\n"
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
                return response({"object": {"type": "commit", "sha": state["tag"]},
                                 "message": state["message"]})
            if endpoint.startswith("releases?"):
                query = dict(item.split("=") for item in endpoint.split("?", 1)[1].split("&"))
                listing = foreign + ([{
                    "tag_name": verified.version, "id": 7, "prerelease": True,
                    "draft": state["release"] == "draft",
                    "assets": [{"name": n, "id": i, "state": "uploaded"}
                               for i, n in enumerate(state["assets"], start=1)],
                }] if state["release"] else [])
                size, page = int(query["per_page"]), int(query["page"])
                return response(listing[(page - 1) * size:page * size])
            if endpoint == "releases/7" and payload:
                assert args[args.index("--method") + 1] == "PATCH"
                assert payload == {"draft": False, "prerelease": True, "make_latest": "false"}
                assert state["release"] == "draft"
                state["release"] = "published"
                writes.append("publish-release")
                return response({})
        if args[:3] == ("gh", "release", "create"):
            assert {"--draft", "--verify-tag", "--prerelease"} <= set(args)
            state["release"] = "draft"
            writes.append("release")
            return response(b"")
        if args[:3] == ("gh", "release", "upload"):
            path = Path(args[4])
            assert path.name not in state["assets"] and "--clobber" not in args
            # Immutable releases refuse new assets once published.
            assert state["release"] == "draft", "upload to a published release"
            state["assets"][path.name] = path.read_bytes()
            writes.append("asset")
            return response(b"")
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
        assert state["release"] == "draft"
        assert stage.stage_upload(verified, client, tmp_path / "first") == 2
        state["python"] = verified.python_hashes.copy()
        publish(verified, client, move_rc=True)
        assert state["images"].keys() == {verified.version, "rc"}
        assert state["release"] == "draft"
        stage.finalize(verified, client)
        assert state["release"] == "published" and writes[-1] == "publish-release"
        first_writes = writes.copy()
        assert stage.reserve(verified, client) == 0
        assert stage.stage_upload(verified, client, tmp_path / "retry") == 0
        publish(verified, client, move_rc=True)
        stage.finalize(verified, client)
        assert writes == first_writes
    finally:
        client.close()


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/rc-publish-stage.yml"


ADMISSION = (
    "vars.SDLC_PIPELINE == 'enabled' && "
    "github.event.workflow_run.conclusion == 'success' && "
    "github.event.workflow_run.event == 'push' && "
    "(github.event.workflow_run.head_branch == 'release/new' || "
    "github.event.workflow_run.head_branch == 'hotfix/new') && "
    "github.event.workflow_run.head_repository.full_name == github.repository"
)


def squash(text):
    return re.sub(r"\s+", " ", text).replace("( ", "(").replace(" )", ")").strip()


def test_lock_is_taken_only_by_admitted_runs():
    """C1: no-op runs get a private group and cannot cancel pending promotions."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    concurrency = workflow["concurrency"]
    assert set(concurrency) == {"group", "cancel-in-progress"}
    assert concurrency["cancel-in-progress"] is False
    assert squash(concurrency["group"]) == (
        "${{ (" + ADMISSION + ") && 'data-olympus-promotion' || "
        "format('rc-publish-noop-{0}', github.run_id) }}")
    # The lock condition and the job gate are the same expression.
    assert squash(workflow["jobs"]["reserve"]["if"]) == ADMISSION


def test_workflow_security_contract():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert workflow.get("on", workflow.get(True)) == {
        "workflow_run": {"workflows": ["rc-build"], "types": ["completed"]}}
    assert workflow["permissions"] == {}
    jobs = workflow["jobs"]
    assert set(jobs) == {"reserve", "pypi", "publish", "attest", "finalize"}
    for gate in ("vars.SDLC_PIPELINE == 'enabled'", "conclusion == 'success'",
                 "head_branch == 'release/new'", "head_branch == 'hotfix/new'",
                 "event == 'push'", "head_repository.full_name == github.repository"):
        assert gate in jobs["reserve"]["if"]
    assert jobs["pypi"]["needs"] == "reserve"
    assert jobs["publish"]["needs"] == ["reserve", "pypi"]
    assert jobs["attest"]["needs"] == "publish"
    # A skipped pypi (files already on PyPI) must not skip attest or finalize:
    # GitHub's implicit success() treats a skipped ancestor as not successful.
    assert jobs["attest"]["if"] == "${{ !cancelled() && needs.publish.result == 'success' }}"
    # Immutable releases: the draft is published last, after attestations.
    assert jobs["finalize"]["needs"] == ["publish", "attest"]
    assert jobs["finalize"]["if"] == (
        "${{ !cancelled() && needs.publish.result == 'success' && "
        "needs.attest.result == 'success' }}")
    assert squash(jobs["publish"]["if"]) == (
        "!cancelled() && needs.reserve.result == 'success' && "
        "(needs.pypi.result == 'success' || needs.pypi.result == 'skipped')")
    assert list(jobs)[-1] == "finalize"
    assert jobs["reserve"]["permissions"] == {
        "actions": "read", "contents": "write", "packages": "read"}
    assert jobs["pypi"]["permissions"] == {
        "actions": "read", "contents": "read", "packages": "read", "id-token": "write"}
    assert jobs["publish"]["permissions"] == {
        "actions": "read", "contents": "read", "packages": "write"}
    assert jobs["attest"]["permissions"] == {
        "actions": "read", "attestations": "write", "id-token": "write", "packages": "write"}
    assert jobs["finalize"]["permissions"] == {
        "actions": "read", "contents": "write", "packages": "read"}
    assert [n for n, j in jobs.items() if j["permissions"].get("contents") == "write"] == [
        "reserve", "finalize"]
    finalize_run = [s["run"] for s in jobs["finalize"]["steps"]
                    if "rc_verify_and_publish.py" in s.get("run", "")]
    assert len(finalize_run) == 1 and "rc_verify_and_publish.py finalize" in finalize_run[0]
    assert [n for n, j in jobs.items() if "attestations" in j["permissions"]] == ["attest"]
    assert [n for n, j in jobs.items() if "id-token" in j["permissions"]] == ["pypi", "attest"]
    assert [name for name, job in jobs.items() if "environment" in job] == ["pypi"]
    assert jobs["pypi"]["environment"] == "pypi-rc"
    text = WORKFLOW.read_text()
    assert "secrets." not in text and "pull_request" not in text
    for name, job in jobs.items():
        steps = job["steps"]
        if name == "attest":
            # The signing job runs no repository code.
            assert not any(s.get("uses", "").startswith("actions/checkout") for s in steps)
            assert "scripts/" not in json.dumps(steps) and "uv run" not in json.dumps(steps)
        else:
            checkout = steps[0]
            assert checkout["uses"].startswith("actions/checkout")
            assert checkout["with"] == {"ref": "${{ github.sha }}",
                                        "persist-credentials": False}
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
        assert any(u.startswith("actions/attest") for u in uses) == (name == "attest")
    publish_step = next(s for s in jobs["pypi"]["steps"]
                        if s.get("uses", "").startswith("pypa/gh-action-pypi-publish"))
    assert set(publish_step["with"]) == {"packages-dir"}


def test_rc_channel_gate_reaches_only_the_publish_script():
    """C3: the rc move is gated by its own variable, default off."""
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    users = [(name, step) for name, job in jobs.items() for step in job["steps"]
             if "SDLC_RC_CHANNEL" in json.dumps(step)]
    assert [(name, step["id"]) for name, step in users] == [("publish", "publish")]
    assert users[0][1]["env"]["SDLC_RC_CHANNEL"] == "${{ vars.SDLC_RC_CHANNEL }}"


SHA_PINNED = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7"),
    "astral-sh/setup-uv": ("37802adc94f370d6bfd71619e3f0bf239e1f3b78", "v7"),
    "actions/create-github-app-token": ("fee1f7d63c2ff003460e3d139729b119787bc349", "v2"),
    "actions/attest-build-provenance": ("977bb373ede98d70efdf65b84cb5f73e068dcc2a", "v3"),
    "pypa/gh-action-pypi-publish": ("dc37677b2e1c63e2034f94d8a5b11f265b73ba33", "release/v1"),
}


def test_privileged_actions_are_pinned_by_full_sha():
    """Every use of the five ruled actions is the verified 40-hex SHA with its tag
    as a trailing comment; other actions may keep tags."""
    lines = [line for line in WORKFLOW.read_text().splitlines()
             if re.match(r"\s*(- )?uses:", line)]
    assert lines
    seen = set()
    for line in lines:
        ref = line.split("uses:", 1)[1].split("#", 1)[0].strip()
        action, _, version = ref.partition("@")
        assert version, line
        if action in SHA_PINNED:
            sha, tag = SHA_PINNED[action]
            assert re.fullmatch(r"[0-9a-f]{40}", version) and version == sha, line
            assert line.rstrip().endswith(f"# {tag}"), line
            seen.add(action)
    # create-github-app-token is not used: stage 2 needs no App token.
    assert seen == set(SHA_PINNED) - {"actions/create-github-app-token"}
    assert "tag-pin" not in WORKFLOW.read_text()


def test_attestations_follow_verified_publication():
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    assert jobs["publish"]["outputs"] == {
        key: "${{ steps.publish.outputs." + key + " }}"
        for key in ("image_digest", "wheel", "wheel_sha256", "sdist", "sdist_sha256")}
    steps = jobs["attest"]["steps"]
    check = next(i for i, s in enumerate(steps) if "sha256sum --check" in s.get("run", ""))
    assert steps[check]["env"] == {
        "IMAGE_DIGEST": "${{ needs.publish.outputs.image_digest }}",
        "WHEEL": "${{ needs.publish.outputs.wheel }}",
        "WHEEL_SHA256": "${{ needs.publish.outputs.wheel_sha256 }}",
        "SDIST": "${{ needs.publish.outputs.sdist }}",
        "SDIST_SHA256": "${{ needs.publish.outputs.sdist_sha256 }}"}
    attest = [i for i, s in enumerate(steps)
              if s.get("uses", "").startswith("actions/attest-build-provenance@")]
    assert len(attest) == 2 and min(attest) > check
    image, files = (steps[i]["with"] for i in attest)
    assert image == {"subject-name": stage.IMAGE,
                     "subject-digest": "${{ needs.publish.outputs.image_digest }}",
                     "push-to-registry": True}
    # Only the copies that passed sha256sum are subjects, never the raw artifact.
    assert files == {"subject-path": "to-delete/rc-attest/*.whl\n"
                                     "to-delete/rc-attest/*.tar.gz\n"}


def test_attest_hash_check_rejects_tampered_files(tmp_path):
    """C4: the attest job's shell check fails on a byte change or a symlink."""
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    script = next(s["run"] for s in jobs["attest"]["steps"]
                  if "sha256sum --check" in s.get("run", ""))
    if subprocess.run(["bash", "-c", "command -v sha256sum"], check=False,
                      capture_output=True).returncode:
        pytest.skip("sha256sum not available")
    dist = tmp_path / "to-delete" / "rc-input" / "dist"
    dist.mkdir(parents=True)
    wheel, sdist = "data_olympus-1.4.3rc1-py3-none-any.whl", "data_olympus-1.4.3rc1.tar.gz"
    (dist / wheel).write_bytes(b"wheel")
    (dist / sdist).write_bytes(b"sdist")
    env = {"PATH": os.environ["PATH"],
           "IMAGE_DIGEST": "sha256:" + "a" * 64, "WHEEL": wheel, "SDIST": sdist,
           "WHEEL_SHA256": digest(b"wheel"), "SDIST_SHA256": digest(b"sdist")}

    def run(**overrides):
        target = tmp_path / "to-delete" / "rc-attest"
        if target.exists():
            for child in target.iterdir():
                child.unlink()
            target.rmdir()
        return subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env | overrides,
                              check=False, capture_output=True).returncode

    assert run() == 0
    assert run(WHEEL_SHA256=digest(b"other")) != 0
    # Each unsafe name points at an existing file with the matching hash, so only
    # the bash name regex can reject it.
    # dist/../../ is to-delete/, outside the downloaded artifact.
    (tmp_path / "to-delete" / "evil.tar.gz").write_bytes(b"sdist")
    (tmp_path / "to-delete" / "evil.whl").write_bytes(b"wheel")
    (dist / "data_olympus-a b.whl").write_bytes(b"wheel")
    (dist / "data_olympus-a b.tar.gz").write_bytes(b"sdist")
    for traversal in ("../../evil.tar.gz", "data_olympus-a b.tar.gz"):
        assert run(SDIST=traversal) != 0, traversal
    for traversal in ("../../evil.whl", "data_olympus-a b.whl"):
        assert run(WHEEL=traversal) != 0, traversal
    assert run(IMAGE_DIGEST="sha256:" + "a" * 63) != 0
    # A symlink to identical bytes passes sha256sum, so only the -L check stops it.
    (dist / wheel).unlink()
    (tmp_path / "outside.whl").write_bytes(b"wheel")
    (dist / wheel).symlink_to(tmp_path / "outside.whl")
    assert run() != 0


def test_docker_login_reads_token_from_stdin_only(tmp_path):
    """The GHCR token reaches docker on stdin, never on argv."""
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    step = next(s for s in jobs["attest"]["steps"] if "docker login" in s.get("run", ""))
    assert step["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert "--password-stdin" in step["run"]
    assert re.search(r"--password(?!-stdin)|-p\s", step["run"]) is None
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$OUT/argv"\n'
                                 'cat > "$OUT/stdin"\n')
    (fake / "docker").chmod(0o755)
    token = "fake-token-value"
    subprocess.run(["bash", "-c", step["run"]], check=True, env={
        "PATH": f"{fake}:{os.environ['PATH']}", "OUT": str(tmp_path),
        "GH_TOKEN": token, "GITHUB_ACTOR": "machine"})
    assert token not in (tmp_path / "argv").read_text()
    assert (tmp_path / "stdin").read_text() == token


def test_secrets_live_only_in_declared_environments():
    """Bot or App credentials must be environment secrets on main-only environments."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert "secrets" not in json.dumps(workflow.get("env", {}))
    for name, job in workflow["jobs"].items():
        if "secrets." in json.dumps(job):
            assert job.get("environment") in ("pypi-rc", "sdlc-bot"), name
    # Stage 2 needs no App token: no App-token action and no sdlc-bot job today.
    assert "create-github-app-token" not in WORKFLOW.read_text()
    assert not any(job.get("environment") == "sdlc-bot"
                   for job in workflow["jobs"].values())


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
    calls = []
    monkeypatch.setenv("SDLC_RC_CHANNEL", "enabled")
    monkeypatch.setattr(stage, "publish", lambda *_a, **kw: calls.append(kw) or {
        "H": verified.head, "branch": verified.branch})
    monkeypatch.setattr(stage, "selection", lambda record, _h: record | {"selected": True})
    selection = tmp_path / "out" / "staging-selection.json"
    assert stage.main(["publish", "--artifacts", str(build[1]), "--history",
                       str(build[0].path), "--selection", str(selection)]) == 0
    wheel = next(n for n in verified.python_hashes if n.endswith(".whl"))
    sdist = next(n for n in verified.python_hashes if n.endswith(".tar.gz"))
    assert output.read_text() == (
        f"image_digest={verified.image_digest}\n"
        f"wheel={wheel}\nwheel_sha256={verified.python_hashes[wheel]}\n"
        f"sdist={sdist}\nsdist_sha256={verified.python_hashes[sdist]}\n")
    assert calls == [{"move_rc": True}]
    assert json.loads(selection.read_text())["selected"] is True
    assert verified.head in summary.read_text()


@pytest.mark.parametrize(("value", "expected"), [(None, False), ("", False),
                                                 ("true", False), ("enabled", True)])
def test_cli_rc_channel_variable_must_be_exactly_enabled(build, tmp_path, monkeypatch,
                                                         value, expected):
    verified = verify(build)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(build[3]))
    for key, item in {"SDLC_PIPELINE": "enabled", "GITHUB_EVENT_NAME": "workflow_run",
                      "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_PATH": str(event_path),
                      "GITHUB_OUTPUT": str(tmp_path / "output"),
                      "GITHUB_STEP_SUMMARY": str(tmp_path / "summary")}.items():
        monkeypatch.setenv(key, item)
    if value is None:
        monkeypatch.delenv("SDLC_RC_CHANNEL", raising=False)
    else:
        monkeypatch.setenv("SDLC_RC_CHANNEL", value)
    calls = []
    monkeypatch.setattr(stage, "fetch_history", lambda *_a: None)
    monkeypatch.setattr(stage, "verify", lambda *_a, **_kw: verified)
    monkeypatch.setattr(stage, "publish", lambda *_a, **kw: calls.append(kw) or {
        "H": verified.head, "branch": verified.branch})
    monkeypatch.setattr(stage, "selection", lambda record, _h: record | {"selected": True})
    assert stage.main(["publish", "--artifacts", str(build[1]), "--history",
                       str(build[0].path), "--selection", str(tmp_path / "s.json")]) == 0
    assert calls == [{"move_rc": expected}]


@pytest.mark.parametrize("name", ["data_olympus-1.4.3rc1\nx=y.whl", "evil-1.0.whl"])
def test_attestation_outputs_refuse_unsafe_names(build, name):
    """Only validated single-line values reach GITHUB_OUTPUT for the attest job."""
    verified = verify(build)
    sdist = next(n for n in verified.python_hashes if n.endswith(".tar.gz"))
    unsafe = replace(verified, python_hashes={name: "a" * 64, sdist: "b" * 64})
    with pytest.raises(ValueError, match="invalid distribution output"):
        stage.attestation_outputs(unsafe)
    with pytest.raises(ValueError, match="invalid distribution output"):
        stage.attestation_outputs(replace(verified, python_hashes={
            n: "A" * 64 for n in verified.python_hashes}))


def failing_run(monkeypatch, stderr, stdout=b"stdout-secret-marker"):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def command_error(*args, **kwargs):
    with pytest.raises(ValueError) as caught:
        stage.command(*args, **kwargs)
    return str(caught.value)


def test_command_failure_carries_stderr_excerpt(monkeypatch):
    failing_run(monkeypatch, b"HTTP 422: Validation Failed (ReleaseAsset.name already_exists)")
    assert command_error("gh", "release", "upload") == (
        "gh operation failed (exit 1): HTTP 422: Validation Failed "
        "(ReleaseAsset.name already_exists)")


def test_command_failure_without_stderr_keeps_plain_message(monkeypatch):
    failing_run(monkeypatch, b" \n\t ")
    assert command_error("gh", "api") == "gh operation failed (exit 1)"


def test_stderr_excerpt_is_single_printable_line():
    raw = (b"first\nsecond\r\n\tthird\x1b[31mred\x1b[0m\x00\x07\x7f"
           b"\x1b]0;title\x07end \xc3\xa9\xff caf\xc3\xa9")
    excerpt = stage._stderr_excerpt(raw)
    assert excerpt == "first second thirdredend caf"
    assert re.fullmatch(r"[\x20-\x7e]*", excerpt)


@pytest.mark.parametrize("secret, kept", [
    (b"github_pat_11ABCDEFG0123_ab", b""),
    (b"ghp_" + b"a1" * 18, b""),
    (b"ghs_" + b"Z9" * 18, b""),
    (b"gho_" + b"q" * 20, b""),
    (b"Authorization: Bearer abc.def.ghi", b"Authorization"),
    (b"authorization: token sekrit-value", b"authorization"),
    (b"Authorization: Basic dXNlcjpwYXNz", b"Authorization"),
    (b"Bearer eyJhbGciOi.payload.sig", b"Bearer"),
    (b"token 0123456789abcdef", b"token"),
    (b"https://user:hunter2pass@github.com/x", b"github.com/x"),
    (b"f" * 40, b""),
    (b"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0123456789+/==", b""),
])
def test_stderr_excerpt_redacts_credentials(secret, kept):
    excerpt = stage._stderr_excerpt(b"before " + secret + b" after")
    assert "[REDACTED]" in excerpt
    assert excerpt.startswith("before ") and excerpt.endswith(" after")
    assert kept.decode() in excerpt
    for fragment in (b"ABCDEFG012345", b"a1a1a1", b"Z9Z9Z9", b"qqqqqqqq", b"abc.def",
                     b"sekrit", b"dXNlcjpw", b"eyJhbG", b"0123456789abcdef", b"hunter2",
                     b"ffffffff", b"QUJDREVGR0hJ"):
        assert fragment.decode() not in excerpt


def test_stderr_excerpt_keeps_ordinary_token_wording():
    assert stage._stderr_excerpt(b"token expired") == "token expired"


@pytest.mark.parametrize("payload", [
    b"::set-output name=x::y",
    b"::add-mask::value",
    b"x\n::error file=a::boom",
    b"##[error]boom",
    b"###[group]x",
    b":::::",
])
def test_stderr_excerpt_neutralises_workflow_commands(payload):
    excerpt = stage._stderr_excerpt(payload)
    assert "::" not in excerpt
    assert "##[" not in excerpt


def test_workflow_command_cannot_start_printed_line(monkeypatch, capsys):
    failing_run(monkeypatch, b"\n::set-output name=upload::true\n##[error]x")
    with pytest.raises(ValueError) as caught:
        stage.command("gh", "release", "upload")
    print(f"RC publication refused: {stage._one_line(str(caught.value))}")
    for line in capsys.readouterr().out.splitlines():
        assert line.startswith("RC publication refused: gh operation failed (exit 1): ")
        assert "::" not in line and "##[" not in line


def test_stderr_excerpt_truncates():
    excerpt = stage._stderr_excerpt(b"word " * 200)
    assert len(excerpt) == stage.STDERR_EXCERPT_LIMIT + 3
    assert excerpt.endswith("...")
    exact = b"y " * 150
    assert stage._stderr_excerpt(exact[:-1]) == exact[:-1].decode()
    assert stage._one_line(f"gh operation failed (exit 1): {excerpt}").endswith("...")


def test_command_failure_never_echoes_stdout_args_or_input(monkeypatch):
    failing_run(monkeypatch, b"failure detail")
    message = command_error("gh", "api", "--field", "argument-secret-marker",
                            input_data=b"input-secret-marker")
    assert "failure detail" in message
    for marker in ("stdout-secret-marker", "argument-secret-marker", "input-secret-marker",
                   "api", "--field"):
        assert marker not in message


def test_absent_matches_raw_stderr_before_sanitising(monkeypatch):
    # The raw text spans lines and carries controls the excerpt would remove.
    failing_run(monkeypatch, b"error:\n\x1b[1mmanifest\tunknown\x1b[0m")
    assert stage.command("skopeo", "inspect", absent=r"manifest\tunknown") is None
    assert stage.command("skopeo", "inspect", absent=r"error:\n") is None
    message = command_error("skopeo", "inspect", absent=r"not-present")
    assert message == "skopeo operation failed (exit 1): error: manifest unknown"


def test_environment_token_value_is_redacted(monkeypatch):
    fake = "fake-env-token-value-xyz"
    monkeypatch.setenv("GH_TOKEN", fake)
    monkeypatch.setenv("GITHUB_TOKEN", "other-fake-value")
    failing_run(monkeypatch, f"bad credentials for {fake}; also other-fake-value".encode())
    message = command_error("gh", "release", "upload")
    assert fake not in message and "other-fake-value" not in message
    assert message == ("gh operation failed (exit 1): bad credentials for [REDACTED]; "
                       "also [REDACTED]")


def reserved(build, **overrides):
    verified, registry = verify(build), Registry()
    stage.reserve(verified, registry)
    for key, value in overrides.items():
        setattr(registry, key, value)
    registry.writes.clear()
    return verified, registry


def test_reservation_stays_draft_until_finalize_publishes_last(build, tmp_path):
    verified, registry = verify(build), Registry()
    stage.reserve(verified, registry)
    assert (registry.draft, registry.assets) == (True, verified.assets)
    stage.stage_upload(verified, registry, tmp_path / "upload")
    registry.python = verified.python_hashes.copy()
    publish(verified, registry, move_rc=True)
    assert registry.draft is True
    stage.finalize(verified, registry)
    assert registry.writes == ["reserve", "image", "rc", "finalize"]
    assert registry.draft is False


@pytest.mark.parametrize("missing", ["python", "image", "asset", "tag"])
def test_finalize_refuses_before_every_surface_is_complete(build, missing):
    verified, registry = reserved(build)
    registry.python = verified.python_hashes.copy()
    registry.image = verified.image_digest
    if missing == "python":
        registry.python.popitem()
    elif missing == "image":
        registry.image = None
    elif missing == "asset":
        registry.assets.pop(stage.PROVENANCE)
    else:
        registry.tag = None
    with pytest.raises(ValueError):
        stage.finalize(verified, registry)
    assert registry.draft is True and not registry.writes


def test_finalize_readback_must_show_a_published_release(build):
    verified, registry = reserved(build, image=None)
    registry.python = verified.python_hashes.copy()
    registry.image = verified.image_digest
    registry.publish_release = lambda _v: registry.writes.append("noop")
    with pytest.raises(ValueError, match="publication readback"):
        stage.finalize(verified, registry)


@pytest.mark.parametrize("phase", ["reserve", "stage_upload", "publish", "finalize"])
@pytest.mark.parametrize("assets", ["none", "provenance_only"])
def test_published_release_with_missing_assets_is_burned(build, tmp_path, phase, assets):
    verified, registry = verify(build), Registry()
    bind_tag(registry, verified)
    registry.draft = False
    registry.assets = ({} if assets == "none"
                       else {stage.PROVENANCE: verified.assets[stage.PROVENANCE]})
    registry.python = verified.python_hashes.copy()
    registry.image = verified.image_digest
    call = {"reserve": lambda: stage.reserve(verified, registry),
            "stage_upload": lambda: stage.stage_upload(verified, registry, tmp_path / "u"),
            "publish": lambda: publish(verified, registry),
            "finalize": lambda: stage.finalize(verified, registry)}[phase]
    with pytest.raises(ValueError, match="burned") as caught:
        call()
    assert "next rc number" in str(caught.value)
    assert not registry.writes


def test_published_release_with_different_bytes_is_burned(build):
    verified, registry = verify(build), Registry()
    bind_tag(registry, verified)
    registry.draft = False
    registry.assets = {**verified.assets, stage.PROVENANCE: b"other"}
    with pytest.raises(ValueError, match="burned"):
        stage.reserve(verified, registry)
    assert not registry.writes


def test_complete_published_release_is_reused(build, tmp_path):
    verified, registry = verify(build), Registry()
    bind_tag(registry, verified)
    registry.draft, registry.assets = False, verified.assets.copy()
    assert stage.reserve(verified, registry) == 2
    assert stage.stage_upload(verified, registry, tmp_path / "upload") == 2
    registry.python = verified.python_hashes.copy()
    publish(verified, registry)
    stage.finalize(verified, registry)
    assert registry.writes == ["image"]


def test_partial_draft_is_reused_and_completed(build):
    verified, registry = verify(build), Registry()
    bind_tag(registry, verified)
    registry.assets = {stage.PROVENANCE: verified.assets[stage.PROVENANCE]}
    stage.reserve(verified, registry)
    assert registry.writes == ["reserve"]
    assert (registry.draft, registry.assets) == (True, verified.assets)
    assert stage.reserve(verified, registry) == 2
    assert registry.writes == ["reserve"]


def test_wrong_draft_asset_bytes_are_refused(build):
    verified, registry = verify(build), Registry()
    bind_tag(registry, verified)
    name = next(iter(verified.python_hashes))
    registry.assets = {name: b"partial or substituted bytes"}
    with pytest.raises(ValueError, match="draft asset differs"):
        stage.reserve(verified, registry)
    assert not registry.writes


def test_read_only_jobs_bind_an_invisible_draft_through_the_tag(build, tmp_path):
    """contents: read cannot list drafts; the annotated tag at H binds instead."""
    verified, registry = reserved(build, hide_drafts=True)
    assert stage.stage_upload(verified, registry, tmp_path / "upload") == 2
    registry.python = verified.python_hashes.copy()
    publish(verified, registry)
    assert registry.writes == ["image"]
    registry.tag = None
    with pytest.raises(ValueError):
        stage.stage_upload(verified, registry, tmp_path / "other")
    with pytest.raises(ValueError):
        publish(verified, registry)
    # The draft-aware finalize never trusts that shortcut.
    bind_tag(registry, verified)
    registry.hide_drafts = False
    registry.assets.pop(next(iter(verified.python_hashes)))
    with pytest.raises(ValueError, match="incomplete GitHub reservation"):
        stage.finalize(verified, registry)
    assert registry.draft is True


def release_listing(monkeypatch, pages):
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:4] == ("gh", "api", "-H", "Accept: application/octet-stream"):
            return SimpleNamespace(returncode=0, stdout=b"bytes", stderr=b"")
        page = int(args[2].rsplit("page=", 1)[1])
        assert "per_page=100" in args[2]
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            pages[page - 1] if page <= len(pages) else []).encode(), stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def entry(tag, **extra):
    return {"tag_name": tag, "id": 9, "draft": True, "prerelease": True, "assets": []} | extra


def test_release_lookup_paginates_and_matches_the_exact_tag(monkeypatch):
    first = [entry("1.4.3-rc.10"), entry("v1.4.3-rc.1"), entry("1.4.3-rc.1 ")] + [
        entry(f"0.0.{n}") for n in range(97)]
    second = [entry("1.4.3-rc.1", assets=[{"name": stage.PROVENANCE, "id": 3,
                                           "state": "uploaded"}])]
    calls = release_listing(monkeypatch, [first, second])
    found = stage.Registries().release("1.4.3-rc.1")
    assert found == stage.Release(True, {stage.PROVENANCE: b"bytes"})
    assert calls[-1][-1].endswith("releases/assets/3")
    assert stage.Registries().release("1.4.3-rc.2") is None


def test_release_lookup_refuses_ambiguity_and_unbounded_listings(monkeypatch):
    release_listing(monkeypatch, [[entry("1.4.3-rc.1"), entry("1.4.3-rc.1", draft=False)]])
    with pytest.raises(ValueError, match="ambiguous"):
        stage.Registries().release("1.4.3-rc.1")
    monkeypatch.setattr(stage, "RELEASE_PAGE_LIMIT", 2)
    full = [entry(f"0.0.{n}") for n in range(100)]
    release_listing(monkeypatch, [full, full, full])
    with pytest.raises(ValueError, match="page limit"):
        stage.Registries().release("1.4.3-rc.1")


def test_interrupted_asset_upload_is_never_read_as_complete(monkeypatch):
    release_listing(monkeypatch, [[entry("1.4.3-rc.1", assets=[
        {"name": stage.PROVENANCE, "id": 3, "state": "starter"}])]])
    with pytest.raises(ValueError, match="upload is incomplete"):
        stage.Registries().release("1.4.3-rc.1")


def test_production_reserve_creates_a_draft_and_uploads_only_missing(build, monkeypatch):
    """Draft reuse: an existing draft is never recreated, published or clobbered."""
    verified = verify(build)
    calls = []
    uploaded = {stage.PROVENANCE: verified.assets[stage.PROVENANCE]}

    def run(args, **_kwargs):
        calls.append(args)
        if args[:4] == ("gh", "api", "-H", "Accept: application/octet-stream"):
            return SimpleNamespace(returncode=0, stdout=list(uploaded.values())[
                int(args[4].rsplit("/", 1)[1]) - 1], stderr=b"")
        if args[:2] == ("gh", "api") and "git/ref/tags/" in args[2]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"object": {"type": "tag", "sha": "a" * 40}}).encode(), stderr=b"")
        if args[:2] == ("gh", "api") and "git/tags/" in args[2]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"object": {"type": "commit", "sha": verified.head}}).encode(), stderr=b"")
        if args[:2] == ("gh", "api") and "releases?" in args[2]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([entry(
                verified.version, assets=[{"name": n, "id": i, "state": "uploaded"}
                                          for i, n in enumerate(uploaded, start=1)])]).encode(),
                stderr=b"")
        if args[:3] == ("gh", "release", "upload"):
            assert "--clobber" not in args
            uploaded[Path(args[4]).name] = Path(args[4]).read_bytes()
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        pytest.fail(f"unexpected call {args[:3]}")

    monkeypatch.setattr(subprocess, "run", run)
    stage.Registries().reserve(verified)
    assert [Path(a[4]).name for a in calls if a[:3] == ("gh", "release", "upload")] == sorted(
        set(verified.files) - {stage.PROVENANCE}, key=list(verified.files).index)
    assert not any(a[:3] == ("gh", "release", "create") for a in calls)
    assert uploaded == verified.assets



def test_finalize_cli_runs_only_finalize(build, tmp_path, monkeypatch):
    verified = verify(build)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(build[3]))
    for key, value in {"SDLC_PIPELINE": "enabled", "GITHUB_EVENT_NAME": "workflow_run",
                       "GITHUB_REF": "refs/heads/main",
                       "GITHUB_EVENT_PATH": str(event_path)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(stage, "fetch_history", lambda *_a: None)
    monkeypatch.setattr(stage, "verify", lambda *_a, **_kw: verified)
    calls = []
    monkeypatch.setattr(stage, "finalize", lambda v, _c: calls.append(v))
    for name in ("reserve", "stage_upload", "publish"):
        monkeypatch.setattr(stage, name, lambda *_a, **_kw: pytest.fail("wrong phase"))
    assert stage.main(["finalize", "--artifacts", str(build[1]),
                       "--history", str(build[0].path)]) == 0
    assert calls == [verified]


@pytest.mark.parametrize("fault", ["drop", "bytes"])
def test_reserve_refuses_an_upload_that_does_not_read_back(build, fault):
    """The reserve readback is the byte check before PyPI: a lossy upload fails it."""
    verified, registry = verify(build), Registry()
    real = registry.reserve
    victim = next(iter(verified.python_hashes))

    def lossy(v):
        real(v)
        if fault == "drop":
            del registry.assets[victim]
        else:
            registry.assets[victim] = b"truncated"

    registry.reserve = lossy
    sleeps = []
    with pytest.raises(ValueError):
        stage.reserve(verified, registry, sleep=sleeps.append)
    # A dropped asset is awaited within the bound; different bytes fail at once.
    assert sleeps == (list(stage.LISTING_DELAYS) if fault == "drop" else [])
    with pytest.raises(ValueError):
        stage.finalize(verified, registry)
    assert registry.draft is True


@pytest.mark.parametrize("change", ["hash", "older_format", "missing", "extra_line"])
def test_read_only_jobs_require_the_tag_to_bind_the_asset_hashes(build, tmp_path, change):
    """C1: pypi/publish cannot see the draft, so the tag message binds the bytes."""
    verified, registry = reserved(build, hide_drafts=True)
    if change == "hash":
        registry.tag_text = registry.tag_text.replace(
            hashlib.sha256(verified.assets[stage.PROVENANCE]).hexdigest(), "0" * 64)
    elif change == "older_format":
        registry.tag_text = f"Candidate {verified.version}\nH: {verified.head}"
    elif change == "extra_line":
        registry.tag_text += f"\nsha256 {'a' * 64}  extra.bin"
    else:
        registry.tag_text = None
    with pytest.raises(ValueError, match="does not bind these asset hashes"):
        stage.stage_upload(verified, registry, tmp_path / "upload")
    with pytest.raises(ValueError, match="does not bind these asset hashes"):
        publish(verified, registry)
    registry.hide_drafts = False
    with pytest.raises(ValueError, match="does not bind these asset hashes"):
        stage.reserve(verified, registry)
    assert not registry.writes and not (tmp_path / "upload").exists()


def test_tag_message_format_is_exact(build):
    verified = verify(build)
    lines = stage.tag_message(verified).split("\n")
    assert lines[:3] == [f"Candidate {verified.version}", f"H: {verified.head}", ""]
    assert lines[3:] == [f"sha256 {digest(data)}  {name}"
                         for name, data in sorted(verified.assets.items())]


def production_reserve_fake(monkeypatch, verified, listing):
    """gh fake for Registries.reserve; listing(calls) returns the current releases."""
    calls = []

    def ok(value):
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        return SimpleNamespace(returncode=0, stdout=data, stderr=b"")

    def run(args, **_kwargs):
        calls.append(args)
        if args[:2] == ("gh", "api") and "git/ref/tags/" in args[2]:
            return ok({"object": {"type": "tag", "sha": "a" * 40}})
        if args[:2] == ("gh", "api") and "git/tags/" in args[2]:
            return ok({"object": {"type": "commit", "sha": verified.head},
                       "message": stage.tag_message(verified)})
        if args[:2] == ("gh", "api") and "releases?" in args[2]:
            return ok(listing(calls))
        if args[:3] in (("gh", "release", "create"), ("gh", "release", "upload")):
            return ok(b"")
        if args[:2] == ("gh", "api") and "/releases/" in args[2] and "PATCH" in args:
            return ok({})
        pytest.fail(f"unexpected call {args[:3]}")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_reserve_refuses_when_the_created_release_is_not_a_draft(build, monkeypatch):
    """If create ever published the release, stop before any upload or PyPI."""
    verified = verify(build)

    def listing(calls):
        created = any(a[:3] == ("gh", "release", "create") for a in calls)
        return [entry(verified.version, draft=False)] if created else []

    calls = production_reserve_fake(monkeypatch, verified, listing)
    with pytest.raises(ValueError, match="draft release was not created"):
        stage.Registries().reserve(verified)
    assert not any(a[:3] == ("gh", "release", "upload") for a in calls)


def test_reserve_refuses_a_release_published_after_inventory(build, monkeypatch):
    """Nit 1: a release that became published since inventory is never uploaded to."""
    verified = verify(build)
    calls = production_reserve_fake(
        monkeypatch, verified, lambda _calls: [entry(verified.version, draft=False)])
    with pytest.raises(ValueError, match="burned"):
        stage.Registries().reserve(verified)
    assert not any(a[:3] == ("gh", "release", "upload") for a in calls)


def test_publish_release_refuses_a_non_draft(build, monkeypatch):
    verified = verify(build)
    calls = production_reserve_fake(
        monkeypatch, verified, lambda _calls: [entry(verified.version, draft=False)])
    with pytest.raises(ValueError, match="no draft release to publish"):
        stage.Registries().publish_release(verified)
    assert not any("PATCH" in a for a in calls)



def test_environment_token_split_by_control_bytes_is_redacted(monkeypatch):
    """A token interleaved with escapes survives the first pass; the second catches it."""
    monkeypatch.setenv("GH_TOKEN", "fake env token")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    excerpt = stage._stderr_excerpt(b"denied for fake\x1b[0m env\ttoken here")
    assert excerpt == "denied for [REDACTED] here"


@pytest.mark.parametrize("splitter", [b"\x1b[0m", b"\x01", b"\x1b]0;t\x07", b"\x7f"])
def test_realistic_token_split_by_an_escape_is_redacted(monkeypatch, splitter):
    """A 40-character ghs_ token cut by a control sequence is rejoined, then redacted."""
    token = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    assert len(token) == 40
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    raw = b"denied " + token[:22].encode() + splitter + token[22:].encode() + b" end"
    excerpt = stage._stderr_excerpt(raw)
    assert excerpt == "denied [REDACTED] end"
    for start in range(0, 40 - 8):
        assert token[start:start + 8] not in excerpt
    monkeypatch.setenv("GH_TOKEN", token)
    assert stage._stderr_excerpt(raw) == "denied [REDACTED] end"



def listing_after_create(verified, shown_on):
    """Releases as listed: the created draft appears on the shown_on-th listing."""
    def listing(calls):
        created = [i for i, a in enumerate(calls) if a[:3] == ("gh", "release", "create")]
        if not created:
            return []
        reads = sum(1 for a in calls[created[0]:] if a[:2] == ("gh", "api")
                    and "releases?" in a[2])
        return [entry(verified.version)] if shown_on and reads >= shown_on else []
    return listing


def test_reserve_tolerates_a_draft_listed_only_on_the_third_read(build, monkeypatch):
    """The first live run failed once on listing lag right after the create."""
    verified = verify(build)
    calls = production_reserve_fake(monkeypatch, verified, listing_after_create(verified, 3))
    sleeps = []
    stage.Registries(sleep=sleeps.append).reserve(verified)
    assert sleeps == [1.0, 2.0]
    assert len([a for a in calls if a[:3] == ("gh", "release", "create")]) == 1
    assert sorted(Path(a[4]).name for a in calls if a[:3] == ("gh", "release", "upload")) == \
        sorted(verified.files)


def test_reserve_refuses_a_draft_never_listed_after_the_bounded_reads(build, monkeypatch):
    verified = verify(build)
    calls = production_reserve_fake(monkeypatch, verified, listing_after_create(verified, 0))
    sleeps = []
    with pytest.raises(ValueError, match="draft release was not created"):
        stage.Registries(sleep=sleeps.append).reserve(verified)
    assert sleeps == list(stage.LISTING_DELAYS)
    assert sum(sleeps) == pytest.approx(60.0)
    create = next(i for i, a in enumerate(calls) if a[:3] == ("gh", "release", "create"))
    assert sum(1 for a in calls[create:] if "releases?" in a[2]) == len(stage.LISTING_DELAYS) + 1
    assert not any(a[:3] == ("gh", "release", "upload") for a in calls)


def test_reserve_readback_waits_for_uploaded_assets_to_be_listed(build):
    verified, registry = verify(build), Registry()
    real = registry.release
    reads = []

    def lagging(tag):
        reads.append(tag)
        found = real(tag)
        if found is not None and len(reads) < 4:
            # The second and third reads (after the upload) list no assets yet.
            return stage.Release(found.draft, {})
        return found

    registry.release = lagging
    sleeps = []
    assert stage.reserve(verified, registry, sleep=sleeps.append) == len(verified.python_hashes)
    assert sleeps == [1.0, 2.0]
    assert registry.writes == ["reserve"]


def test_listing_poll_never_retries_a_refusal():
    reads = []

    def read():
        reads.append(1)
        raise ValueError("GitHub release asset collision")

    with pytest.raises(ValueError, match="collision"):
        stage.poll(read, lambda _value: False, sleep=lambda _s: pytest.fail("slept"))
    assert reads == [1]
