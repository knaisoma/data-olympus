#!/usr/bin/env python3
"""Executable interoperability checks against a pinned official OKF revision."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OFFICIAL_REPOSITORY = "https://github.com/GoogleCloudPlatform/knowledge-catalog.git"
ROOT = Path(__file__).resolve().parents[1]
REFERENCE_PATH = ROOT / "tests" / "okf" / "reference.json"
_LOWER_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class LicensePin:
    spdx: str
    path: str
    local_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ReferencePin:
    repository: str
    commit: str
    fixture_path: str
    local_fixture_path: str
    fixture_sha256: str
    license: LicensePin


def _required_string(data: dict[str, Any], key: str, *, context: str = "pin") -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} requires non-empty string field {key!r}")
    return value


def _relative_path(value: str, *, field: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must be a safe relative path")
    return value


def load_reference(path: Path) -> ReferencePin:
    """Load and validate the immutable upstream repository and fixture pin."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read OKF reference pin: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("OKF reference pin must be a JSON object")

    repository = _required_string(data, "repository")
    if repository != OFFICIAL_REPOSITORY:
        raise ValueError("repository must be the official Google OKF repository")
    commit = _required_string(data, "commit")
    if _LOWER_SHA.fullmatch(commit) is None:
        raise ValueError("commit must be a 40 character lowercase hexadecimal SHA")
    fixture_sha256 = _required_string(data, "fixture_sha256")
    if _SHA256.fullmatch(fixture_sha256) is None:
        raise ValueError("fixture_sha256 must be a 64 character lowercase SHA256")

    license_data = data.get("license")
    if not isinstance(license_data, dict):
        raise ValueError("license provenance must be a JSON object")
    license_sha256 = _required_string(license_data, "sha256", context="license")
    if _SHA256.fullmatch(license_sha256) is None:
        raise ValueError("license sha256 must be a 64 character lowercase SHA256")
    license_pin = LicensePin(
        spdx=_required_string(license_data, "spdx", context="license"),
        path=_relative_path(
            _required_string(license_data, "path", context="license"),
            field="license.path",
        ),
        local_path=_relative_path(
            _required_string(license_data, "local_path", context="license"),
            field="license.local_path",
        ),
        sha256=license_sha256,
    )
    if license_pin.spdx != "Apache-2.0":
        raise ValueError("license SPDX identifier must be Apache-2.0")

    return ReferencePin(
        repository=repository,
        commit=commit,
        fixture_path=_relative_path(
            _required_string(data, "fixture_path"), field="fixture_path"
        ),
        local_fixture_path=_relative_path(
            _required_string(data, "local_fixture_path"),
            field="local_fixture_path",
        ),
        fixture_sha256=fixture_sha256,
        license=license_pin,
    )


def _tree_sha256(root: Path) -> str:
    if not root.is_dir():
        raise ValueError(f"fixture directory does not exist: {root}")
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise ValueError(f"fixture directory is empty: {root}")
    for path in files:
        relative = "./" + path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def verify_fixture(pin: ReferencePin, root: Path) -> None:
    """Fail when a local fixture differs from the content recorded by the pin."""
    actual = _tree_sha256(root)
    if actual != pin.fixture_sha256:
        raise ValueError(
            f"fixture checksum drift: expected {pin.fixture_sha256}, got {actual}"
        )


def _verify_file(path: Path, expected_sha256: str, *, label: str) -> None:
    if not path.is_file():
        raise ValueError(f"{label} file does not exist: {path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError(
            f"{label} checksum drift: expected {expected_sha256}, got {actual}"
        )


def verify_pin(pin: ReferencePin, project_root: Path = ROOT) -> dict[str, object]:
    verify_fixture(pin, project_root / pin.local_fixture_path)
    _verify_file(
        project_root / pin.license.local_path,
        pin.license.sha256,
        label="license",
    )
    return {
        "repository": pin.repository,
        "commit": pin.commit,
        "fixture_sha256": pin.fixture_sha256,
        "license": pin.license.spdx,
        "verified": True,
    }


def _scratch_parent(project_root: Path) -> Path:
    scratch = project_root / "to-delete"
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def consume_data_olympus(
    pin: ReferencePin, project_root: Path, upstream_root: Path
) -> dict[str, object]:
    """Run the pinned upstream reference consumer over the Data Olympus bundle."""
    upstream_root = upstream_root.resolve()
    actual_commit = subprocess.run(
        ["git", "-C", str(upstream_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual_commit != pin.commit:
        raise ValueError(f"upstream checkout must be pinned to {pin.commit}, got {actual_commit}")
    verify_fixture(pin, upstream_root / pin.fixture_path)
    _verify_file(upstream_root / pin.license.path, pin.license.sha256, label="upstream license")

    source_root = upstream_root / "okf" / "src"
    bundle_root = project_root / "example-bundle"
    expected = sum(
        1 for path in bundle_root.rglob("*.md") if path.name != "index.md"
    )
    code = """
import json
import sys
from pathlib import Path
from reference_agent.viewer.generator import generate_visualization

result = generate_visualization(Path(sys.argv[1]), Path(sys.argv[2]))
print(json.dumps(result, sort_keys=True))
"""
    with tempfile.TemporaryDirectory(
        prefix="okf-reference-consumer-", dir=_scratch_parent(project_root)
    ) as temp_dir:
        output = Path(temp_dir) / "data-olympus.html"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(source_root), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        completed = subprocess.run(
            [sys.executable, "-c", code, str(bundle_root), str(output)],
            cwd=upstream_root / "okf",
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError(
                "pinned upstream consumer failed: " + completed.stderr.strip()
            )
        result = json.loads(completed.stdout)
        if result.get("concepts") != expected or not output.is_file():
            raise ValueError(
                "pinned upstream consumer did not consume every Data Olympus concept"
            )
    return {
        "upstream_commit": actual_commit,
        "concepts": result["concepts"],
        "edges": result["edges"],
        "verified": True,
    }


def consume_upstream(pin: ReferencePin, project_root: Path) -> dict[str, object]:
    """Import, lint, index, search, and retrieve the pinned upstream fixture."""
    from data_olympus.format import lint_bundle
    from data_olympus.importer import run_import
    from data_olympus.index import Index
    from data_olympus.tools_read import kb_get_fn

    source = project_root / pin.local_fixture_path
    verify_fixture(pin, source)
    with tempfile.TemporaryDirectory(
        prefix="okf-data-olympus-consumer-", dir=_scratch_parent(project_root)
    ) as temp_dir:
        temp_root = Path(temp_dir)
        normalized = temp_root / "normalized"
        report = run_import(
            source=source,
            kind="okf",
            tier="T3",
            out=normalized,
        )
        if not report.lint_clean or any(
            finding.severity == "error"
            for findings in lint_bundle(normalized).values()
            for finding in findings
        ):
            raise ValueError("Data Olympus did not lint the normalized upstream fixture")

        index = Index(temp_root / "index.db")
        build = index.build(normalized, source_commit=pin.commit)
        hits = index.search("bitcoin", limit=10)
        if not hits:
            raise ValueError("Data Olympus search returned no pinned upstream concepts")
        retrieved = kb_get_fn(idx=index, id=hits[0].id)
    return {
        "imported": len(report.created),
        "indexed": build.docs_indexed,
        "search_hits": len(hits),
        "retrieved_id": retrieved.id,
        "verified": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify-pin", help="verify the vendored fixture offline")
    data_olympus = subparsers.add_parser(
        "consume-data-olympus",
        help="run the pinned upstream consumer over example-bundle",
    )
    data_olympus.add_argument("--upstream-root", type=Path, required=True)
    subparsers.add_parser(
        "consume-upstream",
        help="run Data Olympus over the vendored pinned upstream fixture",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    pin = load_reference(REFERENCE_PATH)
    if args.command == "verify-pin":
        result = verify_pin(pin)
    elif args.command == "consume-data-olympus":
        result = consume_data_olympus(pin, ROOT, args.upstream_root)
    else:
        result = consume_upstream(pin, ROOT)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
