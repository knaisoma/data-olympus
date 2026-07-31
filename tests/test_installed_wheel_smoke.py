from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke_installed_wheel.py"
WORKFLOWS = ROOT / ".github" / "workflows"


def test_installed_wheel_smoke_runner_exists() -> None:
    assert SCRIPT.is_file()


def test_clean_installed_wheel_smoke(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--out-dir", str(dist)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    for artifact in (wheels[0], sdists[0]):
        smoke = subprocess.run(
            [
                "uv", "run", "python", str(SCRIPT),
                "--artifact", str(artifact), "--expected-version", version,
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert smoke.returncode == 0, smoke.stdout + smoke.stderr
        assert "installed artifact smoke: ok" in smoke.stdout


def test_packaging_workflows_smoke_both_artifacts_at_the_expected_version() -> None:
    jobs = {
        "publish-pypi-reusable.yml": "build",
        "rc-publish.yml": "build-python",
        "tag-release.yml": "publish-pypi",
    }
    for workflow, job in jobs.items():
        doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
        commands = "\n".join(
            str(step.get("run", "")) for step in doc["jobs"][job]["steps"]
        )
        assert "scripts/smoke_installed_wheel.py" in commands, workflow
        assert "dist/*.whl dist/*.tar.gz" in commands, workflow
        assert "--artifact" in commands, workflow
        assert "--expected-version" in commands, workflow
