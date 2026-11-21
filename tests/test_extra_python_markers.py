"""The published metadata gates fastembed below Python 3.15.

onnxruntime (pulled in by fastembed) publishes no cp315 wheels, so the
`embeddings` extra and the fastembed entry of the `dev` extra carry
`python_version < "3.15"`. This reads `Requires-Dist` from the METADATA of a
freshly built wheel, the bytes installers actually see, and evaluates each
fastembed requirement per interpreter line and extra.
"""

from __future__ import annotations

import subprocess
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def requires_dist(tmp_path_factory: pytest.TempPathFactory) -> list[Requirement]:
    out = tmp_path_factory.mktemp("wheel")
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=ROOT, check=False, capture_output=True, text=True, timeout=240,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    (wheel,) = out.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        (name,) = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        metadata = Parser().parsestr(archive.read(name).decode("utf-8"))
    return [Requirement(line) for line in metadata.get_all("Requires-Dist") or []]


def _fastembed_applies(reqs: list[Requirement], extra: str, python: str) -> bool:
    env = {"python_version": python, "python_full_version": f"{python}.0", "extra": extra}
    return any(
        req.name == "fastembed" and req.marker is not None and req.marker.evaluate(env)
        for req in reqs
    )


@pytest.mark.parametrize("extra", ["embeddings", "dev"])
@pytest.mark.parametrize(("python", "applies"), [("3.13", True), ("3.14", True), ("3.15", False)])
def test_fastembed_is_gated_below_python_3_15(
    requires_dist: list[Requirement], extra: str, python: str, applies: bool,
) -> None:
    assert _fastembed_applies(requires_dist, extra, python) is applies


def test_fastembed_is_never_a_core_dependency(requires_dist: list[Requirement]) -> None:
    for python in ("3.13", "3.14", "3.15"):
        assert not _fastembed_applies(requires_dist, "", python)
        assert not _fastembed_applies(requires_dist, "metrics", python)
    fastembed = [req for req in requires_dist if req.name == "fastembed"]
    assert fastembed and all(req.marker is not None for req in fastembed)
