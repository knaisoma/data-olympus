#!/usr/bin/env python3
"""Trusted stage-two RC verifier and publisher (W6 R8), disabled by default.

Runs only from the main checkout of a workflow_run job and only when
SDLC_PIPELINE is exactly "enabled". Candidate history is fetched into a bare
repository and read as Git data; downloaded artifacts are hashed and parsed,
never built, installed, extracted to disk or executed.

Each phase runs in its own job, holds the shared promotion lock, and repeats
the full verification (branch head, main, version recompute, hashes, digest):

  reserve       annotated tag at H and a public prerelease whose wheel, sdist
                and provenance assets bind the identity to H before PyPI
                (contents: write)
  stage-upload  copy the Python files still missing from PyPI for the OIDC
                upload action; id-token: write exists only in that job
  publish       wait for the PyPI readback, push the OCI archive by digest,
                move rc for release/new heads only when SDLC_RC_CHANNEL is
                exactly "enabled", write the staging selection record
                (packages: write) and emit the validated image digest and
                Python file hashes as job outputs

A separate attest job, which never runs this script or parses the archives,
re-checks the files against those hashes and signs the attestations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import adoption_ratification as ratification  # noqa: E402
from scripts.sdlc_version import Git, VersionError, compute_version  # noqa: E402

REPOSITORY = "knaisoma/data-olympus"
IMAGE = f"ghcr.io/{REPOSITORY}"
BUILD_WORKFLOW = ".github/workflows/rc-build.yml"
BRANCHES = ("release/new", "hotfix/new")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
PROVENANCE = "release-provenance.json"
PYPI_ATTEMPTS = 20
PYPI_DELAY_SECONDS = 15.0
# Upper bounds for untrusted metadata read into memory (fail closed above them).
METADATA_LIMIT = 1024 * 1024
PROVENANCE_LIMIT = 1024 * 1024


def require(condition: bool, message: str) -> None:
    """Fail closed. Messages are fixed strings, never untrusted input."""
    if not condition:
        raise ValueError(message)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def admission(event: dict[str, Any]) -> tuple[str, str]:
    """Accept only a successful rc-build push run of this repository's branches."""
    run = event["workflow_run"]
    require(event["repository"]["full_name"] == REPOSITORY, "unexpected repository")
    require(run["head_repository"]["full_name"] == REPOSITORY, "foreign head repository")
    # workflow_run matches by display name; bind the run to the real file too.
    path = run["path"]
    require(isinstance(path, str) and (path == BUILD_WORKFLOW
                                       or path.startswith(BUILD_WORKFLOW + "@")),
            "triggering run is not rc-build.yml")
    require(run["name"] == "rc-build" and run["conclusion"] == "success"
            and run["event"] == "push", "not a successful push build")
    branch, head = run["head_branch"], run["head_sha"]
    require(branch in BRANCHES, "branch not in allow-list")
    require(isinstance(head, str) and SHA.fullmatch(head) is not None, "invalid head SHA")
    for key in ("id", "run_attempt"):
        require(type(run[key]) is int and run[key] > 0, "invalid triggering run identity")
    return branch, head


def safe_file(root: Path, name: object) -> Path:
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", name) is not None
            and name not in (".", ".."), "unsafe artifact name")
    assert isinstance(name, str)
    path = root / name
    require(not path.is_symlink() and path.is_file(), "artifact must be a regular file")
    require(path.resolve().parent == root.resolve(), "artifact escaped its directory")
    return path


def bounded_read(stream: Any, declared: int, limit: int) -> bytes:
    """Read at most limit bytes; a declared or actual larger size fails closed."""
    require(declared <= limit, "oversized metadata")
    data = stream.read(limit + 1)
    require(isinstance(data, bytes) and len(data) <= limit, "oversized metadata")
    return bytes(data)


def metadata_version(raw: bytes, version: str) -> None:
    metadata = BytesParser().parsebytes(raw)
    require(metadata.get_all("Name") == ["data-olympus"]
            and metadata.get_all("Version") == [version], "distribution metadata mismatch")


def verify_distribution(path: Path, version: str, kind: str) -> None:
    if kind == "wheel":
        require(re.fullmatch(rf"data_olympus-{re.escape(version)}-[A-Za-z0-9_.]+-"
                             r"[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl", path.name) is not None,
                "wheel filename version mismatch")
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
            require(names == [f"data_olympus-{version}.dist-info/METADATA"],
                    "ambiguous wheel metadata")
            with archive.open(names[0]) as entry:
                raw = bounded_read(entry, archive.getinfo(names[0]).file_size, METADATA_LIMIT)
            metadata_version(raw, version)
    else:
        require(path.name == f"data_olympus-{version}.tar.gz", "sdist filename mismatch")
        with tarfile.open(path) as archive:
            members = [m for m in archive if m.name.endswith("/PKG-INFO")
                       and len(PurePosixPath(m.name).parts) == 2]
            require(len(members) == 1 and members[0].isfile(), "ambiguous sdist metadata")
            stream = archive.extractfile(members[0])
            require(stream is not None, "missing sdist metadata")
            assert stream is not None
            metadata_version(bounded_read(stream, members[0].size, METADATA_LIMIT), version)


def archive_digest(path: Path) -> str:
    """Validate the OCI descriptor graph without extracting or running anything."""
    with tarfile.open(path) as archive:
        members = {}
        for member in archive:
            name = member.name.removeprefix("./")
            require(not PurePosixPath(name).is_absolute()
                    and ".." not in PurePosixPath(name).parts, "unsafe OCI path")
            if member.isdir():
                continue
            require(member.isfile() and name not in members, "unsafe or duplicate OCI entry")
            require(name in ("index.json", "oci-layout")
                    or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name) is not None,
                    "unexpected OCI entry")
            members[name] = member

        def read(name: str) -> bytes:
            require(name in members, "missing OCI blob")
            require(members[name].size <= 16 * 1024 * 1024, "oversized OCI metadata")
            stream = archive.extractfile(members[name])
            assert stream is not None
            return stream.read()

        require(json.loads(read("oci-layout")) == {"imageLayoutVersion": "1.0.0"},
                "invalid OCI layout")
        for name, member in members.items():
            if name.startswith("blobs/"):
                stream = archive.extractfile(member)
                assert stream is not None
                blob = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    blob.update(chunk)
                require(blob.hexdigest() == name.split("/")[-1], "OCI blob hash mismatch")
        index = json.loads(read("index.json"))
        require(isinstance(index, dict) and index.get("schemaVersion") == 2
                and isinstance(index.get("manifests"), list) and len(index["manifests"]) == 1,
                "OCI archive must have exactly one root descriptor")

        def visit(descriptor: dict[str, Any], manifest: bool, depth: int = 0) -> None:
            require(depth < 16 and isinstance(descriptor, dict)
                    and isinstance(descriptor.get("digest"), str)
                    and DIGEST.fullmatch(descriptor["digest"]) is not None,
                    "invalid OCI descriptor")
            require(not descriptor.get("urls"), "external OCI URLs forbidden")
            name = "blobs/sha256/" + descriptor["digest"][7:]
            require(name in members and type(descriptor.get("size")) is int
                    and descriptor["size"] == members[name].size, "OCI descriptor size mismatch")
            if manifest:
                data = json.loads(read(name))
                require(isinstance(data, dict) and data.get("schemaVersion") == 2,
                        "invalid OCI manifest")
                if "manifests" in data:
                    for child in data["manifests"]:
                        visit(child, True, depth + 1)
                else:
                    visit(data["config"], False, depth + 1)
                    for child in data["layers"]:
                        visit(child, False, depth + 1)

        root = index["manifests"][0]
        visit(root, True)
        return str(root["digest"])


@dataclass(frozen=True)
class Verified:
    head: str
    branch: str
    version: str
    python_version: str
    image_digest: str
    archive: Path
    files: dict[str, Path]
    assets: dict[str, bytes]
    python_hashes: dict[str, str]
    base: str
    main: str
    build_run_id: int
    build_run_attempt: int


def verify(artifacts: Path, history: Path, event: dict[str, Any], *,
           main: str = "refs/heads/main", branch_ref: str | None = None) -> Verified:
    """Recompute the identity from H's history and bind every artifact to it."""
    branch, head = admission(event)
    git = Git(history)
    # R-CONC: under the lock, H must still be the branch head and main must not
    # have moved (M is compared with provenance below); otherwise fail closed.
    require(git.resolve(branch_ref or f"refs/heads/{branch}") == head,
            "triggered head is no longer the branch head")
    # Adoption ratification comes only from this trusted main checkout (ROOT),
    # never from H's tree or the artifacts; the engine applies it only while B
    # carries release/ADOPTION.json. There is no dry-run mode in this stage.
    computed = compute_version(cwd=history, head=head, main=main, branch=branch,
                               **ratification.engine_kwargs(Path(ROOT)))
    require(computed["N"] > 0, "N=0 never publishes")
    require(computed["promotable"], "engine refuses promotion")
    provenance_path = safe_file(artifacts, PROVENANCE)
    with provenance_path.open("rb") as stream:
        raw = bounded_read(stream, provenance_path.stat().st_size, PROVENANCE_LIMIT)
    payload = json.loads(raw)
    require(isinstance(payload, dict) and isinstance(payload.get("candidate"), dict),
            "provenance must contain a candidate receipt")
    expected = {**{key: computed[key] for key in ("B", "H", "M", "N")},
                "source_sha": head, "candidate_tag": computed["candidate"],
                "python_version": computed["pypi_version"], "dry_run": False, "promotable": True}
    for key, value in expected.items():
        require(type(payload.get(key)) is type(value) and payload[key] == value,
                f"provenance recompute mismatch: {key}")
    # Stage one encodes the mode in candidate_tag (checked above against the
    # engine for this branch); an explicit mode field, if present, must agree.
    mode = "hotfix" if branch == "hotfix/new" else "release"
    require(payload.get("mode", mode) == mode, "provenance mode mismatch")
    candidate = payload["candidate"]
    require(candidate.get("source_sha") == head
            and candidate.get("version") == computed["pypi_version"],
            "candidate receipt mismatch")
    dist = artifacts / "dist"
    require(not dist.is_symlink() and dist.is_dir(), "missing distribution directory")
    files: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for kind in ("wheel", "sdist"):
        path = safe_file(dist, candidate.get(kind))
        actual = sha256(path)
        require(actual == candidate.get(f"{kind}_sha256"), f"{kind} hash mismatch")
        verify_distribution(path, computed["pypi_version"], kind)
        files[path.name], hashes[path.name] = path, actual
    require({p.name for p in dist.iterdir()} == set(files), "unexpected distribution files")
    archive = safe_file(artifacts, "image.oci.tar")
    require(sha256(archive) == payload.get("oci_archive_sha256"), "OCI archive hash mismatch")
    image = archive_digest(archive)
    require(image == payload.get("image_digest"), "OCI manifest digest mismatch")
    files[provenance_path.name] = provenance_path
    run = event["workflow_run"]
    return Verified(head, branch, computed["candidate"], computed["pypi_version"], image,
                    archive, files, {name: p.read_bytes() for name, p in files.items()},
                    hashes, computed["B"], computed["M"], run["id"], run["run_attempt"])


class Client(Protocol):
    def python_hashes(self, version: str) -> dict[str, str]: ...
    def image_digest(self, reference: str) -> str | None: ...
    def tag_head(self, tag: str) -> str | None: ...
    def release_assets(self, tag: str) -> dict[str, bytes] | None: ...
    def reserve(self, verified: Verified) -> None: ...
    def push_image(self, verified: Verified) -> None: ...
    def move_channel(self, verified: Verified) -> None: ...


def inventory(verified: Verified, client: Client
              ) -> tuple[dict[str, str], str | None, dict[str, bytes] | None]:
    """Read every surface before writes; outages and ambiguous ownership fail closed."""
    python = client.python_hashes(verified.python_version)
    image = client.image_digest(verified.version)
    tag = client.tag_head(verified.version)
    assets = client.release_assets(verified.version)
    require(tag in (None, verified.head), "Git tag collision: different H")
    require(image in (None, verified.image_digest), "image digest collision")
    require(all(verified.python_hashes.get(n) == h for n, h in python.items()),
            "PyPI hash collision")
    if assets is not None:
        require(tag == verified.head, "release is not bound to H")
        require(all(verified.assets.get(n) == data for n, data in assets.items()),
                "GitHub release asset collision")
    if python or image:
        # Objects that exist must have been published for this same H and bytes.
        require(assets is not None and assets.get(PROVENANCE) == verified.assets[PROVENANCE],
                "existing publication lacks same-H provenance")
    return python, image, assets


def reserve(verified: Verified, client: Client) -> int:
    """Bind the identity to H (tag, prerelease, assets); return missing PyPI files."""
    python, _, assets = inventory(verified, client)
    if assets != verified.assets:
        client.reserve(verified)
        _, _, assets = inventory(verified, client)
        require(assets == verified.assets, "GitHub reservation readback mismatch")
    return len(set(verified.python_hashes) - set(python))


def stage_upload(verified: Verified, client: Client, upload: Path) -> int:
    """Copy only the verified files PyPI still lacks; the reservation must exist."""
    python, _, assets = inventory(verified, client)
    require(client.tag_head(verified.version) == verified.head and assets == verified.assets,
            "incomplete GitHub reservation")
    require(not upload.exists(), "upload directory must be new")
    upload.mkdir(parents=True)
    missing = sorted(set(verified.python_hashes) - set(python))
    for name in missing:
        shutil.copyfile(verified.files[name], upload / name)
    return len(missing)


def publish(verified: Verified, client: Client, *,
            move_rc: bool = False,
            sleep: Callable[[float], None] = time.sleep,
            attempts: int = PYPI_ATTEMPTS, delay: float = PYPI_DELAY_SECONDS) -> dict[str, Any]:
    """Wait for PyPI, push the image by digest, optionally move rc for release/new.

    The rc channel stays with set-channel.yml until the first release under the
    new model (W6 spec invariant); move_rc is off unless SDLC_RC_CHANNEL is
    exactly "enabled", and even then a hotfix head never moves it.
    """
    _, image, assets = inventory(verified, client)
    require(assets == verified.assets, "incomplete GitHub reservation")
    # The PyPI JSON API is eventually consistent after an upload.
    for attempt in range(attempts):
        python = client.python_hashes(verified.python_version)
        require(all(verified.python_hashes.get(n) == h for n, h in python.items()),
                "PyPI hash collision")
        if python == verified.python_hashes:
            break
        if attempt + 1 < attempts:
            sleep(delay)
    else:
        raise ValueError("PyPI publication is incomplete")
    if image is None:
        client.push_image(verified)
    require(client.image_digest(verified.version) == verified.image_digest,
            "remote image digest mismatch")
    moved = move_rc and verified.branch == "release/new"
    if moved:
        client.move_channel(verified)
    return {"H": verified.head, "B": verified.base, "M": verified.main,
            "branch": verified.branch, "version": verified.version,
            "python_version": verified.python_version,
            "image": f"{IMAGE}@{verified.image_digest}",
            "image_digest": verified.image_digest,
            "build_run_id": verified.build_run_id,
            "build_run_attempt": verified.build_run_attempt,
            "rc_channel_moved": moved}


def selection(record: dict[str, Any], history: Path) -> dict[str, Any]:
    """Branch-based staging selection (STD-U-821 1.2) from freshly fetched refs."""
    hotfix = Git(history).run(
        "for-each-ref", "--format=%(objectname)", "refs/heads/hotfix/new",
    ).strip() or None
    selected = hotfix == record["H"] if record["branch"] == "hotfix/new" else hotfix is None
    require(record["branch"] != "hotfix/new" or selected, "hotfix head moved during publication")
    return record | {"selected": selected,
                     "selection_branch": "hotfix/new" if hotfix else "release/new"}


def command(*args: str, input_data: bytes | None = None, absent: str | None = None
            ) -> bytes | None:
    result = subprocess.run(args, input=input_data, capture_output=True, check=False, timeout=600)
    if result.returncode:
        if absent and re.search(absent, result.stderr.decode(errors="replace")):
            return None
        # Never echo arguments or output: registries and gh can reflect remote text.
        raise ValueError(f"{args[0]} operation failed (exit {result.returncode})")
    return bytes(result.stdout)


class Registries:
    """Production adapters. Only an explicit 404 or manifest-unknown means absent.

    gh reads GH_TOKEN (the job's GITHUB_TOKEN) from the environment. Skopeo
    gets it through --password-stdin into a private auth file, never argv.
    """

    def __init__(self) -> None:
        self._authdir: str | None = None

    def close(self) -> None:
        if self._authdir is not None:
            shutil.rmtree(self._authdir, ignore_errors=True)
            self._authdir = None

    def authfile(self) -> str:
        if self._authdir is None:
            self._authdir = tempfile.mkdtemp(prefix="rc-publish-auth-")
            path = str(Path(self._authdir) / "auth.json")
            command("skopeo", "login", "--authfile", path,
                    "--username", os.environ.get("GITHUB_ACTOR") or "github-actions",
                    "--password-stdin", "ghcr.io",
                    input_data=os.environ["GH_TOKEN"].encode())
        return str(Path(self._authdir) / "auth.json")

    def api(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        args = ["gh", "api", f"repos/{REPOSITORY}/{path}"]
        if payload is not None:
            args += ["--method", "POST", "--input", "-"]
        raw = command(*args, input_data=json.dumps(payload).encode() if payload else None,
                      absent=r"\(HTTP 404\)" if payload is None else None)
        return json.loads(raw) if raw is not None else None

    def python_hashes(self, version: str) -> dict[str, str]:
        try:
            with urllib.request.urlopen(
                f"https://pypi.org/pypi/data-olympus/{version}/json", timeout=30,
            ) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {}
            raise ValueError("PyPI inventory unavailable") from None
        result = {item["filename"]: item["digests"]["sha256"] for item in data["urls"]}
        require(len(result) == len(data["urls"]), "ambiguous PyPI inventory")
        return result

    def image_digest(self, reference: str) -> str | None:
        raw = command("skopeo", "inspect", "--raw", "--authfile", self.authfile(),
                      f"docker://{IMAGE}:{reference}", absent=r"manifest unknown|MANIFEST_UNKNOWN")
        return "sha256:" + hashlib.sha256(raw).hexdigest() if raw is not None else None

    def tag_head(self, tag: str) -> str | None:
        result = self.api(f"git/ref/tags/{tag}")
        if result is None:
            return None
        obj = result["object"]
        require(obj["type"] == "tag", "candidate tag must be annotated")
        tag_data = self.api(f"git/tags/{obj['sha']}")
        require(tag_data is not None and tag_data["object"]["type"] == "commit",
                "candidate tag must point directly to a commit")
        return str(tag_data["object"]["sha"])

    def release_assets(self, tag: str) -> dict[str, bytes] | None:
        release = self.api(f"releases/tags/{tag}")
        if release is None:
            return None
        require(release["prerelease"] is True and release["draft"] is False,
                "existing release is not a public prerelease")
        result: dict[str, bytes] = {}
        for asset in release["assets"]:
            name = asset["name"]
            require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", name) is not None
                    and name not in result, "invalid release asset name")
            data = command("gh", "release", "download", tag, "--repo", REPOSITORY,
                           "--pattern", name, "--output", "-")
            assert data is not None
            result[name] = data
        return result

    def reserve(self, verified: Verified) -> None:
        if self.tag_head(verified.version) is None:
            tag = self.api("git/tags", {"tag": verified.version,
                           "message": f"Candidate {verified.version}\nH: {verified.head}",
                           "object": verified.head, "type": "commit"})
            self.api("git/refs", {"ref": f"refs/tags/{verified.version}", "sha": tag["sha"]})
        assets = self.release_assets(verified.version)
        if assets is None:
            command("gh", "release", "create", verified.version, "--repo", REPOSITORY,
                    "--verify-tag", "--prerelease", "--title", verified.version,
                    "--notes", f"Candidate from {verified.head}. See {PROVENANCE}.")
            assets = {}
        # Never --clobber. Partial uploads are reconciled by byte readback.
        for name, path in verified.files.items():
            if name not in assets:
                command("gh", "release", "upload", verified.version, str(path),
                        "--repo", REPOSITORY)

    def push_image(self, verified: Verified) -> None:
        command("skopeo", "copy", "--all", "--preserve-digests", "--authfile", self.authfile(),
                f"oci-archive:{verified.archive.resolve()}", f"docker://{IMAGE}:{verified.version}")

    def move_channel(self, verified: Verified) -> None:
        if self.image_digest("rc") != verified.image_digest:
            command("skopeo", "copy", "--all", "--preserve-digests",
                    "--authfile", self.authfile(),
                    f"docker://{IMAGE}@{verified.image_digest}", f"docker://{IMAGE}:rc")
        require(self.image_digest("rc") == verified.image_digest, "rc channel digest mismatch")


def fetch_history(path: Path, event: dict[str, Any]) -> None:
    """Fetch H and main as data into a fresh bare repository; recheck the head."""
    branch, head = admission(event)
    shutil.rmtree(path, ignore_errors=True)
    command("git", "init", "--quiet", "--bare", str(path))
    # Fixed public URL, no credentials, no checkout, hooks or submodules.
    command("git", "-C", str(path), "fetch", "--quiet", "--force", "--no-recurse-submodules",
            f"https://github.com/{REPOSITORY}.git", "+refs/heads/*:refs/heads/*",
            "+refs/tags/*:refs/tags/*")
    require(Git(path).resolve(f"refs/heads/{branch}") == head, "branch head moved")


def attestation_outputs(verified: Verified) -> str:
    """GITHUB_OUTPUT lines for the attest job; every value is validated here."""
    require(DIGEST.fullmatch(verified.image_digest) is not None, "invalid image digest")
    lines = [f"image_digest={verified.image_digest}"]
    for kind, suffix in (("wheel", ".whl"), ("sdist", ".tar.gz")):
        names = [n for n in verified.python_hashes if n.endswith(suffix)]
        require(len(names) == 1, "ambiguous distribution outputs")
        name, value = names[0], verified.python_hashes[names[0]]
        require(re.fullmatch(r"data_olympus-[A-Za-z0-9_.-]+", name) is not None
                and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
                "invalid distribution output")
        lines += [f"{kind}={name}", f"{kind}_sha256={value}"]
    return "".join(line + "\n" for line in lines)


def _one_line(text: str) -> str:
    # Never start a log line with "::" (workflow commands) or span lines.
    return re.sub(r"[^\x20-\x7e]", "?", text)[:200]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("reserve", "stage-upload", "publish"))
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--upload", type=Path)
    parser.add_argument("--selection", type=Path)
    args = parser.parse_args(argv)
    client: Registries | None = None
    try:
        require(os.environ.get("SDLC_PIPELINE") == "enabled", "SDLC pipeline disabled")
        require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_run", "workflow_run required")
        require(os.environ.get("GITHUB_REF") == "refs/heads/main",
                "trusted definition must come from main")
        require(args.phase != "stage-upload" or args.upload is not None, "--upload required")
        require(args.phase != "publish" or args.selection is not None, "--selection required")
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
        fetch_history(args.history, event)
        verified = verify(args.artifacts, args.history, event)
        client = Registries()
        if args.phase in ("reserve", "stage-upload"):
            if args.phase == "reserve":
                count = reserve(verified, client)
            else:
                assert args.upload is not None
                count = stage_upload(verified, client, args.upload)
            # Only a boolean literal reaches GITHUB_OUTPUT, never untrusted text.
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write(f"upload={'true' if count else 'false'}\n")
        else:
            assert args.selection is not None
            record = publish(verified, client,
                             move_rc=os.environ.get("SDLC_RC_CHANNEL") == "enabled")
            # Detect ref movement during external publication before selecting.
            fetch_history(args.history, event)
            verify(args.artifacts, args.history, event)
            rendered = json.dumps(selection(record, args.history), sort_keys=True, indent=2) + "\n"
            args.selection.parent.mkdir(parents=True, exist_ok=True)
            args.selection.write_text(rendered)
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                summary.write(
                    "Staging selection (no deployment):\n\n```json\n" + rendered + "```\n")
            # Attestation subjects for the attest job; validated single-line values.
            rendered_outputs = attestation_outputs(verified)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write(rendered_outputs)
    except VersionError as error:
        print(f"RC publication refused: version engine {_one_line(error.code)}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"RC publication refused: {_one_line(str(error))}", file=sys.stderr)
        return 1
    except (OSError, KeyError, TypeError, AttributeError, RecursionError,
            tarfile.TarError, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        # Avoid reflecting attacker-controlled metadata or command lines.
        print(f"RC publication refused ({type(error).__name__}); verify inputs and registry state",
              file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
