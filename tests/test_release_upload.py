import json
import subprocess

import pytest


@pytest.mark.parametrize("remote", [b"different", b"same"])
def test_existing_assets_are_never_replaced(tmp_path, monkeypatch, remote):
    from scripts import release_upload

    artifact = tmp_path / "package.whl"
    artifact.write_bytes(b"same")
    uploads = []

    def run(command, **_kwargs):
        if command[1:3] == ["release", "view"]:
            output = json.dumps({"assets": [{"name": artifact.name}]}).encode()
        elif command[1:3] == ["release", "download"]:
            output = remote
        else:
            uploads.append(command)
            output = b""
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(release_upload.subprocess, "run", run)
    if remote != b"same":
        with pytest.raises(ValueError, match="different bytes"):
            release_upload.upload("v1.0.0", [artifact])
    else:
        release_upload.upload("v1.0.0", [artifact])
    assert uploads == []


def test_checks_all_collisions_before_uploading_missing_files(tmp_path, monkeypatch):
    from scripts import release_upload

    missing = tmp_path / "missing.whl"
    existing = tmp_path / "existing.whl"
    missing.write_bytes(b"missing")
    existing.write_bytes(b"new bytes")
    uploads = []

    def run(command, **_kwargs):
        if command[1:3] == ["release", "view"]:
            output = b'{"assets": [{"name": "existing.whl"}]}'
        elif command[1:3] == ["release", "download"]:
            output = b"published bytes"
        else:
            uploads.append(command)
            output = b""
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(release_upload.subprocess, "run", run)
    with pytest.raises(ValueError, match="different bytes"):
        release_upload.upload("v1.0.0", [missing, existing])
    assert uploads == []


def test_missing_assets_upload_without_replacement(tmp_path, monkeypatch):
    from scripts import release_upload

    artifact = tmp_path / "package.whl"
    artifact.write_bytes(b"new")
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b'{"assets": []}')

    monkeypatch.setattr(release_upload.subprocess, "run", run)
    release_upload.upload("v1.0.0", [artifact])
    assert calls[-1] == ["gh", "release", "upload", "v1.0.0", str(artifact)]


def test_inventory_error_blocks_upload(tmp_path, monkeypatch):
    from scripts import release_upload

    def run(command, **_kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(release_upload.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        release_upload.upload("v1.0.0", [tmp_path / "package.whl"])
