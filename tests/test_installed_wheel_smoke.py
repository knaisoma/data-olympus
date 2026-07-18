from __future__ import annotations

import subprocess
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
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1
    smoke = subprocess.run(
        ["uv", "run", "python", str(SCRIPT), "--wheel", str(wheels[0])],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    assert "installed wheel smoke: ok" in smoke.stdout


def test_packaging_workflows_run_clean_installed_wheel_smoke() -> None:
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
        assert "scripts/smoke_installed_wheel.py --wheel" in commands, workflow
