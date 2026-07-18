from __future__ import annotations

import importlib.util
import json
import sys
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
