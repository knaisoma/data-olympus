"""New-model promotion compares complete Python payloads and preserves legacy bytes."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import tarfile
import zipfile
from typing import TYPE_CHECKING

import pytest

from scripts import release_artifacts as artifacts

if TYPE_CHECKING:
    from pathlib import Path


def wheel(path: Path, version: str, change: str = "") -> Path:
    info = f"data_olympus-{version}.dist-info"
    entries = {
        "data_olympus/__init__.py": b"VALUE = 1\n",
        f"{info}/METADATA": f"Name: data-olympus\nVersion: {version}\n\nBody\n".encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\n",
    }
    if change == "body":
        entries[f"{info}/METADATA"] += b"Version: forbidden-body-change\n"
    elif change == "code":
        entries["data_olympus/__init__.py"] = b"VALUE = 2\n"
    elif change == "version":
        entries[f"{info}/METADATA"] = b"Name: data-olympus\nVersion: 9.9.9\n\nBody\n"
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, data in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, f"sha256={digest}", len(data)])
    writer.writerow([f"{info}/RECORD", "", ""])
    entries[f"{info}/RECORD"] = record.getvalue().encode()
    if change == "record":
        entries[f"{info}/RECORD"] += b"injected.py,sha256=bad,1\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def sdist(path: Path, version: str, change: str = "") -> Path:
    entries = {
        "PKG-INFO": f"Name: data-olympus\nVersion: {version}\n\nBody\n".encode(),
        "pyproject.toml": f'[project]\nname = "data-olympus"\nversion = "{version}"\n'.encode(),
        "uv.lock": f'[[package]]\nname = "data-olympus"\nversion = "{version}"\n'.encode(),
        "src/data_olympus/__init__.py": b"VALUE = 1\n",
        "LICENSE": b"license\n",
    }
    if change == "code":
        entries["src/data_olympus/__init__.py"] = b"VALUE = 2\n"
    elif change == "project":
        entries["pyproject.toml"] += b'dependencies = ["unexpected"]\n'
    elif change == "body":
        entries["PKG-INFO"] += b"Version: forbidden-body-change\n"
    elif change == "license":
        del entries["LICENSE"]
    with tarfile.open(path, "w:gz") as archive:
        for name, data in entries.items():
            member = tarfile.TarInfo(f"data_olympus-{version}/{name}")
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
    return path


@pytest.mark.parametrize("candidate_version", ["0.6.0rc3", "0.6.0.dev3"])
def test_compare_distributions_accepts_only_version_changes(tmp_path, candidate_version):
    result = artifacts.compare_distributions(
        wheel(tmp_path / "candidate.whl", candidate_version),
        wheel(tmp_path / "stable.whl", "0.6.0"),
        sdist(tmp_path / "candidate.tar.gz", candidate_version),
        sdist(tmp_path / "stable.tar.gz", "0.6.0"),
        candidate_version=candidate_version, stable_version="0.6.0",
    )
    assert result["wheel"]["equivalent"] is True
    assert result["sdist"]["equivalent"] is True
    assert result["sdist"]["files_compared"] == 5


@pytest.mark.parametrize(("kind", "change"), [
    ("wheel", "body"), ("wheel", "code"), ("wheel", "version"), ("wheel", "record"),
    ("sdist", "body"), ("sdist", "code"), ("sdist", "project"), ("sdist", "license"),
])
def test_compare_distributions_rejects_payload_or_identity_drift(tmp_path, kind, change):
    with pytest.raises(ValueError):
        artifacts.compare_distributions(
            wheel(tmp_path / "candidate.whl", "0.6.0rc3"),
            wheel(tmp_path / "stable.whl", "0.6.0", change if kind == "wheel" else ""),
            sdist(tmp_path / "candidate.tar.gz", "0.6.0rc3"),
            sdist(tmp_path / "stable.tar.gz", "0.6.0", change if kind == "sdist" else ""),
            candidate_version="0.6.0rc3", stable_version="0.6.0",
        )


@pytest.fixture
def promotion(tmp_path, monkeypatch):
    candidate_wheel = wheel(tmp_path / "candidate.whl", "0.6.0rc3")
    candidate_sdist = sdist(tmp_path / "candidate.tar.gz", "0.6.0rc3")
    candidate = artifacts.BuildReceipt(
        "0.6.0rc3", "a" * 40, "b" * 64, "c" * 64, candidate_wheel, candidate_sdist,
        artifacts._sha256(candidate_wheel), artifacts._sha256(candidate_sdist),
    )
    stable_wheel = wheel(tmp_path / "stable.whl", "0.6.0")
    stable_sdist = sdist(tmp_path / "stable.tar.gz", "0.6.0")
    stable = artifacts.BuildReceipt(
        "0.6.0", "d" * 40, "b" * 64, "c" * 64, stable_wheel, stable_sdist,
        artifacts._sha256(stable_wheel), artifacts._sha256(stable_sdist),
    )
    record = dict(schema_version=1, H="a" * 40, S="d" * 40, B="e" * 40, M="e" * 40,
                  target="0.6.0", tag="v0.6.0", candidate_tag="0.6.0-rc.3",
                  candidate_version="0.6.0rc3", image_digest="sha256:" + "f" * 64)
    provenance = tmp_path / "candidate.json"
    artifacts.write_provenance(
        artifacts.ReleaseReceipt(candidate.source_sha, candidate), provenance,
    )
    payload = json.loads(provenance.read_text()) | {
        key: record[key] for key in ("H", "B", "M", "candidate_tag", "image_digest")
    } | {"promotable": True, "dry_run": False, "N": 3}
    provenance.write_text(json.dumps(payload))
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(record))
    monkeypatch.setattr(artifacts, "_source_sha", lambda _source: stable.source_sha)
    monkeypatch.setattr(artifacts, "build_distribution", lambda *_args: stable)
    args = ["stable-promotion", "--source", str(tmp_path), "--release-record", str(record_path),
            "--candidate-provenance", str(provenance), "--candidate-wheel", str(candidate.wheel),
            "--candidate-sdist", str(candidate.sdist), "--output", str(tmp_path / "dist"),
            "--provenance", str(tmp_path / "stable.json")]
    return args, record_path, provenance, tmp_path / "stable.json", candidate


def test_promotion_build_maps_reviewed_head_to_squash_and_digest(promotion):
    args, record, _, output, _ = promotion
    assert artifacts.main(args) == 0
    result = json.loads(output.read_text())
    expected = json.loads(record.read_text())
    assert {key: result[key] for key in ("H", "S", "tag", "image_digest")} == {
        key: expected[key] for key in ("H", "S", "tag", "image_digest")
    }
    assert result["candidate"]["source_sha"] == expected["H"]
    assert result["stable"]["source_sha"] == expected["S"]
    assert result["comparison"]["sdist"]["equivalent"] is True


@pytest.mark.parametrize(("key", "value"), [
    ("H", "0" * 40), ("S", "0" * 40), ("B", "0" * 40), ("M", "0" * 40),
    ("tag", "v0.6.1"), ("image_digest", "sha256:" + "0" * 64),
    ("candidate_tag", "0.6.0-rc.4"), ("candidate_version", "0.6.0rc4"),
])
def test_promotion_refuses_mismatched_record(promotion, key, value):
    args, record, _, output, _ = promotion
    payload = json.loads(record.read_text())
    payload[key] = value
    record.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        artifacts.main(args)
    assert not output.exists()


def test_promotion_verifies_candidate_sdist_hash(promotion):
    args, _, _, output, candidate = promotion
    candidate.sdist.write_bytes(b"substituted archive")
    with pytest.raises(ValueError, match="sdist hash"):
        artifacts.main(args)
    assert not output.exists()


@pytest.mark.parametrize(("key", "value"), [("promotable", False), ("dry_run", True), ("N", 0)])
def test_promotion_refuses_nonpromotable_provenance(promotion, key, value):
    args, _, provenance, output, _ = promotion
    payload = json.loads(provenance.read_text())
    payload[key] = value
    provenance.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="promotable"):
        artifacts.main(args)
    assert not output.exists()


@pytest.mark.parametrize("field", ["source_tree_sha256", "lock_sha256", "source_sha"])
def test_promotion_refuses_build_tree_lock_or_commit_drift(promotion, monkeypatch, field):
    args, _, _, output, _ = promotion
    stable = artifacts.build_distribution(None, None, None)
    values = artifacts.asdict(stable)
    values[field] = "0" * len(values[field])
    monkeypatch.setattr(artifacts, "build_distribution",
                        lambda *_args: artifacts.BuildReceipt(**values))
    with pytest.raises(ValueError):
        artifacts.main(args)
    assert not output.exists()


@pytest.mark.parametrize("change", ["duplicate", "symlink", "traversal", "mode"])
def test_sdist_rejects_unsafe_members_and_mode_changes(tmp_path, change):
    candidate = sdist(tmp_path / "candidate.tar.gz", "0.6.0rc3")
    stable = sdist(tmp_path / "stable.tar.gz", "0.6.0")
    with tarfile.open(stable) as archive:
        entries = [(member, archive.extractfile(member).read()) for member in archive]
    member, content = entries[-1]
    if change == "duplicate":
        entries.append((member, content))
    elif change == "symlink":
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        member.size = 0
    elif change == "traversal":
        member.name = "data_olympus-0.6.0/../outside"
    else:
        member.mode = 0o755
    with tarfile.open(stable, "w:gz") as archive:
        for member, content in entries:
            archive.addfile(member, io.BytesIO(content))
    with pytest.raises(ValueError):
        artifacts.compare_distributions(
            wheel(tmp_path / "candidate.whl", "0.6.0rc3"),
            wheel(tmp_path / "stable.whl", "0.6.0"), candidate, stable,
            candidate_version="0.6.0rc3", stable_version="0.6.0",
        )
