#!/usr/bin/env python3
"""Build and compare same-source Python release distributions."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

_BASE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_DIST_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:rc[1-9][0-9]*)?")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class CandidateVersion:
    base: str
    number: int
    git_tag: str
    pypi_version: str
    stable_tag: str

    @classmethod
    def from_base(cls, base: str, number: int) -> CandidateVersion:
        if _BASE_VERSION.fullmatch(base) is None:
            raise ValueError("base version must be X.Y.Z without a prefix or suffix")
        if number <= 0:
            raise ValueError("release candidate number must be positive")
        return cls(
            base=base,
            number=number,
            git_tag=f"{base}-rc.{number}",
            pypi_version=f"{base}rc{number}",
            stable_tag=f"v{base}",
        )


@dataclass(frozen=True, slots=True)
class BuildReceipt:
    version: str
    source_sha: str
    source_tree_sha256: str
    lock_sha256: str
    wheel: Path
    sdist: Path
    wheel_sha256: str
    sdist_sha256: str


@dataclass(frozen=True, slots=True)
class ComparisonReceipt:
    candidate_version: str
    stable_version: str
    candidate_wheel_sha256: str
    stable_wheel_sha256: str
    normalized_payload_sha256: str
    files_compared: int
    equivalent: bool


@dataclass(frozen=True, slots=True)
class ReleaseReceipt:
    source_sha: str
    candidate: BuildReceipt
    stable: BuildReceipt | None = None
    comparison: ComparisonReceipt | None = None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tracked_files(source: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "-C", str(source), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if completed.returncode == 0:
        return [
            source / item.decode("utf-8")
            for item in completed.stdout.split(b"\0")
            if item
        ]
    excluded = {".git", ".venv", "dist", "build", "to-delete", "__pycache__"}
    return sorted(
        path
        for path in source.rglob("*")
        if path.is_file() and not any(part in excluded for part in path.relative_to(source).parts)
    )


def _copy_build_inputs(source: Path, destination: Path) -> list[Path]:
    copied: list[Path] = []
    for path in _tracked_files(source):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(relative)
    if Path("pyproject.toml") not in copied:
        raise ValueError("build source must contain a tracked pyproject.toml")
    return copied


def _tree_hash(root: Path, relative_paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(relative_paths):
        path = root / relative
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _source_sha(source: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    sha = completed.stdout.strip()
    return sha if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else ""


def _normalized_lock(lock_path: Path) -> tuple[dict[str, Any], str]:
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    normalized = copy.deepcopy(data)
    roots = [
        package
        for package in normalized.get("package", [])
        if package.get("name") == "data-olympus"
    ]
    if len(roots) != 1:
        raise ValueError("uv.lock must contain exactly one data-olympus root package")
    roots[0]["version"] = "__VERSION__"
    rendered = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return normalized, hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _project_without_version(path: Path) -> tuple[dict[str, Any], str]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    normalized = copy.deepcopy(data)
    project = normalized.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("version"), str):
        raise ValueError("pyproject.toml must declare project.version")
    version = project["version"]
    project["version"] = "__VERSION__"
    return normalized, version


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in root.rglob("*")
        if path.is_file()
    }


def _set_lock_root_version(path: Path, version: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r'(\[\[package\]\]\nname = "data-olympus"\nversion = ")[^"]+("\n)'
    )
    updated, replacements = pattern.subn(rf"\g<1>{version}\g<2>", text)
    if replacements != 1:
        raise ValueError("uv.lock must expose one replaceable data-olympus root version")
    path.write_text(updated, encoding="utf-8")


def _apply_version_overlay(root: Path, version: str) -> str:
    if _DIST_VERSION.fullmatch(version) is None:
        raise ValueError("distribution version must be X.Y.Z or X.Y.ZrcN")
    pyproject = root / "pyproject.toml"
    lock = root / "uv.lock"
    before_project, base_version = _project_without_version(pyproject)
    before_lock, _lock_sha = _normalized_lock(lock)
    before_files = _file_hashes(root)

    completed = subprocess.run(
        ["uv", "version", version, "--frozen", "--offline"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError("uv version overlay failed: " + completed.stderr.strip())
    _set_lock_root_version(lock, version)

    after_project, actual_version = _project_without_version(pyproject)
    after_lock, _after_lock_sha = _normalized_lock(lock)
    if actual_version != version:
        raise ValueError(f"version overlay produced {actual_version!r}, expected {version!r}")
    if before_project != after_project:
        raise ValueError("version overlay changed pyproject content beyond project.version")
    if before_lock != after_lock:
        raise ValueError("version overlay changed uv.lock beyond the root package version")

    after_files = _file_hashes(root)
    changed = {
        path
        for path in before_files.keys() | after_files.keys()
        if before_files.get(path) != after_files.get(path)
    }
    if not changed <= {"pyproject.toml", "uv.lock"}:
        raise ValueError(f"version overlay changed unexpected files: {sorted(changed)}")
    return base_version


def _copy_artifact(source: Path, output: Path) -> Path:
    destination = output / source.name
    if destination.exists() and _sha256(destination) != _sha256(source):
        raise ValueError(f"refusing to replace different existing artifact: {destination}")
    shutil.copy2(source, destination)
    return destination


def build_distribution(source: Path, version: str, output: Path) -> BuildReceipt:
    """Build a wheel and sdist from a version-only isolated source overlay."""
    source = source.resolve()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    scratch = source / "to-delete" / "release-build"
    scratch.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"{version}-", dir=scratch) as temp_dir:
        overlay = Path(temp_dir)
        copied = _copy_build_inputs(source, overlay)
        source_tree_sha256 = _tree_hash(overlay, copied)
        source_sha = _source_sha(source)
        if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
            raise ValueError("distribution build requires an exact git source SHA")
        _apply_version_overlay(overlay, version)
        _normalized, lock_sha256 = _normalized_lock(overlay / "uv.lock")
        built = overlay / "dist"
        completed = subprocess.run(
            ["uv", "build", "--out-dir", str(built)],
            cwd=overlay,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError("distribution build failed: " + completed.stderr.strip())
        wheels = list(built.glob("*.whl"))
        sdists = list(built.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ValueError("distribution build must produce exactly one wheel and one sdist")
        wheel = _copy_artifact(wheels[0], output)
        sdist = _copy_artifact(sdists[0], output)

    return BuildReceipt(
        version=version,
        source_sha=source_sha,
        source_tree_sha256=source_tree_sha256,
        lock_sha256=lock_sha256,
        wheel=wheel,
        sdist=sdist,
        wheel_sha256=_sha256(wheel),
        sdist_sha256=_sha256(sdist),
    )


def _metadata_version(content: bytes) -> str:
    for line in content.decode("utf-8").splitlines():
        if line.startswith("Version: "):
            return line.removeprefix("Version: ").strip()
    raise ValueError("wheel METADATA has no Version field")


def _normalize_dist_info_path(path: str) -> str:
    first, separator, remainder = path.partition("/")
    if first.endswith(".dist-info"):
        stem = first.removesuffix(".dist-info")
        distribution, separator_version, _version = stem.rpartition("-")
        if not distribution or not separator_version:
            raise ValueError(f"malformed dist-info path: {path}")
        first = f"{distribution}-__VERSION__.dist-info"
    return first + (separator + remainder if separator else "")


def _normalize_metadata(content: bytes) -> bytes:
    return b"".join(
        line
        for line in content.splitlines(keepends=True)
        if not line.startswith(b"Version: ")
    )


def _normalize_record(content: bytes) -> bytes:
    rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(content.decode("utf-8"))):
        if not row:
            continue
        row[0] = _normalize_dist_info_path(row[0])
        if row[0].endswith(".dist-info/METADATA"):
            continue
        rows.append(row)
    rows.sort(key=lambda row: row[0])
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _normalized_wheel(path: Path) -> tuple[str, dict[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError("wheel must contain exactly one METADATA file")
        version = _metadata_version(archive.read(metadata_names[0]))
        normalized: dict[str, bytes] = {}
        for name in names:
            if name.endswith("/"):
                continue
            normalized_name = _normalize_dist_info_path(name)
            content = archive.read(name)
            if name.endswith(".dist-info/METADATA"):
                content = _normalize_metadata(content)
            elif name.endswith(".dist-info/RECORD"):
                content = _normalize_record(content)
            if normalized_name in normalized:
                raise ValueError(f"normalized wheel path collision: {normalized_name}")
            normalized[normalized_name] = content
    return version, normalized


def _payload_hash(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def compare_wheels(candidate: Path, stable: Path) -> ComparisonReceipt:
    """Require wheel payload identity after normalizing version-only metadata."""
    candidate_version, candidate_files = _normalized_wheel(candidate)
    stable_version, stable_files = _normalized_wheel(stable)
    if candidate_files != stable_files:
        mismatches = sorted(
            name
            for name in candidate_files.keys() | stable_files.keys()
            if candidate_files.get(name) != stable_files.get(name)
        )
        raise ValueError("wheel payload mismatch: " + ", ".join(mismatches[:20]))
    return ComparisonReceipt(
        candidate_version=candidate_version,
        stable_version=stable_version,
        candidate_wheel_sha256=_sha256(candidate),
        stable_wheel_sha256=_sha256(stable),
        normalized_payload_sha256=_payload_hash(candidate_files),
        files_compared=len(candidate_files),
        equivalent=True,
    )


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return value.name
    return value


def write_provenance(receipt: ReleaseReceipt, path: Path) -> None:
    """Write a stable JSON receipt without mutating any build input."""
    _validate_release_receipt(receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(_jsonable(receipt), indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)


def _validate_release_receipt(receipt: ReleaseReceipt) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", receipt.source_sha) is None:
        raise ValueError("release provenance requires an exact source SHA")
    if receipt.candidate.source_sha != receipt.source_sha:
        raise ValueError("candidate source SHA does not match release provenance")
    if (receipt.stable is None) != (receipt.comparison is None):
        raise ValueError("stable build and comparison receipts must appear together")
    if receipt.stable is None or receipt.comparison is None:
        return
    stable = receipt.stable
    comparison = receipt.comparison
    if stable.source_sha != receipt.source_sha:
        raise ValueError("stable source SHA does not match release provenance")
    if stable.source_tree_sha256 != receipt.candidate.source_tree_sha256:
        raise ValueError("candidate and stable source tree hashes differ")
    if stable.lock_sha256 != receipt.candidate.lock_sha256:
        raise ValueError("candidate and stable normalized lock hashes differ")
    if not comparison.equivalent:
        raise ValueError("wheel comparison must be equivalent")
    if comparison.candidate_wheel_sha256 != receipt.candidate.wheel_sha256:
        raise ValueError("candidate wheel hash does not match comparison")
    if comparison.stable_wheel_sha256 != stable.wheel_sha256:
        raise ValueError("stable wheel hash does not match comparison")


def _receipt_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"candidate provenance has invalid {key!r}")
    return value


def _load_candidate_receipt(path: Path, wheel: Path) -> BuildReceipt:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("candidate provenance must be a JSON object")
    candidate = payload.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("candidate provenance must contain a candidate receipt")
    source_sha = _receipt_string(payload, "source_sha")
    candidate_source_sha = _receipt_string(candidate, "source_sha")
    if source_sha != candidate_source_sha:
        raise ValueError("candidate provenance source SHA fields disagree")
    expected_wheel_hash = _receipt_string(candidate, "wheel_sha256")
    if _sha256(wheel) != expected_wheel_hash:
        raise ValueError("candidate wheel hash does not match provenance")
    return BuildReceipt(
        version=_receipt_string(candidate, "version"),
        source_sha=source_sha,
        source_tree_sha256=_receipt_string(candidate, "source_tree_sha256"),
        lock_sha256=_receipt_string(candidate, "lock_sha256"),
        wheel=wheel,
        sdist=Path(_receipt_string(candidate, "sdist")),
        wheel_sha256=expected_wheel_hash,
        sdist_sha256=_receipt_string(candidate, "sdist_sha256"),
    )


def finalize_candidate_provenance(
    provenance: Path,
    output: Path,
    *,
    source_sha: str,
    candidate_tag: str,
    image_digest: str,
) -> None:
    """Bind a validated candidate receipt to its public Git and OCI coordinates."""
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("candidate provenance must be a JSON object")
    candidate = payload.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("candidate provenance must contain a candidate receipt")
    recorded_source = _receipt_string(payload, "source_sha")
    candidate_source = _receipt_string(candidate, "source_sha")
    if recorded_source != source_sha or candidate_source != source_sha:
        raise ValueError("candidate provenance source SHA does not match checkout")
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("candidate provenance requires an exact source SHA")

    version = _receipt_string(candidate, "version")
    match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)rc([1-9][0-9]*)", version)
    if match is None or candidate_tag != f"{match.group(1)}-rc.{match.group(2)}":
        raise ValueError("candidate tag does not match candidate provenance version")
    if _IMAGE_DIGEST.fullmatch(image_digest) is None:
        raise ValueError("image digest must be an exact sha256 digest")

    for key, expected in (
        ("candidate_tag", candidate_tag),
        ("image_digest", image_digest),
    ):
        current = payload.get(key)
        if current is not None and current != expected:
            raise ValueError(f"candidate provenance has conflicting {key}")
        payload[key] = expected

    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(output)


def _run_candidate(args: argparse.Namespace) -> int:
    version = CandidateVersion.from_base(args.base, args.number)
    candidate = build_distribution(args.source, version.pypi_version, args.output)
    if not candidate.source_sha:
        raise ValueError("candidate builds require an exact git source SHA")
    write_provenance(
        ReleaseReceipt(source_sha=candidate.source_sha, candidate=candidate),
        args.provenance,
    )
    return 0


def _run_stable(args: argparse.Namespace) -> int:
    if _BASE_VERSION.fullmatch(args.base) is None:
        raise ValueError("base version must be X.Y.Z without a prefix or suffix")
    candidate = _load_candidate_receipt(args.candidate_provenance, args.candidate_wheel)
    expected_candidate = re.compile(rf"{re.escape(args.base)}rc[1-9][0-9]*")
    if expected_candidate.fullmatch(candidate.version) is None:
        raise ValueError("candidate provenance version does not match the stable base")
    source_sha = _source_sha(args.source)
    if source_sha != candidate.source_sha:
        raise ValueError("candidate provenance source SHA does not match the checkout")
    stable = build_distribution(args.source, args.base, args.output)
    if stable.source_tree_sha256 != candidate.source_tree_sha256:
        raise ValueError("candidate and stable source tree hashes differ")
    if stable.lock_sha256 != candidate.lock_sha256:
        raise ValueError("candidate and stable normalized lock hashes differ")
    comparison = compare_wheels(candidate.wheel, stable.wheel)
    write_provenance(
        ReleaseReceipt(
            source_sha=source_sha,
            candidate=candidate,
            stable=stable,
            comparison=comparison,
        ),
        args.provenance,
    )
    return 0


def _run_finalize_candidate(args: argparse.Namespace) -> int:
    finalize_candidate_provenance(
        args.provenance,
        args.output,
        source_sha=args.source_sha,
        candidate_tag=args.candidate_tag,
        image_digest=args.image_digest,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_artifacts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    candidate = subparsers.add_parser("candidate")
    candidate.add_argument("--base", required=True)
    candidate.add_argument("--number", required=True, type=int)
    candidate.add_argument("--source", required=True, type=Path)
    candidate.add_argument("--output", required=True, type=Path)
    candidate.add_argument("--provenance", required=True, type=Path)
    candidate.set_defaults(handler=_run_candidate)

    stable = subparsers.add_parser("stable")
    stable.add_argument("--base", required=True)
    stable.add_argument("--source", required=True, type=Path)
    stable.add_argument("--output", required=True, type=Path)
    stable.add_argument("--candidate-provenance", required=True, type=Path)
    stable.add_argument("--candidate-wheel", required=True, type=Path)
    stable.add_argument("--provenance", required=True, type=Path)
    stable.set_defaults(handler=_run_stable)

    finalize = subparsers.add_parser("finalize-candidate")
    finalize.add_argument("--provenance", required=True, type=Path)
    finalize.add_argument("--output", required=True, type=Path)
    finalize.add_argument("--source-sha", required=True)
    finalize.add_argument("--candidate-tag", required=True)
    finalize.add_argument("--image-digest", required=True)
    finalize.set_defaults(handler=_run_finalize_candidate)

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
