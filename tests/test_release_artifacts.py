from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_artifacts.py"


def _module():
    assert SCRIPT.is_file(), "scripts/release_artifacts.py must exist"
    spec = importlib.util.spec_from_file_location("release_artifacts", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_release_artifact_runner_exists() -> None:
    assert SCRIPT.is_file()


def test_candidate_version_maps_public_channels() -> None:
    version = _module().CandidateVersion.from_base("0.6.0", 3)
    assert version.base == "0.6.0"
    assert version.number == 3
    assert version.git_tag == "0.6.0-rc.3"
    assert version.pypi_version == "0.6.0rc3"
    assert version.stable_tag == "v0.6.0"


@pytest.mark.parametrize(("tag", "python_version"), [
    ("0.6.0-hotfix.rc.3", "0.6.0.dev3"),
    ("0.6.0-hotfix.rc.0", "0.6.0.dev0"),
    ("0.6.0-rc.0", "0.6.0rc0"),
    ("0.6.0-rc.3", "0.6.0rc3"),
])
def test_candidate_identity_supports_stage_one(tag: str, python_version: str) -> None:
    version = _module().CandidateVersion.from_tag(tag)
    assert version.git_tag == tag
    assert version.pypi_version == python_version


@pytest.mark.parametrize("tag", ["0.6.0-rc.01", "v0.6.0-rc.1", "0.6.0-rc.-1",
                                "0.6.0-hotfix.rc.1\n", "0.6.0"])
def test_stage_one_identity_rejects_malformed_tag(tag: str) -> None:
    with pytest.raises(ValueError, match="candidate tag"):
        _module().CandidateVersion.from_tag(tag)


def test_prepare_context_injects_hotfix_without_mutating_source(tmp_path: Path) -> None:
    module = _module()
    before = (ROOT / "pyproject.toml").read_bytes()
    output = tmp_path / "context"
    assert module.main(["prepare-context", "--source", str(ROOT), "--output", str(output),
                        "--version", "0.6.0.dev0"]) == 0
    project = tomllib.loads((output / "pyproject.toml").read_text())["project"]
    assert project["version"] == "0.6.0.dev0"
    assert (ROOT / "pyproject.toml").read_bytes() == before
    assert (output / "deploy/docker/Dockerfile").is_file()


@pytest.mark.parametrize(("tag", "number", "dry_run", "promotable"), [
    ("0.6.0-hotfix.rc.3", 3, False, True),
    ("0.6.0-hotfix.rc.0", 0, False, False),
    ("0.6.0-rc.3", 3, True, False),
])
def test_archive_provenance_binds_inputs_and_decision(
    tmp_path: Path, tag: str, number: int, dry_run: bool, promotable: bool,
) -> None:
    module = _module()
    version = module.CandidateVersion.from_tag(tag)
    receipt = module.BuildReceipt(version.pypi_version, "a" * 40, "b" * 64, "c" * 64,
                                  Path("test.whl"), Path("test.tar.gz"), "d" * 64, "e" * 64)
    source = tmp_path / "source.json"
    output = tmp_path / "final.json"
    archive = tmp_path / "image.oci.tar"
    archive.write_bytes(b"oci archive fixture")
    module.write_provenance(module.ReleaseReceipt("a" * 40, receipt), source)
    args = ["finalize-archive", "--provenance", str(source), "--output", str(output),
            "--source-sha", "a" * 40, "--base-sha", "b" * 40, "--main-sha", "c" * 40,
            "--candidate-tag", tag, "--number", str(number), "--oci-archive", str(archive),
            "--image-digest", "sha256:" + "d" * 64,
            "--promotable", str(promotable).lower()]
    args.extend(["--dry-run", str(dry_run).lower()])
    assert module.main(args) == 0
    payload = json.loads(output.read_text())
    assert {key: payload[key] for key in ("B", "H", "M", "N")} == {
        "B": "b" * 40, "H": "a" * 40, "M": "c" * 40, "N": number,
    }
    assert payload["python_version"] == version.pypi_version
    assert payload["candidate_tag"] == tag
    assert payload["oci_archive_sha256"] == hashlib.sha256(b"oci archive fixture").hexdigest()
    assert payload["image_digest"] == "sha256:" + "d" * 64
    assert payload["promotable"] is promotable
    assert payload["dry_run"] is dry_run


@pytest.mark.parametrize("stable", [False, True])
def test_legacy_provenance_bytes_remain_unchanged(tmp_path: Path, stable: bool) -> None:
    module = _module()
    candidate = module.BuildReceipt("0.6.0rc3", "a" * 40, "b" * 64, "c" * 64,
                                   Path("c.whl"), Path("c.tar.gz"), "d" * 64, "e" * 64)
    stable_build = module.BuildReceipt("0.6.0", "a" * 40, "b" * 64, "c" * 64,
                                      Path("s.whl"), Path("s.tar.gz"), "f" * 64, "0" * 64)
    comparison = module.ComparisonReceipt("0.6.0rc3", "0.6.0", "d" * 64, "f" * 64,
                                         "1" * 64, 10, True)
    expected_candidate = dict(version="0.6.0rc3", source_sha="a" * 40,
                              source_tree_sha256="b" * 64, lock_sha256="c" * 64,
                              wheel="c.whl", sdist="c.tar.gz", wheel_sha256="d" * 64,
                              sdist_sha256="e" * 64)
    expected_stable = dict(expected_candidate, version="0.6.0", wheel="s.whl", sdist="s.tar.gz",
                           wheel_sha256="f" * 64, sdist_sha256="0" * 64)
    expected_comparison = dict(candidate_version="0.6.0rc3", stable_version="0.6.0",
                               candidate_wheel_sha256="d" * 64, stable_wheel_sha256="f" * 64,
                               normalized_payload_sha256="1" * 64, files_compared=10,
                               equivalent=True)
    expected = {"source_sha": "a" * 40, "candidate": expected_candidate,
                "stable": expected_stable if stable else None,
                "comparison": expected_comparison if stable else None}
    output = tmp_path / "legacy.json"
    receipt = module.ReleaseReceipt("a" * 40, candidate, stable_build if stable else None,
                                    comparison if stable else None)
    module.write_provenance(receipt, output)
    assert output.read_bytes() == (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode()


@pytest.mark.parametrize(("number", "dry_run"), [(0, "false"), (3, "true")])
def test_archive_provenance_refuses_promotable_cut_or_dry_run(
    tmp_path: Path, number: int, dry_run: str,
) -> None:
    with pytest.raises(ValueError, match="not promotable"):
        _module().main([
            "finalize-archive", "--provenance", str(tmp_path / "unused.json"),
            "--output", str(tmp_path / "final.json"), "--source-sha", "a" * 40,
            "--base-sha", "b" * 40, "--main-sha", "c" * 40,
            "--candidate-tag", f"0.6.0-rc.{number}", "--number", str(number),
            "--oci-archive", str(tmp_path / "image.tar"), "--image-digest", "sha256:" + "d" * 64,
            "--promotable", "true", "--dry-run", dry_run,
        ])


@pytest.mark.parametrize("base", ["0.6", "v0.6.0", "0.6.0rc1", "0.6.0-alpha"])
def test_candidate_version_rejects_malformed_base(base: str) -> None:
    with pytest.raises(ValueError, match="base version"):
        _module().CandidateVersion.from_base(base, 1)


@pytest.mark.parametrize("number", [0, -1])
def test_candidate_version_rejects_nonpositive_number(number: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        _module().CandidateVersion.from_base("0.6.0", number)


@pytest.fixture(scope="module")
def built_pair(tmp_path_factory: pytest.TempPathFactory):
    module = _module()
    output = tmp_path_factory.mktemp("release-artifacts")
    source_pyproject = (ROOT / "pyproject.toml").read_bytes()
    source_lock = (ROOT / "uv.lock").read_bytes()
    candidate = module.build_distribution(ROOT, "0.6.0rc3", output)
    stable = module.build_distribution(ROOT, "0.6.0", output)
    assert (ROOT / "pyproject.toml").read_bytes() == source_pyproject
    assert (ROOT / "uv.lock").read_bytes() == source_lock
    return module, candidate, stable


def test_build_distribution_uses_isolated_version_overlay(built_pair) -> None:
    _module_value, candidate, stable = built_pair
    assert candidate.version == "0.6.0rc3"
    assert stable.version == "0.6.0"
    assert candidate.source_sha == stable.source_sha
    assert candidate.lock_sha256 == stable.lock_sha256
    assert candidate.wheel.is_file() and candidate.sdist.is_file()
    assert stable.wheel.is_file() and stable.sdist.is_file()
    assert "0.6.0rc3" in candidate.wheel.name
    assert "0.6.0" in stable.wheel.name


def test_build_distribution_rejects_missing_source_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "_source_sha", lambda _source: "")
    with pytest.raises(ValueError, match="source SHA"):
        module.build_distribution(ROOT, "0.6.0rc3", tmp_path)


def test_compare_wheels_accepts_version_only_difference(built_pair) -> None:
    module, candidate, stable = built_pair
    receipt = module.compare_wheels(candidate.wheel, stable.wheel)
    assert receipt.equivalent is True
    assert receipt.candidate_version == "0.6.0rc3"
    assert receipt.stable_version == "0.6.0"
    assert receipt.files_compared > 0


def _mutate_wheel(source: Path, output: Path, target: str) -> Path:
    with zipfile.ZipFile(source) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}

    if target == "python":
        name = next(name for name in entries if name == "data_olympus/__init__.py")
        entries[name] += b"\nMUTATED = True\n"
    elif target == "bin":
        name = next(name for name in entries if name == "data_olympus/_bin/kb")
        entries[name] += b"\n# mutated\n"
    elif target == "entry_points":
        name = next(name for name in entries if name.endswith(".dist-info/entry_points.txt"))
        entries[name] += b"\nmutated = data_olympus.cli.main:main\n"
    elif target == "license":
        name = next(name for name in entries if ".dist-info/licenses/LICENSE" in name)
        entries[name] += b"\nmutated\n"
    elif target == "dependency":
        name = next(name for name in entries if name.endswith(".dist-info/METADATA"))
        entries[name] += b"Requires-Dist: unexpected-package>=1\n"
    else:  # pragma: no cover
        raise AssertionError(target)

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output


@pytest.mark.parametrize(
    "target", ["python", "bin", "entry_points", "license", "dependency"]
)
def test_compare_wheels_rejects_nonversion_drift(
    built_pair, tmp_path: Path, target: str,
) -> None:
    module, candidate, stable = built_pair
    changed = _mutate_wheel(stable.wheel, tmp_path / f"{target}.whl", target)
    with pytest.raises(ValueError, match="wheel payload mismatch"):
        module.compare_wheels(candidate.wheel, changed)


def test_write_provenance_emits_machine_readable_receipt(
    built_pair, tmp_path: Path,
) -> None:
    module, candidate, stable = built_pair
    comparison = module.compare_wheels(candidate.wheel, stable.wheel)
    receipt = module.ReleaseReceipt(
        source_sha=candidate.source_sha,
        candidate=candidate,
        stable=stable,
        comparison=comparison,
    )
    output = tmp_path / "release-provenance.json"
    module.write_provenance(receipt, output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["source_sha"] == candidate.source_sha
    assert payload["candidate"]["wheel_sha256"] == candidate.wheel_sha256
    assert payload["stable"]["wheel_sha256"] == stable.wheel_sha256
    assert payload["comparison"]["equivalent"] is True


def test_write_provenance_rejects_inconsistent_receipt(
    built_pair, tmp_path: Path,
) -> None:
    module, candidate, stable = built_pair
    comparison = module.compare_wheels(candidate.wheel, stable.wheel)
    inconsistent = module.ReleaseReceipt(
        source_sha=candidate.source_sha,
        candidate=candidate,
        stable=module.BuildReceipt(
            version=stable.version,
            source_sha="0" * 40,
            source_tree_sha256=stable.source_tree_sha256,
            lock_sha256=stable.lock_sha256,
            wheel=stable.wheel,
            sdist=stable.sdist,
            wheel_sha256=stable.wheel_sha256,
            sdist_sha256=stable.sdist_sha256,
        ),
        comparison=comparison,
    )
    with pytest.raises(ValueError, match="source SHA"):
        module.write_provenance(inconsistent, tmp_path / "invalid.json")


def test_finalize_candidate_provenance_validates_public_coordinates(
    built_pair, tmp_path: Path,
) -> None:
    module, candidate, _stable = built_pair
    source = tmp_path / "candidate.json"
    output = tmp_path / "candidate-final.json"
    module.write_provenance(
        module.ReleaseReceipt(source_sha=candidate.source_sha, candidate=candidate),
        source,
    )

    module.finalize_candidate_provenance(
        source,
        output,
        source_sha=candidate.source_sha,
        candidate_tag="0.6.0-rc.3",
        image_digest="sha256:" + "a" * 64,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["candidate_tag"] == "0.6.0-rc.3"
    assert payload["image_digest"] == "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("source_sha", "candidate_tag", "image_digest", "message"),
    [
        ("0" * 40, "0.6.0-rc.3", "sha256:" + "a" * 64, "source SHA"),
        (None, "0.6.0-rc.4", "sha256:" + "a" * 64, "candidate tag"),
        (None, "0.6.0-rc.3", "latest", "image digest"),
    ],
)
def test_finalize_candidate_provenance_rejects_mismatched_coordinates(
    built_pair,
    tmp_path: Path,
    source_sha: str | None,
    candidate_tag: str,
    image_digest: str,
    message: str,
) -> None:
    module, candidate, _stable = built_pair
    provenance = tmp_path / "candidate.json"
    module.write_provenance(
        module.ReleaseReceipt(source_sha=candidate.source_sha, candidate=candidate),
        provenance,
    )
    with pytest.raises(ValueError, match=message):
        module.finalize_candidate_provenance(
            provenance,
            tmp_path / "final.json",
            source_sha=source_sha or candidate.source_sha,
            candidate_tag=candidate_tag,
            image_digest=image_digest,
        )


def test_candidate_and_stable_cli_create_portable_provenance(
    built_pair, tmp_path: Path,
) -> None:
    module, candidate, _stable = built_pair
    candidate_receipt = tmp_path / "candidate-provenance.json"
    candidate_output = tmp_path / "candidate-dist"
    assert module.main(
        [
            "candidate",
            "--base",
            "0.6.0",
            "--number",
            "3",
            "--source",
            str(ROOT),
            "--output",
            str(candidate_output),
            "--provenance",
            str(candidate_receipt),
        ]
    ) == 0
    candidate_payload = json.loads(candidate_receipt.read_text(encoding="utf-8"))
    assert candidate_payload["candidate"]["version"] == "0.6.0rc3"
    assert candidate_payload["candidate"]["wheel"] == next(candidate_output.glob("*.whl")).name

    stable_receipt = tmp_path / "stable-provenance.json"
    stable_output = tmp_path / "stable-dist"
    assert module.main(
        [
            "stable",
            "--base",
            "0.6.0",
            "--source",
            str(ROOT),
            "--output",
            str(stable_output),
            "--candidate-provenance",
            str(candidate_receipt),
            "--candidate-wheel",
            str(next(candidate_output.glob("*.whl"))),
            "--provenance",
            str(stable_receipt),
        ]
    ) == 0
    stable_payload = json.loads(stable_receipt.read_text(encoding="utf-8"))
    assert stable_payload["source_sha"] == candidate.source_sha
    assert stable_payload["stable"]["version"] == "0.6.0"
    assert stable_payload["comparison"]["equivalent"] is True


def test_stable_cli_rejects_candidate_from_another_source(
    built_pair, tmp_path: Path,
) -> None:
    module, candidate, _stable = built_pair
    receipt = module.ReleaseReceipt(source_sha=candidate.source_sha, candidate=candidate)
    provenance = tmp_path / "wrong-source.json"
    module.write_provenance(receipt, provenance)
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    payload["source_sha"] = "0" * 40
    payload["candidate"]["source_sha"] = "0" * 40
    provenance.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="source SHA"):
        module.main(
            [
                "stable",
                "--base",
                "0.6.0",
                "--source",
                str(ROOT),
                "--output",
                str(tmp_path / "stable"),
                "--candidate-provenance",
                str(provenance),
                "--candidate-wheel",
                str(candidate.wheel),
                "--provenance",
                str(tmp_path / "stable.json"),
            ]
        )
