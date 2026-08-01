"""Live evidence and mutation boundaries for the Data Olympus release routine."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from scripts.ci_status import evaluate as evaluate_ci_status
from scripts.compute_release import next_rc_number

if TYPE_CHECKING:
    from datetime import date

GATEWAY_URL = "http://fastmcp-gateway.mcp-gateway.apps.172.30.1.2.nip.io/mcp"
DATA_OLYMPUS_URL = (
    "http://data-olympus-mcp.data-olympus.apps.172.30.1.2.nip.io"
)
IMAGE_REPOSITORY = "ghcr.io/knaisoma/data-olympus"
GITHUB_OWNER = "knaisoma"
GITHUB_REPOSITORY = "data-olympus"

REQUIRED_PULL_REQUEST_CHECKS = {
    "Analyze (actions)",
    "Analyze (javascript-typescript)",
    "Analyze (python)",
    "CodeQL",
    "changelog-guard",
    "doc-consistency-guard",
    "lint-title",
    "test",
    "version-free-guard",
}
REQUIRED_MAIN_CHECKS = {
    "Analyze (actions)",
    "Analyze (javascript-typescript)",
    "Analyze (python)",
    "doc-consistency-guard",
    "test",
}

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PULL_REQUEST_URL = re.compile(
    r"^https://github[.]com/knaisoma/data-olympus/pull/([1-9][0-9]*)$"
)

CommandOutput = Callable[[list[str], Path, int], str]
JsonFetch = Callable[[str, int], dict[str, Any]]
AuthorityConsult = Callable[[dict[str, object]], dict[str, Any]]
AuthorityGateCheck = Callable[[dict[str, object]], dict[str, Any]]

MAX_AUTHORITY_CONSULT_AGE_SECONDS = 300.0
MAX_AUTHORITY_CLOCK_SKEW_SECONDS = 5.0


class ReleaseDeliveryError(ValueError):
    """A release failure after externally visible side effects."""

    external_state_changed = True

    def __init__(self, reason: str, evidence: dict[str, Any]) -> None:
        super().__init__(reason)
        self.evidence = evidence


def _default_command_output(command: list[str], cwd: Path, timeout: int) -> str:
    process = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if process.returncode != 0:
        raise ValueError(f"command failed: {command[0]}")
    return process.stdout.strip()


def _default_json_fetch(url: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "data-olympus-release"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise ValueError("public release evidence is unavailable") from error
    return _object(payload, "public release evidence")


def _object(value: Any, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{name} must be an object")
    return value


def _positive_finite_number(value: Any) -> float | None:
    if type(value) not in {int, float}:
        return None
    number = float(value)
    return number if number > 0 and math.isfinite(number) else None


def _parse_nested_json(value: Any) -> Any:
    current = value
    for _ in range(4):
        if type(current) is not str:
            break
        try:
            current = json.loads(current)
        except json.JSONDecodeError:
            break
    return current


def _data_olympus_call(
    target: str,
    arguments: dict[str, object],
    run_command: CommandOutput,
) -> dict[str, Any]:
    command = [
        "fastmcp",
        "call",
        "--server-spec",
        f"{DATA_OLYMPUS_URL}/mcp",
        "--target",
        target,
        "--input-json",
        json.dumps(arguments, separators=(",", ":"), sort_keys=True),
        "--json",
        "--timeout",
        "30",
        "--auth",
        "none",
    ]
    raw = run_command(command, Path.cwd(), 40)
    try:
        envelope = _object(json.loads(raw), "Data Olympus MCP response")
    except json.JSONDecodeError as error:
        raise ValueError("Data Olympus MCP returned invalid JSON") from error
    is_error = envelope.get("is_error")
    if type(is_error) is not bool or is_error:
        raise ValueError(f"Data Olympus MCP {target} failed")
    return _object(
        envelope.get("structured_content"),
        "Data Olympus MCP structured content",
    )


def _authority_consult(
    arguments: dict[str, object],
    run_command: CommandOutput = _default_command_output,
) -> dict[str, Any]:
    return _data_olympus_call("kb_consult", arguments, run_command)


def _authority_gate_check(
    arguments: dict[str, object],
    run_command: CommandOutput = _default_command_output,
) -> dict[str, Any]:
    return _data_olympus_call("kb_gate_check", arguments, run_command)


class FastMCPGateway:
    """Call external systems through the local FastMCP gateway."""

    def __init__(
        self,
        gateway_url: str = GATEWAY_URL,
        *,
        run_command: CommandOutput = _default_command_output,
    ) -> None:
        if not gateway_url.startswith(("http://", "https://")):
            raise ValueError("FastMCP gateway URL is invalid")
        self.gateway_url = gateway_url
        self.run_command = run_command

    def execute(self, name: str, arguments: dict[str, object]) -> Any:
        command = [
            "fastmcp",
            "call",
            "--server-spec",
            self.gateway_url,
            "--target",
            "execute_tool",
            "--input-json",
            json.dumps(
                {"arguments": arguments, "tool_name": name},
                separators=(",", ":"),
                sort_keys=True,
            ),
            "--json",
            "--timeout",
            "30",
            "--auth",
            "none",
        ]
        raw = self.run_command(command, Path.cwd(), 40)
        try:
            envelope = _object(json.loads(raw), "FastMCP response")
        except json.JSONDecodeError as error:
            raise ValueError("FastMCP returned invalid JSON") from error
        if envelope.get("is_error") is True:
            raise ValueError("FastMCP returned an error result")
        structured = envelope.get("structured_content")
        used_text_content = structured is None
        if structured is None:
            content = envelope.get("content")
            if type(content) is not list or len(content) != 1:
                raise ValueError("FastMCP content must contain one item")
            item = _object(content[0], "FastMCP content item")
            if item.get("type") != "text" or type(item.get("text")) is not str:
                raise ValueError("FastMCP content item must be text")
            result = _parse_nested_json(item["text"])
        else:
            structured_object = _object(
                structured,
                "FastMCP structured content",
            )
            if "result" not in structured_object:
                raise ValueError("FastMCP structured content omitted result")
            result = _parse_nested_json(structured_object["result"])
        if used_text_content and not (
            type(result) is dict and set(result) >= {"tool", "result"}
        ):
            raise ValueError("FastMCP text content omitted gateway result")
        if type(result) is dict and set(result) >= {"tool", "result"}:
            if result["tool"] != name:
                raise ValueError("FastMCP result tool does not match request")
            result = _parse_nested_json(result["result"])
        return result


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _release_sections(changes: dict[str, Any]) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    for key, title in (
        ("breaking", "Breaking changes"),
        ("features", "New features"),
        ("fixes", "Fixed"),
    ):
        raw_items = changes.get(key)
        if type(raw_items) is not list or any(type(item) is not str for item in raw_items):
            raise ValueError(f"release changes.{key} must be an array of strings")
        items = [item.strip() for item in raw_items if item.strip()]
        if items:
            sections.append((title, items))
    if not sections:
        raise ValueError("release changes must contain at least one item")
    return sections


def render_release_documents(
    repository_root: Path,
    version: str,
    changes: dict[str, Any],
    release_date: date,
) -> dict[str, str]:
    """Render the one deterministic release commit from computed changes."""
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ValueError("release version must be X.Y.Z")
    sections = _release_sections(changes)
    pyproject_path = repository_root / "pyproject.toml"
    pyproject = pyproject_path.read_text(encoding="utf-8")
    updated_project, replacements = re.subn(
        r'(?m)^(version = ")[^"]+("\s*)$',
        rf"\g<1>{version}\g<2>",
        pyproject,
        count=1,
    )
    if replacements != 1:
        raise ValueError("pyproject.toml must contain one project version")
    pyproject_path.write_text(updated_project, encoding="utf-8")

    changelog_path = repository_root / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8")
    marker = "## [Unreleased]"
    if changelog.count(marker) != 1:
        raise ValueError("CHANGELOG.md must contain one Unreleased section")
    changelog_sections = "\n".join(
        f"### {title}\n\n" + "\n".join(f"* {item}" for item in items)
        for title, items in sections
    )
    replacement = (
        f"{marker}\n\n## [{version}] - {release_date.isoformat()}\n\n"
        f"{changelog_sections}\n"
    )
    updated_changelog = changelog.replace(marker, replacement, 1)
    changelog_path.write_text(updated_changelog, encoding="utf-8")

    note_sections = "\n\n".join(
        f"## {title}\n\n" + "\n".join(f"* {item}" for item in items)
        for title, items in sections
    )
    release_note = f"# data-olympus {version}\n\n{note_sections}\n"
    note_path = repository_root / "docs" / "releases" / f"v{version}.md"
    if note_path.exists():
        raise ValueError(f"release note already exists for {version}")
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(release_note, encoding="utf-8")
    note_hash = _sha256_text(release_note)
    return {
        "changelog_hash": _sha256_text(updated_changelog),
        "content_hash": note_hash,
        "release_note_hash": note_hash,
    }


def completed_check_evidence(
    payload: dict[str, Any],
    *,
    required: set[str],
) -> dict[str, Any]:
    """Require an exact complete GitHub check set, allowing optional skips."""
    check_runs = payload.get("check_runs")
    if type(check_runs) is not list or any(type(item) is not dict for item in check_runs):
        raise ValueError("GitHub check runs response is invalid")
    evidence = evaluate_ci_status(check_runs, sorted(required))
    if not evidence["all_success"]:
        raise ValueError("GitHub check runs did not succeed")
    return evidence


def _asset_names(release: dict[str, Any]) -> set[str]:
    assets = release.get("assets")
    if type(assets) is not list:
        raise ValueError("GitHub release assets are unavailable")
    names: set[str] = set()
    for raw_asset in assets:
        asset = _object(raw_asset, "GitHub release asset")
        name = asset.get("name")
        if type(name) is not str or not name:
            raise ValueError("GitHub release asset name is unavailable")
        names.add(name)
    return names


def candidate_release_evidence(
    release: dict[str, Any],
    provenance: dict[str, Any],
    *,
    source_revision: str,
    version: str,
    candidate_tag: str,
    registry_digest: str,
) -> dict[str, Any]:
    """Bind the complete public candidate transaction to one source and digest."""
    if type(source_revision) is not str or _SHA40.fullmatch(source_revision) is None:
        raise ValueError("candidate source revision is invalid")
    if type(registry_digest) is not str or _DIGEST.fullmatch(registry_digest) is None:
        raise ValueError("candidate registry digest is invalid")
    if (
        release.get("tag_name") != candidate_tag
        or release.get("target_commitish") != source_revision
        or release.get("draft") is not False
        or release.get("prerelease") is not True
    ):
        raise ValueError("GitHub candidate release identity does not match")
    candidate = _object(provenance.get("candidate"), "candidate provenance")
    expected_python_version = version + "rc" + candidate_tag.rsplit(".", 1)[-1]
    if (
        provenance.get("source_sha") != source_revision
        or provenance.get("candidate_tag") != candidate_tag
        or provenance.get("image_digest") != registry_digest
        or candidate.get("source_sha") != source_revision
        or candidate.get("version") != expected_python_version
    ):
        raise ValueError("candidate provenance identity does not match")
    wheel = candidate.get("wheel")
    sdist = candidate.get("sdist")
    if type(wheel) is not str or type(sdist) is not str:
        raise ValueError("candidate provenance artifact names are unavailable")
    required_assets = {wheel, sdist, "release-provenance.json"}
    if not required_assets.issubset(_asset_names(release)):
        raise ValueError("GitHub candidate release is incomplete")
    return {
        "candidate_tag": candidate_tag,
        "image_digest": registry_digest,
        "pypi_version": expected_python_version,
        "source_revision": source_revision,
    }


def stable_release_evidence(
    release: dict[str, Any],
    provenance: dict[str, Any],
    pypi: dict[str, Any],
    *,
    source_revision: str,
    version: str,
) -> dict[str, Any]:
    """Bind stable GitHub and PyPI artifacts to the reviewed source."""
    if type(source_revision) is not str or _SHA40.fullmatch(source_revision) is None:
        raise ValueError("stable source revision is invalid")
    if (
        release.get("tag_name") != f"v{version}"
        or release.get("target_commitish") != source_revision
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or provenance.get("source_sha") != source_revision
    ):
        raise ValueError("stable GitHub release identity does not match")
    stable = _object(provenance.get("stable"), "stable provenance")
    if stable.get("source_sha") != source_revision or stable.get("version") != version:
        raise ValueError("stable provenance identity does not match")
    wheel = stable.get("wheel")
    sdist = stable.get("sdist")
    if type(wheel) is not str or type(sdist) is not str:
        raise ValueError("stable provenance artifact names are unavailable")
    required_assets = {wheel, sdist, "release-provenance.json"}
    if not required_assets.issubset(_asset_names(release)):
        raise ValueError("stable GitHub release is incomplete")
    info = _object(pypi.get("info"), "PyPI info")
    urls = pypi.get("urls")
    if info.get("version") != version or type(urls) is not list:
        raise ValueError("stable PyPI release identity does not match")
    remote: dict[str, str] = {}
    for raw_url in urls:
        item = _object(raw_url, "PyPI artifact")
        filename = item.get("filename")
        digests = _object(item.get("digests"), "PyPI artifact digests")
        digest = digests.get("sha256")
        if type(filename) is str and type(digest) is str:
            remote[filename] = digest
    for name, field in ((wheel, "wheel_sha256"), (sdist, "sdist_sha256")):
        expected_hash = stable.get(field)
        if (
            type(expected_hash) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
            or remote.get(name) != expected_hash
        ):
            raise ValueError("stable PyPI artifact hash does not match provenance")
    return {
        "pypi_version": version,
        "source_revision": source_revision,
    }


def _yaml_object(value: Any, name: str) -> dict[str, Any]:
    if type(value) is str:
        try:
            value = yaml.safe_load(value)
        except yaml.YAMLError as error:
            raise ValueError(f"{name} is invalid YAML") from error
    return _object(value, name)


def deployment_state(value: Any) -> dict[str, Any]:
    """Extract exact digest and rollout facts from the live StatefulSet."""
    manifest = _yaml_object(value, "StatefulSet")
    if manifest.get("apiVersion") != "apps/v1" or manifest.get("kind") != "StatefulSet":
        raise ValueError("live deployment is not the Data Olympus StatefulSet")
    metadata = _object(manifest.get("metadata"), "StatefulSet metadata")
    if (
        metadata.get("name") != "data-olympus-mcp"
        or metadata.get("namespace") != "data-olympus"
    ):
        raise ValueError("live deployment identity does not match Data Olympus")
    annotations = _object(metadata.get("annotations"), "StatefulSet annotations")
    if annotations.get("keel.sh/policy") != "never":
        raise ValueError("Data Olympus Keel policy must be never")
    spec = _object(manifest.get("spec"), "StatefulSet spec")
    pod_spec = _object(
        _object(_object(spec.get("template"), "pod template").get("spec"), "pod spec"),
        "pod spec",
    )
    images: dict[str, str] = {}
    for collection_name in ("initContainers", "containers"):
        collection = pod_spec.get(collection_name)
        if type(collection) is not list:
            raise ValueError(f"{collection_name} must be an array")
        for raw_container in collection:
            container = _object(raw_container, "container")
            name = container.get("name")
            if name in {"prepare-git", "data-olympus-mcp"}:
                image = container.get("image")
                if type(image) is not str or "@" not in image:
                    raise ValueError(f"{name} image must be digest pinned")
                repository, digest = image.rsplit("@", 1)
                if repository != IMAGE_REPOSITORY or _DIGEST.fullmatch(digest) is None:
                    raise ValueError(f"{name} image is invalid")
                images[name] = digest
    if set(images) != {"prepare-git", "data-olympus-mcp"}:
        raise ValueError("both Data Olympus containers must be present")
    if len(set(images.values())) != 1:
        raise ValueError("both Data Olympus containers must use the same exact digest")
    status = _object(manifest.get("status", {}), "StatefulSet status")
    desired = spec.get("replicas", 1)
    rollout_complete = (
        type(desired) is int
        and desired > 0
        and status.get("readyReplicas") == desired
        and status.get("updatedReplicas") == desired
        and status.get("replicas") == desired
        and (
            status.get("currentRevision") is None
            or status.get("currentRevision") == status.get("updateRevision")
        )
    )
    return {
        "digest": next(iter(images.values())),
        "keel_policy": "never",
        "rollout_complete": rollout_complete,
    }


def deployment_manifest_for_digest(
    live: dict[str, Any],
    digest: str,
) -> dict[str, Any]:
    """Build an apply-safe StatefulSet document with both images pinned."""
    if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
        raise ValueError("deployment digest must be an OCI SHA256 digest")
    manifest = deepcopy(_object(live, "StatefulSet"))
    if manifest.get("apiVersion") != "apps/v1" or manifest.get("kind") != "StatefulSet":
        raise ValueError("live deployment is not the Data Olympus StatefulSet")
    metadata = _object(manifest.get("metadata"), "StatefulSet metadata")
    if (
        metadata.get("name") != "data-olympus-mcp"
        or metadata.get("namespace") != "data-olympus"
    ):
        raise ValueError("live deployment identity does not match Data Olympus")
    for field in (
        "creationTimestamp",
        "generation",
        "managedFields",
        "resourceVersion",
        "selfLink",
        "uid",
    ):
        metadata.pop(field, None)
    annotations = _object(metadata.setdefault("annotations", {}), "annotations")
    annotations["keel.sh/policy"] = "never"
    manifest.pop("status", None)

    spec = _object(manifest.get("spec"), "StatefulSet spec")
    volume_claims = spec.get("volumeClaimTemplates", [])
    if type(volume_claims) is not list:
        raise ValueError("volumeClaimTemplates must be an array")
    for raw_claim in volume_claims:
        _object(raw_claim, "volume claim template").pop("status", None)

    pod_spec = _object(
        _object(spec.get("template"), "pod template").get("spec"),
        "pod spec",
    )
    expected_image = f"{IMAGE_REPOSITORY}@{digest}"
    matched = set()
    for collection_name, expected_name in (
        ("initContainers", "prepare-git"),
        ("containers", "data-olympus-mcp"),
    ):
        collection = pod_spec.get(collection_name)
        if type(collection) is not list:
            raise ValueError(f"{collection_name} must be an array")
        for raw_container in collection:
            container = _object(raw_container, "container")
            if container.get("name") == expected_name:
                container["image"] = expected_image
                matched.add(expected_name)
    if matched != {"prepare-git", "data-olympus-mcp"}:
        raise ValueError("both Data Olympus containers must be present")
    return manifest


class ReleaseRuntime:
    """Collect live release evidence from the admitted managed worktree."""

    def __init__(
        self,
        repository_root: Path,
        *,
        gateway: FastMCPGateway,
        command_output: CommandOutput = _default_command_output,
        authority_consult: AuthorityConsult = _authority_consult,
        authority_gate_check: AuthorityGateCheck = _authority_gate_check,
        fetch_json: JsonFetch = _default_json_fetch,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], dt.date] = dt.date.today,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.gateway = gateway
        self.command_output = command_output
        self.authority_consult = authority_consult
        self.authority_gate_check = authority_gate_check
        self.fetch_json = fetch_json
        self.sleep = sleep
        self.today = today
        self.clock = clock
        self._heartbeat: Callable[[], None] = lambda: None
        self._prepared: dict[str, Any] | None = None
        self._delivery: dict[str, Any] | None = None

    def set_heartbeat(self, heartbeat: Callable[[], None]) -> None:
        self._heartbeat = heartbeat

    def heartbeat(self) -> None:
        self._heartbeat()

    def command(self, arguments: list[str], timeout: int = 60) -> str:
        self.heartbeat()
        if self.command_output is not _default_command_output:
            result = self.command_output(arguments, self.repository_root, timeout)
            self.heartbeat()
            return result.strip()
        process = subprocess.Popen(
            arguments,
            cwd=self.repository_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        started = time.monotonic()
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise subprocess.TimeoutExpired(arguments, timeout)
            try:
                stdout, _stderr = process.communicate(timeout=min(20.0, remaining))
                break
            except subprocess.TimeoutExpired:
                self.heartbeat()
        self.heartbeat()
        if process.returncode != 0:
            raise ValueError(f"command failed: {arguments[0]}")
        return stdout.strip()

    def gateway_object(self, name: str, arguments: dict[str, object]) -> dict[str, Any]:
        self.heartbeat()
        result = self.gateway.execute(name, arguments)
        self.heartbeat()
        return _object(result, f"{name} response")

    def live_statefulset(self) -> dict[str, Any]:
        self.heartbeat()
        result = self.gateway.execute(
            "k8s_kndev_resources_get",
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "name": "data-olympus-mcp",
                "namespace": "data-olympus",
            },
        )
        self.heartbeat()
        return _yaml_object(result, "live Data Olympus StatefulSet")

    def source_revision(self) -> str:
        revision = self.command(["git", "rev-parse", "HEAD"])
        if _SHA40.fullmatch(revision) is None:
            raise ValueError("managed worktree source revision is invalid")
        return revision

    def candidate_revision(self) -> str:
        revision = self.command(["git", "rev-parse", "origin/main"])
        if _SHA40.fullmatch(revision) is None:
            raise ValueError("remote main revision is invalid")
        return revision

    def collect_admission(self, run_input: dict[str, Any]) -> dict[str, Any]:
        admitted = run_input.get("source_revision")
        if type(admitted) is not str or _SHA40.fullmatch(admitted) is None:
            raise ValueError("admitted source revision is invalid")
        self.command(["git", "fetch", "--tags", "origin", "main"], timeout=120)
        if self.source_revision() != admitted:
            raise ValueError("managed worktree changed after admission")
        remote_main = self.candidate_revision()
        if remote_main != admitted:
            raise ValueError("remote main changed after admission")
        raw_computation = self.command(
            [
                "uv",
                "run",
                "--python",
                "3.13",
                "python",
                "scripts/compute_release.py",
            ],
            timeout=120,
        )
        try:
            computation = _object(
                json.loads(raw_computation),
                "release computation",
            )
        except json.JSONDecodeError as error:
            raise ValueError("release computation returned invalid JSON") from error
        return {
            "branch": "main",
            "source_revision": admitted,
            "remote_main_revision": remote_main,
            "computed_release": computation,
        }

    def _wait_pull_request(self, number: int, head_revision: str) -> dict[str, Any]:
        last_error = "pull request checks did not appear"
        for _attempt in range(120):
            checks = self.gateway_object(
                "pull_request_read",
                {
                    "method": "get_check_runs",
                    "owner": GITHUB_OWNER,
                    "repo": GITHUB_REPOSITORY,
                    "pullNumber": number,
                },
            )
            try:
                check_evidence = completed_check_evidence(
                    checks,
                    required=REQUIRED_PULL_REQUEST_CHECKS,
                )
            except ValueError as error:
                last_error = str(error)
                runs = checks.get("check_runs")
                if type(runs) is list and any(
                    type(run) is dict
                    and run.get("status") == "completed"
                    and run.get("conclusion") not in {"success", "skipped"}
                    for run in runs
                ):
                    raise
                self.sleep(10)
                self.heartbeat()
                continue
            pull = self.gateway_object(
                "pull_request_read",
                {
                    "method": "get",
                    "owner": GITHUB_OWNER,
                    "repo": GITHUB_REPOSITORY,
                    "pullNumber": number,
                },
            )
            head = _object(pull.get("head"), "pull request head")
            if (
                head.get("sha") != head_revision
                or pull.get("draft") is not False
                or pull.get("state") != "open"
                or pull.get("mergeable_state") not in {"clean", "unstable"}
            ):
                raise ValueError("release pull request identity or merge state changed")
            return {"checks": check_evidence, "pull": pull}
        raise ValueError(last_error)

    def _merge_exact(
        self,
        number: int,
        version: str,
        head_revision: str,
    ) -> dict[str, Any]:
        if _SHA40.fullmatch(head_revision) is None:
            raise ValueError("release pull request head revision is invalid")
        raw = self.command(
            [
                "gh",
                "api",
                "--method",
                "PUT",
                f"repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/pulls/{number}/merge",
                "--field",
                "merge_method=squash",
                "--field",
                f"commit_title=chore(release): prepare v{version}",
                "--field",
                f"sha={head_revision}",
            ],
            timeout=120,
        )
        try:
            return _object(json.loads(raw), "release merge result")
        except json.JSONDecodeError as error:
            raise ValueError("release merge result is invalid") from error

    def _confirmed_merge_revision(
        self,
        merged: dict[str, Any],
        *,
        number: int,
        head_revision: str,
    ) -> str:
        if merged.get("merged") is not True:
            raise ValueError("release pull request did not merge")
        final_revision = merged.get("sha")
        if type(final_revision) is not str or _SHA40.fullmatch(final_revision) is None:
            raise ReleaseDeliveryError(
                "confirmed release merge returned no usable source revision",
                {
                    "external_state_changed": True,
                    "merge_confirmed": True,
                    "merge_outcome": "merged",
                    "release_pr_head_revision": head_revision,
                    "release_pr_number": number,
                    "rollback_completed": False,
                },
            )
        return final_revision

    def _wait_main_checks(self, source_revision: str) -> dict[str, Any]:
        required = ",".join(sorted(REQUIRED_MAIN_CHECKS))
        for _attempt in range(120):
            try:
                raw = self.command(
                    [
                        "uv",
                        "run",
                        "--python",
                        "3.13",
                        "python",
                        "scripts/ci_status.py",
                        "--sha",
                        source_revision,
                        "--required",
                        required,
                        "--json",
                    ],
                    timeout=60,
                )
                evidence = _object(json.loads(raw), "main CI evidence")
                if evidence.get("sha") != source_revision:
                    raise ValueError("main CI evidence source changed")
                return evidence
            except (ValueError, json.JSONDecodeError):
                self.sleep(10)
                self.heartbeat()
        raise ValueError("exact main CI did not become green")

    def _final_local_gates(self, source_revision: str, version: str) -> dict[str, Any]:
        scratch = self.repository_root / "to-delete" / "aiops-release-build"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, exist_ok=True)
        outputs: list[str] = []
        commands = [
            ["uv", "run", "--python", "3.13", "ruff", "check", "."],
            ["uv", "run", "--python", "3.13", "mypy", "src"],
            [
                "uv",
                "run",
                "--python",
                "3.13",
                "python",
                "scripts/check_benchmark_docs.py",
            ],
            ["uv", "run", "--python", "3.13", "pytest", "-q"],
            ["bats", "-r", "tests"],
            ["uv", "run", "--python", "3.13", "data-olympus", "lint", "example-bundle"],
            [
                "uv",
                "run",
                "--python",
                "3.13",
                "python",
                "scripts/check_doc_consistency.py",
            ],
            ["uv", "build", "--out-dir", str(scratch)],
        ]
        try:
            for command in commands:
                outputs.append(self.command(command, timeout=900))
            artifacts = sorted([*scratch.glob("*.whl"), *scratch.glob("*.tar.gz")])
            if len(artifacts) != 2:
                raise ValueError("release build did not create one wheel and one sdist")
            for artifact in artifacts:
                outputs.append(
                    self.command(
                        [
                            "uv",
                            "run",
                            "--python",
                            "3.13",
                            "python",
                            "scripts/smoke_installed_wheel.py",
                            "--artifact",
                            str(artifact),
                            "--expected-version",
                            version,
                        ],
                        timeout=300,
                    )
                )
            security_report = self.command(
                [
                    "uv",
                    "run",
                    "--python",
                    "3.13",
                    "python",
                    "scripts/security_alerts.py",
                ],
                timeout=120,
            )
            outputs.append(security_report)
            version_report = self.command(
                [
                    "uv",
                    "run",
                    "--python",
                    "3.13",
                    "python",
                    "scripts/check_version_free.py",
                    "--version",
                    version,
                ],
                timeout=120,
            )
            outputs.append(version_report)
            ci = self._wait_main_checks(source_revision)
            evidence_hash = sha256(
                "\n".join(outputs).encode("utf-8")
                + json.dumps(ci, sort_keys=True).encode("utf-8")
            ).hexdigest()
            return {
                "ci": ci,
                "security": {
                    "exit_code": 0,
                    "report_hash": _sha256_text(security_report),
                    "route": "direct-gh-gateway-security-tools-unavailable",
                },
                "tests": {
                    "source_revision": source_revision,
                    "passed": True,
                    "evidence_hash": evidence_hash,
                },
                "version_free": {"version": version, "free": True},
            }
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def prepare(
        self,
        run_input: dict[str, Any],
        admission: dict[str, Any],
    ) -> dict[str, Any]:
        admitted = run_input["source_revision"]
        candidate = _object(
            _object(admission.get("outputs"), "admission outputs").get("candidate"),
            "admitted candidate",
        )
        version = candidate.get("version")
        if type(version) is not str:
            raise ValueError("admitted candidate version is unavailable")
        computed = _object(
            _object(admission.get("evidence"), "admission evidence").get(
                "computed_release"
            ),
            "computed release",
        )
        changes = _object(computed.get("changes"), "computed release changes")
        if self.command(["git", "status", "--porcelain"]):
            raise ValueError("managed release worktree is not clean")
        run_id = str(run_input["run_id"])
        authority_intent = (
            f"Prepare governed Data Olympus release v{version} from "
            f"exact source {admitted}."
        )
        consultation = self.authority_consult(
            {
                "agent_identity": "ai-operations-release",
                "intent": authority_intent,
                "source_session": run_id,
                "trigger": "explicit",
                "workspace": GITHUB_REPOSITORY,
            }
        )
        consulted_at = _positive_finite_number(consultation.get("consulted_at"))
        ttl_seconds = _positive_finite_number(consultation.get("ttl_seconds"))
        if consulted_at is None or ttl_seconds is None:
            raise ValueError("authority consultation receipt is invalid")
        consultation_age = self.clock() - consulted_at
        if (
            not math.isfinite(consultation_age)
            or consultation_age < -MAX_AUTHORITY_CLOCK_SKEW_SECONDS
            or consultation_age
            > min(ttl_seconds, MAX_AUTHORITY_CONSULT_AGE_SECONDS)
        ):
            raise ValueError("authority consultation is not fresh")
        gate_receipt = self.authority_gate_check(
            {
                "action_diff": authority_intent,
                "action_path": "pyproject.toml",
                "session_id": run_id,
                "tool_name": "git commit",
                "workspace": GITHUB_REPOSITORY,
            }
        )
        if (
            gate_receipt.get("verdict") != "allow"
            or gate_receipt.get("session_id") != run_id
            or gate_receipt.get("workspace") != GITHUB_REPOSITORY
        ):
            raise ValueError("authority gate receipt is invalid")
        run_suffix = run_id.split("-", 1)[0]
        branch = f"chore/release-v{version}-{run_suffix}"
        self.command(["git", "switch", "-c", branch, admitted])
        document_evidence = render_release_documents(
            self.repository_root,
            version,
            changes,
            self.today(),
        )
        self.command(["uv", "lock"], timeout=300)
        release_note = f"docs/releases/v{version}.md"
        self.command(
            [
                "git",
                "add",
                "pyproject.toml",
                "uv.lock",
                "CHANGELOG.md",
                release_note,
            ]
        )
        self.command(["git", "diff", "--cached", "--check"])
        self.command(["git", "commit", "-m", f"chore(release): prepare v{version}"])
        head_revision = self.source_revision()
        self.command(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], timeout=180)
        pull = self.gateway_object(
            "create_pull_request",
            {
                "base": "main",
                "body": (
                    "## Outcome\n\n"
                    f"Prepare immutable Data Olympus release v{version} from "
                    f"exact source `{admitted}`.\n\n"
                    "## Verification\n\n"
                    "All repository checks must pass before the deterministic "
                    "release commit is merged. Publication remains blocked until "
                    "the resulting exact main SHA receives independent review and "
                    "SHA bound standing approval."
                ),
                "draft": False,
                "head": branch,
                "maintainer_can_modify": False,
                "owner": GITHUB_OWNER,
                "repo": GITHUB_REPOSITORY,
                "title": f"chore(release): prepare v{version}",
            },
        )
        number = pull.get("number")
        if type(number) is not int:
            url = pull.get("url")
            match = _PULL_REQUEST_URL.fullmatch(url) if type(url) is str else None
            if match is not None:
                number = int(match.group(1))
        if type(number) is not int or number <= 0:
            raise ValueError("release pull request number is invalid")
        ready = self._wait_pull_request(number, head_revision)
        self.command(["git", "fetch", "origin", "main"], timeout=120)
        if self.candidate_revision() != admitted:
            raise ValueError("remote main changed before release merge")
        try:
            merged = self._merge_exact(number, version, head_revision)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ReleaseDeliveryError(
                "release merge outcome could not be confirmed",
                {
                    "external_state_changed": True,
                    "merge_confirmed": False,
                    "merge_outcome": "unknown",
                    "release_pr_head_revision": head_revision,
                    "release_pr_number": number,
                    "rollback_completed": False,
                },
            ) from error
        final_revision = self._confirmed_merge_revision(
            merged,
            number=number,
            head_revision=head_revision,
        )
        try:
            return self._finish_preparation_after_merge(
                run_input=run_input,
                admitted=admitted,
                version=version,
                head_revision=head_revision,
                final_revision=final_revision,
                number=number,
                ready=ready,
                document_evidence=document_evidence,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ReleaseDeliveryError(
                str(error),
                {
                    "external_state_changed": True,
                    "merge_confirmed": True,
                    "merge_outcome": "merged",
                    "merged_revision": final_revision,
                    "release_pr_head_revision": head_revision,
                    "release_pr_number": number,
                    "rollback_completed": False,
                },
            ) from error

    def _finish_preparation_after_merge(
        self,
        *,
        run_input: dict[str, Any],
        admitted: str,
        version: str,
        head_revision: str,
        final_revision: str,
        number: int,
        ready: dict[str, Any],
        document_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if _SHA40.fullmatch(final_revision) is None:
            raise ValueError("release merge returned an invalid source revision")
        self.command(["git", "fetch", "origin", "main"], timeout=120)
        if self.candidate_revision() != final_revision:
            raise ValueError("release merge SHA does not match remote main")
        self.command(["git", "switch", "--detach", final_revision])
        if self.command(["git", "rev-parse", f"{final_revision}^"]) != admitted:
            raise ValueError("release squash merge parent does not match admission")
        if self.command(["git", "status", "--porcelain"]):
            raise ValueError("release candidate worktree is not clean")
        gates = self._final_local_gates(final_revision, version)
        live = self.live_statefulset()
        rollback = deployment_state(live)
        if not rollback["rollout_complete"]:
            raise ValueError("current Data Olympus rollback point is not ready")
        final_candidate = {
            "version": version,
            "source_revision": final_revision,
        }
        raw = {
            "candidate": final_candidate,
            "extra_context": run_input["extra_context"],
            "changelog": {
                "source_revision": final_revision,
                "content_hash": document_evidence["changelog_hash"],
            },
            "security": gates["security"],
            "tests": gates["tests"],
            "rollback_point": {
                "digest": rollback["digest"],
                "image": f"{IMAGE_REPOSITORY}@{rollback['digest']}",
                "keel_policy": "never",
            },
            "release_pr": {
                "number": number,
                "url": f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/pull/{number}",
                "merged": True,
                "base_source_revision": admitted,
                "head_revision": head_revision,
                "source_revision": final_revision,
                "candidate_version": version,
            },
        }
        self._prepared = {
            "raw": raw,
            "gates": gates,
            "document_evidence": document_evidence,
            "pull_checks": ready["checks"],
        }
        return raw

    def validate(
        self,
        _run_input: dict[str, Any],
        _admission: dict[str, Any],
        _prepared: dict[str, Any],
    ) -> dict[str, Any]:
        if self._prepared is None:
            raise ValueError("release preparation evidence is unavailable")
        candidate = _object(self._prepared["raw"]["candidate"], "candidate")
        source_revision = candidate["source_revision"]
        if (
            self.candidate_revision() != source_revision
            or self.source_revision() != source_revision
        ):
            raise ValueError("release candidate changed before validation")
        gates = self._prepared["gates"]
        return {
            "candidate": candidate,
            "current_source_revision": source_revision,
            "ci": {
                "source_revision": source_revision,
                "all_success": gates["ci"]["all_success"],
                "missing_required": gates["ci"]["missing_required"],
            },
            "security": {"exit_code": gates["security"]["exit_code"]},
            "version_free": gates["version_free"],
            "tests": gates["tests"],
        }

    def _workflow_run(
        self,
        workflow_id: str,
        source_revision: str,
        inputs: dict[str, object],
        *,
        on_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        listed = self.gateway_object(
            "actions_list",
            {
                "method": "list_workflow_runs",
                "owner": GITHUB_OWNER,
                "per_page": 100,
                "repo": GITHUB_REPOSITORY,
                "resource_id": workflow_id,
                "workflow_runs_filter": {"event": "workflow_dispatch"},
            },
        )
        existing_runs = listed.get("workflow_runs")
        if type(existing_runs) is not list:
            raise ValueError("existing workflow runs are unavailable")
        existing_ids = {
            run.get("id")
            for run in existing_runs
            if type(run) is dict and type(run.get("id")) is int
        }
        self.heartbeat()
        if on_dispatch is not None:
            on_dispatch()
        self.gateway.execute(
            "actions_run_trigger",
            {
                "inputs": inputs,
                "method": "run_workflow",
                "owner": GITHUB_OWNER,
                "ref": "main",
                "repo": GITHUB_REPOSITORY,
                "workflow_id": workflow_id,
            },
        )
        self.heartbeat()
        for _attempt in range(180):
            listed = self.gateway_object(
                "actions_list",
                {
                    "method": "list_workflow_runs",
                    "owner": GITHUB_OWNER,
                    "per_page": 30,
                    "repo": GITHUB_REPOSITORY,
                    "resource_id": workflow_id,
                    "workflow_runs_filter": {"event": "workflow_dispatch"},
                },
            )
            runs = listed.get("workflow_runs")
            if type(runs) is not list:
                raise ValueError(f"{workflow_id} workflow runs are unavailable")
            matches = [
                run
                for run in runs
                if type(run) is dict
                and run.get("id") not in existing_ids
                and run.get("head_sha") == source_revision
                and run.get("event") == "workflow_dispatch"
            ]
            if matches:
                run = max(matches, key=lambda item: int(item.get("id", 0)))
                if run.get("status") == "completed":
                    if run.get("conclusion") != "success":
                        raise ValueError(f"{workflow_id} did not succeed")
                    run_id = run.get("id")
                    if type(run_id) is not int:
                        raise ValueError(f"{workflow_id} run identifier is invalid")
                    exact = self.gateway_object(
                        "actions_get",
                        {
                            "method": "get_workflow_run",
                            "owner": GITHUB_OWNER,
                            "repo": GITHUB_REPOSITORY,
                            "resource_id": str(run_id),
                        },
                    )
                    if (
                        exact.get("id") != run_id
                        or exact.get("head_sha") != source_revision
                        or exact.get("conclusion") != "success"
                    ):
                        raise ValueError(f"{workflow_id} exact receipt changed")
                    return {
                        "conclusion": "success",
                        "run_id": run_id,
                        "source_revision": source_revision,
                    }
            self.sleep(10)
            self.heartbeat()
        raise ValueError(f"{workflow_id} did not complete before timeout")

    def _release(self, tag: str) -> dict[str, Any]:
        return self.gateway_object(
            "get_release_by_tag",
            {"owner": GITHUB_OWNER, "repo": GITHUB_REPOSITORY, "tag": tag},
        )

    def _release_asset_json(
        self,
        release: dict[str, Any],
        asset_name: str,
    ) -> dict[str, Any]:
        assets = release.get("assets")
        if type(assets) is not list:
            raise ValueError("GitHub release assets are unavailable")
        matches = [
            asset
            for asset in assets
            if type(asset) is dict and asset.get("name") == asset_name
        ]
        if len(matches) != 1:
            raise ValueError(f"GitHub release must contain one {asset_name}")
        url = matches[0].get("browser_download_url")
        allowed_prefix = (
            f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/download/"
        )
        if type(url) is not str or not url.startswith(allowed_prefix):
            raise ValueError("GitHub release asset URL is invalid")
        self.heartbeat()
        result = self.fetch_json(url, 30)
        self.heartbeat()
        return result

    def _registry_digest(self, tag: str) -> str:
        raw = self.command(
            [
                "uv",
                "run",
                "--python",
                "3.13",
                "python",
                "scripts/deployed_digest.py",
                "--target",
                tag,
                "--json",
            ],
            timeout=120,
        )
        try:
            evidence = _object(json.loads(raw), "GHCR digest evidence")
        except json.JSONDecodeError as error:
            raise ValueError("GHCR digest evidence is invalid") from error
        digest = evidence.get("digest")
        if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
            raise ValueError(f"GHCR tag {tag} does not resolve to one exact digest")
        return digest

    def _apply_digest(
        self,
        digest: str,
        *,
        on_apply: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        live = self.live_statefulset()
        manifest = deployment_manifest_for_digest(live, digest)
        self.heartbeat()
        if on_apply is not None:
            on_apply()
        self.gateway.execute(
            "k8s_kndev_resources_create_or_update",
            {
                "resource": yaml.safe_dump(
                    manifest,
                    allow_unicode=True,
                    sort_keys=False,
                )
            },
        )
        self.heartbeat()
        for _attempt in range(90):
            current = deployment_state(self.live_statefulset())
            if current["digest"] == digest and current["rollout_complete"]:
                return current
            self.sleep(10)
            self.heartbeat()
        raise ValueError("Data Olympus exact digest rollout did not complete")

    def _service_acceptance(self) -> dict[str, bool]:
        raw = self.command(
            [
                "uv",
                "run",
                "--python",
                "3.13",
                "data-olympus",
                "verify",
                "--target",
                DATA_OLYMPUS_URL,
                "--json",
            ],
            timeout=120,
        )
        try:
            result = _object(json.loads(raw), "Data Olympus acceptance evidence")
        except json.JSONDecodeError as error:
            raise ValueError("Data Olympus acceptance evidence is invalid") from error
        checks = result.get("checks")
        if result.get("ok") is not True or type(checks) is not list:
            raise ValueError("Data Olympus external acceptance did not pass")
        by_name = {
            check.get("name"): check.get("ok")
            for check in checks
            if type(check) is dict
        }
        required = {"health", "readiness", "search", "enforcement"}
        if any(by_name.get(name) is not True for name in required):
            raise ValueError("Data Olympus external acceptance is incomplete")
        documentation_raw = self.command(
            [
                "fastmcp",
                "call",
                "--server-spec",
                f"{DATA_OLYMPUS_URL}/mcp",
                "--target",
                "kb_get",
                "--input-json",
                json.dumps({"id": "projects-data-olympus-release-contract"}),
                "--json",
                "--timeout",
                "30",
                "--auth",
                "none",
            ],
            timeout=60,
        )
        try:
            documentation = _object(
                json.loads(documentation_raw),
                "Data Olympus documentation evidence",
            )
        except json.JSONDecodeError as error:
            raise ValueError("Data Olympus documentation evidence is invalid") from error
        structured = documentation.get("structured_content")
        if (
            documentation.get("is_error") is not False
            or type(structured) is not dict
            or structured.get("id") != "projects-data-olympus-release-contract"
        ):
            raise ValueError("Data Olympus documentation read did not pass")
        return {
            "documentation": True,
            "enforcement": True,
            "healthy": True,
            "mcp_search": True,
            "ready": True,
        }

    def _candidate_publication(
        self,
        source_revision: str,
        version: str,
        candidate_tag: str,
    ) -> dict[str, Any]:
        release = self._release(candidate_tag)
        provenance = self._release_asset_json(release, "release-provenance.json")
        digest = self._registry_digest(candidate_tag)
        return candidate_release_evidence(
            release,
            provenance,
            source_revision=source_revision,
            version=version,
            candidate_tag=candidate_tag,
            registry_digest=digest,
        )

    def _stable_publication(
        self,
        source_revision: str,
        version: str,
    ) -> dict[str, Any]:
        release = self._release(f"v{version}")
        provenance = self._release_asset_json(release, "release-provenance.json")
        pypi_url = f"https://pypi.org/pypi/data-olympus/{version}/json"
        pypi = self.fetch_json(pypi_url, 30)
        return stable_release_evidence(
            release,
            provenance,
            pypi,
            source_revision=source_revision,
            version=version,
        )

    def deliver(
        self,
        _run_input: dict[str, Any],
        _admission: dict[str, Any],
        _prepared: dict[str, Any],
        _reviewed: dict[str, Any],
    ) -> dict[str, Any]:
        if self._prepared is None:
            raise ValueError("release preparation evidence is unavailable")
        candidate = _object(self._prepared["raw"]["candidate"], "candidate")
        source_revision = candidate["source_revision"]
        version = candidate["version"]
        tags = self.gateway.execute(
            "list_tags",
            {
                "owner": GITHUB_OWNER,
                "page": 1,
                "perPage": 100,
                "repo": GITHUB_REPOSITORY,
            },
        )
        if type(tags) is not list:
            raise ValueError("GitHub tags are unavailable")
        tag_names: list[str] = []
        for raw_tag in tags:
            if type(raw_tag) is dict and type(raw_tag.get("name")) is str:
                tag_names.append(raw_tag["name"])
        rc_number = next_rc_number(version, tag_names)
        candidate_tag = f"{version}-rc.{rc_number}"
        rollback_digest = self._prepared["raw"]["rollback_point"]["digest"]
        deployment_started = False
        external_state_changed = False
        rollback_completed = False

        def mark_external_state_changed() -> None:
            nonlocal external_state_changed
            external_state_changed = True

        def mark_deployment_started() -> None:
            nonlocal deployment_started
            mark_external_state_changed()
            deployment_started = True

        try:
            rc_receipt = self._workflow_run(
                "rc-publish.yml",
                source_revision,
                {"number": rc_number, "ref": source_revision},
                on_dispatch=mark_external_state_changed,
            )
            external_state_changed = True
            publication = self._candidate_publication(
                source_revision,
                version,
                candidate_tag,
            )
            digest = publication["image_digest"]
            canary_rollout = self._apply_digest(
                digest,
                on_apply=mark_deployment_started,
            )
            canary_acceptance = self._service_acceptance()
            tag_receipt = self._workflow_run(
                "tag-release.yml",
                source_revision,
                {"candidate_tag": candidate_tag},
                on_dispatch=mark_external_state_changed,
            )
            stable = self._stable_publication(source_revision, version)
            stable_digest = self._registry_digest(f"v{version}")
            if stable_digest != digest:
                raise ValueError("stable GHCR tag rebuilt or changed the candidate digest")
            channel_receipt = self._workflow_run(
                "set-channel.yml",
                source_revision,
                {"channel": "kndev", "source": f"v{version}"},
                on_dispatch=mark_external_state_changed,
            )
            stable_rollout = self._apply_digest(
                digest,
                on_apply=mark_deployment_started,
            )
            stable_acceptance = self._service_acceptance()
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            if deployment_started:
                try:
                    self._apply_digest(rollback_digest)
                    self._service_acceptance()
                    rollback_completed = True
                except (OSError, subprocess.SubprocessError, ValueError) as rollback_error:
                    raise ReleaseDeliveryError(
                        "release delivery failed and exact digest rollback also failed",
                        {
                            "deployment_started": True,
                            "external_state_changed": True,
                            "rollback_completed": False,
                        },
                    ) from rollback_error
            if external_state_changed:
                raise ReleaseDeliveryError(
                    str(error),
                    {
                        "deployment_started": deployment_started,
                        "external_state_changed": True,
                        "rollback_completed": rollback_completed,
                    },
                ) from error
            raise
        final_candidate = {
            **candidate,
            "candidate_tag": candidate_tag,
            "image_digest": digest,
        }
        raw = {
            "candidate": final_candidate,
            "current_source_revision": source_revision,
            "workflows": [
                {
                    **rc_receipt,
                    "name": "rc-publish.yml",
                    "candidate_tag": candidate_tag,
                },
                {
                    **tag_receipt,
                    "name": "tag-release.yml",
                    "candidate_tag": candidate_tag,
                },
                {
                    **channel_receipt,
                    "name": "set-channel.yml",
                    "source_tag": f"v{version}",
                },
            ],
            "canary": {
                "candidate_tag": candidate_tag,
                "source_revision": source_revision,
                "digest": digest,
                "keel_policy": canary_rollout["keel_policy"],
                "rollout_complete": canary_rollout["rollout_complete"],
                **canary_acceptance,
            },
            "deployment": {
                "keel_policy": stable_rollout["keel_policy"],
                "source_revision": source_revision,
                "image_digest": digest,
                "rollout_complete": stable_rollout["rollout_complete"],
            },
        }
        self._delivery = {
            "raw": raw,
            "acceptance": stable_acceptance,
            "candidate_publication": publication,
            "stable_publication": stable,
        }
        return raw

    def verify(
        self,
        _run_input: dict[str, Any],
        _admission: dict[str, Any],
        _delivery: dict[str, Any],
    ) -> dict[str, Any]:
        if self._delivery is None:
            raise ValueError("release delivery evidence is unavailable")
        candidate = _object(self._delivery["raw"]["candidate"], "candidate")
        source_revision = candidate["source_revision"]
        version = candidate["version"]
        digest = candidate["image_digest"]
        stable = self._stable_publication(source_revision, version)
        if self._registry_digest(f"v{version}") != digest:
            raise ValueError("stable GHCR digest changed after delivery")
        deployment = deployment_state(self.live_statefulset())
        if deployment["digest"] != digest or not deployment["rollout_complete"]:
            raise ValueError("kn dev deployment changed after delivery")
        acceptance = self._service_acceptance()
        return {
            "candidate": candidate,
            "github_release": {
                "tag": f"v{version}",
                "source_revision": stable["source_revision"],
            },
            "pypi": {
                "version": stable["pypi_version"],
                "provenance_source_revision": stable["source_revision"],
            },
            "image": {"tag": f"v{version}", "digest": digest},
            "deployment": {
                "source_revision": source_revision,
                "digest": digest,
                **acceptance,
            },
        }

    def rollback(self, recovery: dict[str, Any]) -> dict[str, Any]:
        source_revision = recovery.get("source_revision")
        if type(source_revision) is not str or _SHA40.fullmatch(source_revision) is None:
            raise ValueError("recovery source revision is invalid")
        outcome = _object(recovery.get("outcome_evidence"), "recovery evidence")
        failed_digest = outcome.get("digest")
        rollback_digest = outcome.get("rollback_digest")
        if type(failed_digest) is not str or _DIGEST.fullmatch(failed_digest) is None:
            raise ValueError("failed release digest is unavailable")
        if (
            type(rollback_digest) is not str
            or _DIGEST.fullmatch(rollback_digest) is None
        ):
            raise ValueError("release rollback digest is unavailable")
        apply_started = False

        def mark_apply_started() -> None:
            nonlocal apply_started
            apply_started = True

        try:
            restored = self._apply_digest(
                rollback_digest,
                on_apply=mark_apply_started,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            if apply_started:
                raise ReleaseDeliveryError(
                    str(error),
                    {
                        "acceptance_completed": False,
                        "digest_apply_confirmed": False,
                        "external_state_changed": True,
                        "rollback_completed": False,
                        "rollback_digest": rollback_digest,
                        "rollout_complete": False,
                    },
                ) from error
            raise
        try:
            acceptance = self._service_acceptance()
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ReleaseDeliveryError(
                str(error),
                {
                    "acceptance_completed": False,
                    "digest_apply_confirmed": True,
                    "external_state_changed": True,
                    "rollback_completed": False,
                    "rollback_digest": rollback_digest,
                    "rollout_complete": restored["rollout_complete"],
                },
            ) from error
        from scripts.operations.release import evaluate_stage

        evaluated = evaluate_stage(
            "rollback",
            {
                "failed_digest": failed_digest,
                "rollback_point": {
                    "digest": rollback_digest,
                    "image": f"{IMAGE_REPOSITORY}@{rollback_digest}",
                    "intended_keel_policy": "never",
                },
                "events": [
                    {"name": "keel-paused", "policy": "never"},
                    {
                        "name": "digest-restored",
                        "digest": rollback_digest,
                        "containers": ["data-olympus-mcp", "prepare-git"],
                    },
                    {
                        "name": "deployment-verified",
                        "digest": rollback_digest,
                        "healthy": acceptance["healthy"],
                        "ready": restored["rollout_complete"],
                    },
                    {"name": "keel-policy-restored", "policy": "never"},
                ],
            },
        )
        return {
            "status": evaluated["status"],
            "reason": evaluated["reason"],
            "evidence": evaluated["evidence"],
            "source_revision": source_revision,
        }


def default_release_dependencies() -> dict[str, Any]:
    """Wire the one project command to its complete live release runtime."""
    runtime = ReleaseRuntime(
        Path.cwd(),
        gateway=FastMCPGateway(),
    )
    return {
        "workspace": runtime.repository_root,
        "source_revision": runtime.source_revision,
        "candidate_revision": runtime.candidate_revision,
        "collect_admission": runtime.collect_admission,
        "prepare": runtime.prepare,
        "validate": runtime.validate,
        "deliver": runtime.deliver,
        "verify": runtime.verify,
        "rollback": runtime.rollback,
        "set_heartbeat": runtime.set_heartbeat,
    }
