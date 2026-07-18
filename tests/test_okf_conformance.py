from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "okf_conformance.py"
REFERENCE = ROOT / "tests" / "okf" / "reference.json"
FIXTURE = ROOT / "tests" / "okf" / "upstream-sample"


def _module():
    assert SCRIPT.is_file(), "scripts/okf_conformance.py must exist"
    spec = importlib.util.spec_from_file_location("okf_conformance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _reference_payload() -> dict[str, object]:
    return json.loads(REFERENCE.read_text(encoding="utf-8"))


def _write_reference(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_conformance_runner_exists() -> None:
    assert SCRIPT.is_file()


def test_load_reference_accepts_pinned_official_repository() -> None:
    pin = _module().load_reference(REFERENCE)
    assert pin.repository == "https://github.com/GoogleCloudPlatform/knowledge-catalog.git"
    assert pin.commit == "d44368c15e38e7c92481c5992e4f9b5b421a801d"
    assert pin.fixture_path == "okf/bundles/crypto_bitcoin"
    assert pin.license.spdx == "Apache-2.0"


@pytest.mark.parametrize("commit", ["main", "abc123", "g" * 40, "A" * 40])
def test_load_reference_rejects_non_exact_commit(tmp_path: Path, commit: str) -> None:
    payload = _reference_payload()
    payload["commit"] = commit
    with pytest.raises(ValueError, match="40 character lowercase hexadecimal"):
        _module().load_reference(_write_reference(tmp_path, payload))


def test_load_reference_rejects_unofficial_repository(tmp_path: Path) -> None:
    payload = _reference_payload()
    payload["repository"] = "https://example.com/okf.git"
    with pytest.raises(ValueError, match="official Google OKF repository"):
        _module().load_reference(_write_reference(tmp_path, payload))


def test_load_reference_rejects_bad_checksum(tmp_path: Path) -> None:
    payload = _reference_payload()
    payload["fixture_sha256"] = "abc"
    with pytest.raises(ValueError, match="fixture_sha256"):
        _module().load_reference(_write_reference(tmp_path, payload))


def test_load_reference_rejects_missing_license_provenance(tmp_path: Path) -> None:
    payload = _reference_payload()
    payload.pop("license")
    with pytest.raises(ValueError, match="license"):
        _module().load_reference(_write_reference(tmp_path, payload))


def test_verify_fixture_accepts_exact_copy() -> None:
    module = _module()
    module.verify_fixture(module.load_reference(REFERENCE), FIXTURE)


def test_verify_fixture_rejects_drift(tmp_path: Path) -> None:
    copied = tmp_path / "upstream-sample"
    shutil.copytree(FIXTURE, copied)
    concept = copied / "tables" / "blocks.md"
    concept.write_text(concept.read_text(encoding="utf-8") + "\nDrift.\n", encoding="utf-8")
    module = _module()
    with pytest.raises(ValueError, match="fixture checksum drift"):
        module.verify_fixture(module.load_reference(REFERENCE), copied)


def test_data_olympus_consumes_pinned_upstream_fixture() -> None:
    module = _module()
    result = module.consume_upstream(module.load_reference(REFERENCE), ROOT)
    assert result["imported"] == 5
    assert result["indexed"] == 5
    assert result["search_hits"] > 0
    assert result["retrieved_id"]


def test_verify_pin_cli_is_offline_and_succeeds() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "verify-pin"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "verified" in completed.stdout.lower()


def _workflow(name: str) -> dict[str, object]:
    path = ROOT / ".github" / "workflows" / name
    assert path.is_file(), f"missing workflow: {name}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_ci_checks_out_and_consumes_exact_okf_pin() -> None:
    assert "/to-delete/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    workflow = _workflow("ci.yaml")
    steps = workflow["jobs"]["test"]["steps"]
    pin = next(step for step in steps if step.get("id") == "okf-pin")
    assert "tests/okf/reference.json" in pin["run"]
    checkout = next(
        step
        for step in steps
        if step.get("with", {}).get("repository")
        == "GoogleCloudPlatform/knowledge-catalog"
    )
    assert checkout["with"]["ref"] == "${{ steps.okf-pin.outputs.sha }}"
    assert checkout["with"]["path"] == "to-delete/okf-reference"
    commands = "\n".join(str(step.get("run", "")) for step in steps)
    assert "okf_conformance.py verify-pin" in commands
    assert "okf_conformance.py consume-data-olympus" in commands
    assert "okf_conformance.py consume-upstream" in commands


def test_okf_pin_freshness_workflow_only_manages_one_issue() -> None:
    workflow = _workflow("okf-pin-freshness.yml")
    triggers = workflow.get("on", workflow.get(True))
    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers
    assert workflow["permissions"] == {"contents": "read", "issues": "write"}
    script = "\n".join(
        str(step.get("with", {}).get("script", ""))
        for step in workflow["jobs"]["check"]["steps"]
    )
    assert "[automation] OKF reference pin is stale" in script
    assert "github.paginate" in script
    assert "issues.listForRepo" in script
    assert "issues.create" in script
    assert "issues.update" in script
    assert "fs.write" not in script
    assert "git push" not in script
