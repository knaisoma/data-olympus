#!/usr/bin/env python3
"""Trusted stage-two RC verifier and publisher (W6 R8), disabled by default.

Runs only from the main checkout of a workflow_run job and only when
SDLC_PIPELINE is exactly "enabled". Candidate history is fetched into a bare
repository and read as Git data; downloaded artifacts are hashed and parsed,
never built, installed, extracted to disk or executed.

Each phase runs in its own job, holds the shared promotion lock, and repeats
the full verification (branch head, main, version recompute, hashes, digest):

  reserve       annotated tag at H and a DRAFT prerelease whose wheel, sdist
                and provenance assets are uploaded and read back byte for
                byte, binding the identity to H before PyPI (contents: write)
  stage-upload  copy the Python files still missing from PyPI for the OIDC
                upload action; id-token: write exists only in that job
  publish       wait for the PyPI readback, push the OCI archive by digest,
                move rc for release/new heads only when SDLC_RC_CHANNEL is
                exactly "enabled", write the staging selection record
                (packages: write) and emit the validated image digest and
                Python file hashes as job outputs
  finalize      after the attest job, re-verify everything and publish the
                draft (contents: write); last on purpose

A separate attest job, which never runs this script or parses the archives,
re-checks the files against those hashes and signs the attestations.

The repository has GitHub immutable releases enabled: a published release
can never gain, lose or replace an asset. The GitHub release therefore stays a
draft until every other surface is complete, so a published candidate release
always means a complete publication. A published release that lacks an asset
or carries different bytes cannot be repaired: that candidate is burned and
the next push to the branch yields the next rc number.
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
RELEASE_PAGE_SIZE = 100
RELEASE_PAGE_LIMIT = 50
BURNED = ("published GitHub release is incomplete and immutable: this candidate is burned; "
          "the next push to the branch yields the next rc number")
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


@dataclass(frozen=True)
class Release:
    """The one GitHub release whose tag_name is the candidate, draft or published."""
    draft: bool
    assets: dict[str, bytes]


def tag_message(verified: Verified) -> str:
    """The annotated candidate tag message; it binds H and every asset's bytes.

    Exact format, lines joined by "\n" with no trailing newline:

        Candidate <version>
        H: <head>
        <empty line>
        sha256 <64 lowercase hex>  <asset name>    (one per asset, sorted by name)

    The read-only jobs cannot see draft releases, so this contents-readable
    message is what binds the bytes PyPI and GHCR receive to the reservation.
    """
    hashes = [f"sha256 {hashlib.sha256(data).hexdigest()}  {name}"
              for name, data in sorted(verified.assets.items())]
    return "\n".join([f"Candidate {verified.version}", f"H: {verified.head}", "", *hashes])


class Client(Protocol):
    def python_hashes(self, version: str) -> dict[str, str]: ...
    def image_digest(self, reference: str) -> str | None: ...
    def tag_head(self, tag: str) -> str | None: ...
    def tag_message(self, tag: str) -> str | None: ...
    def release(self, tag: str) -> Release | None: ...
    def reserve(self, verified: Verified) -> None: ...
    def publish_release(self, verified: Verified) -> None: ...
    def push_image(self, verified: Verified) -> None: ...
    def move_channel(self, verified: Verified) -> None: ...


def inventory(verified: Verified, client: Client, *, drafts_visible: bool = True
              ) -> tuple[dict[str, str], str | None, Release | None]:
    """Read every surface before writes; outages and ambiguous ownership fail closed.

    Draft releases are listed only to tokens with push access. The reserve and
    finalize jobs hold contents: write and see drafts; the pypi and publish
    jobs hold contents: read, so for them (drafts_visible=False) an absent
    release is not proof of absence and the annotated tag at H binds instead.
    The finalize job repeats the full draft-aware check before publishing.
    """
    python = client.python_hashes(verified.python_version)
    image = client.image_digest(verified.version)
    tag = client.tag_head(verified.version)
    release = client.release(verified.version)
    require(tag in (None, verified.head), "Git tag collision: different H")
    require(image in (None, verified.image_digest), "image digest collision")
    require(all(verified.python_hashes.get(n) == h for n, h in python.items()),
            "PyPI hash collision")
    if release is not None:
        require(tag == verified.head, "release is not bound to H")
        if not release.draft:
            # Immutable: a published release can never be completed or repaired.
            require(release.assets == verified.assets, BURNED)
        require(all(verified.assets.get(n) == data for n, data in release.assets.items()),
                "GitHub release asset collision: a draft asset differs from the verified "
                "file; review and delete the draft asset or draft by hand")
    if python or image:
        # Objects that exist must have been published for this same H and bytes.
        if drafts_visible or release is not None:
            require(release is not None
                    and release.assets.get(PROVENANCE) == verified.assets[PROVENANCE],
                    "existing publication lacks same-H provenance")
        else:
            require(tag == verified.head, "existing publication lacks a tag at H")
    if tag is not None:
        # Exact match; only a trailing newline added by Git is tolerated.
        message = client.tag_message(verified.version)
        require(isinstance(message, str) and message.rstrip("\n") == tag_message(verified),
                "candidate tag does not bind these asset hashes; this H was reserved "
                "with other bytes or before the hash-binding tag format")
    return python, image, release


def reservation_bound(verified: Verified, client: Client) -> tuple[dict[str, str], str | None]:
    """Later read-only jobs: the tag binds H and the asset hashes (checked in
    inventory), and any visible release is complete."""
    python, image, release = inventory(verified, client, drafts_visible=False)
    require(client.tag_head(verified.version) == verified.head
            and (release is None or release.assets == verified.assets),
            "incomplete GitHub reservation")
    return python, image


def reserve(verified: Verified, client: Client) -> int:
    """Bind the identity to H (tag, draft prerelease, assets); return missing PyPI files.

    The release stays a draft: finalize publishes it only after PyPI, the image
    and the attestations are complete.
    """
    python, _, release = inventory(verified, client)
    if release is None or release.assets != verified.assets:
        client.reserve(verified)
        python, _, release = inventory(verified, client)
        require(release is not None and release.assets == verified.assets,
                "GitHub reservation readback mismatch")
    return len(set(verified.python_hashes) - set(python))


def stage_upload(verified: Verified, client: Client, upload: Path) -> int:
    """Copy only the verified files PyPI still lacks; the reservation must exist."""
    python, _ = reservation_bound(verified, client)
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
    _, image = reservation_bound(verified, client)
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


def finalize(verified: Verified, client: Client) -> None:
    """Publish the draft release last, after every other surface is complete.

    Immutable releases freeze assets on publication, so this repeats the full
    draft-aware inventory and requires all assets, the PyPI files and the image
    digest before the draft becomes a public prerelease.
    """
    python, image, release = inventory(verified, client)
    if release is None or release.assets != verified.assets:
        raise ValueError("incomplete GitHub reservation")
    require(python == verified.python_hashes, "PyPI publication is incomplete")
    require(image == verified.image_digest, "remote image digest mismatch")
    if release.draft:
        client.publish_release(verified)
        _, _, release = inventory(verified, client)
    require(release is not None and not release.draft and release.assets == verified.assets,
            "GitHub release publication readback mismatch")


def selection(record: dict[str, Any], history: Path) -> dict[str, Any]:
    """Branch-based staging selection (STD-U-821 1.2) from freshly fetched refs."""
    hotfix = Git(history).run(
        "for-each-ref", "--format=%(objectname)", "refs/heads/hotfix/new",
    ).strip() or None
    selected = hotfix == record["H"] if record["branch"] == "hotfix/new" else hotfix is None
    require(record["branch"] != "hotfix/new" or selected, "hotfix head moved during publication")
    return record | {"selected": selected,
                     "selection_branch": "hotfix/new" if hotfix else "release/new"}


STDERR_EXCERPT_LIMIT = 300
_REDACTED = "[REDACTED]"
_TERMINAL_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(authorization)\s*:\s*(?:(?:bearer|token|basic)\s+)?\S+"),
     rf"\1: {_REDACTED}"),
    (re.compile(r"(?i)\b(bearer)\s+\S+"), rf"\1 {_REDACTED}"),
    (re.compile(r"(?i)\b(token)(\s*[:=]?\s+|\s*[:=]\s*)[A-Za-z0-9._~+/=-]{8,}"),
     rf"\1\2{_REDACTED}"),
    (re.compile(r"github_pat_[A-Za-z0-9_]+"), _REDACTED),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), _REDACTED),
    (re.compile(r"[^\s/@:]+:[^\s/@]+@"), f"{_REDACTED}@"),
    (re.compile(r"[A-Za-z0-9+/=_-]{40,}"), _REDACTED),
)


def _stderr_excerpt(raw: bytes) -> str:
    """Single-line, allowlisted, redacted and truncated view of process stderr.

    gh and the registries can reflect remote text, so only printable ASCII
    survives, credential-shaped text is redacted, GitHub Actions workflow
    command markers are broken up, and the result is capped.
    """
    text = raw.decode("utf-8", errors="replace")
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name)
        if value:
            text = text.replace(value, _REDACTED)
    text = _TERMINAL_ESCAPES.sub(" ", text)
    text = " ".join(re.sub(r"[^\x20-\x7e]", " ", text).split())
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name)
        if value:
            text = text.replace(value, _REDACTED)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    while "::" in text or "##[" in text:
        text = text.replace("::", ": :").replace("##[", "# #[")
    if len(text) > STDERR_EXCERPT_LIMIT:
        text = text[:STDERR_EXCERPT_LIMIT] + "..."
    return text


def command(*args: str, input_data: bytes | None = None, absent: str | None = None
            ) -> bytes | None:
    result = subprocess.run(args, input=input_data, capture_output=True, check=False, timeout=600)
    if result.returncode:
        if absent and re.search(absent, result.stderr.decode(errors="replace")):
            return None
        # Stdout, arguments and input are never echoed. Stderr appears only as a
        # sanitized excerpt: registries and gh can reflect remote, attacker
        # influenced text, hence the allowlist, redaction and truncation.
        message = f"{args[0]} operation failed (exit {result.returncode})"
        excerpt = _stderr_excerpt(result.stderr)
        raise ValueError(f"{message}: {excerpt}" if excerpt else message)
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

    def api(self, path: str, payload: dict[str, Any] | None = None, method: str = "POST"
            ) -> Any:
        args = ["gh", "api", f"repos/{REPOSITORY}/{path}"]
        if payload is not None:
            args += ["--method", method, "--input", "-"]
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

    def tag_object(self, tag: str) -> dict[str, Any] | None:
        result = self.api(f"git/ref/tags/{tag}")
        if result is None:
            return None
        obj = result["object"]
        require(obj["type"] == "tag", "candidate tag must be annotated")
        tag_data = self.api(f"git/tags/{obj['sha']}")
        require(isinstance(tag_data, dict) and tag_data["object"]["type"] == "commit",
                "candidate tag must point directly to a commit")
        return dict(tag_data)

    def tag_head(self, tag: str) -> str | None:
        tag_data = self.tag_object(tag)
        return None if tag_data is None else str(tag_data["object"]["sha"])

    def tag_message(self, tag: str) -> str | None:
        tag_data = self.tag_object(tag)
        if tag_data is None:
            return None
        message = tag_data.get("message")
        require(isinstance(message, str), "unreadable candidate tag message")
        return str(message)

    def find_release(self, tag: str) -> dict[str, Any] | None:
        """List releases (drafts too, given push access); match tag_name exactly.

        GET releases/tags/{tag} never returns a draft, so the listing is the
        only uniform view. More than one match is ambiguous and fails closed.
        """
        found: list[dict[str, Any]] = []
        for page in range(1, RELEASE_PAGE_LIMIT + 1):
            batch = self.api(f"releases?per_page={RELEASE_PAGE_SIZE}&page={page}")
            require(isinstance(batch, list) and all(isinstance(r, dict) for r in batch),
                    "unreadable release listing")
            found += [r for r in batch if r.get("tag_name") == tag]
            if len(batch) < RELEASE_PAGE_SIZE:
                break
        else:
            raise ValueError("release listing exceeds the page limit")
        require(len(found) <= 1, "ambiguous releases for the candidate tag")
        return found[0] if found else None

    def release(self, tag: str) -> Release | None:
        found = self.find_release(tag)
        if found is None:
            return None
        require(found.get("prerelease") is True and type(found.get("draft")) is bool,
                "existing release is not a prerelease")
        result: dict[str, bytes] = {}
        for asset in found["assets"]:
            name, asset_id = asset["name"], asset["id"]
            require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", name) is not None
                    and name not in result and type(asset_id) is int,
                    "invalid release asset name")
            # An interrupted upload leaves a non-uploaded asset; never guess its bytes.
            require(asset.get("state") == "uploaded",
                    "release asset upload is incomplete; review and delete the draft "
                    "asset by hand")
            data = command("gh", "api", "-H", "Accept: application/octet-stream",
                           f"repos/{REPOSITORY}/releases/assets/{asset_id}")
            assert data is not None
            result[name] = data
        return Release(found["draft"], result)

    def reserve(self, verified: Verified) -> None:
        if self.tag_head(verified.version) is None:
            tag = self.api("git/tags", {"tag": verified.version,
                           "message": tag_message(verified),
                           "object": verified.head, "type": "commit"})
            self.api("git/refs", {"ref": f"refs/tags/{verified.version}", "sha": tag["sha"]})
        current = self.release(verified.version)
        if current is None:
            # Draft first: immutability applies only once a release is published.
            command("gh", "release", "create", verified.version, "--repo", REPOSITORY,
                    "--draft", "--verify-tag", "--prerelease", "--title", verified.version,
                    "--notes", f"Candidate from {verified.head}. See {PROVENANCE}.")
            current = self.release(verified.version)
            if current is None or not current.draft:
                raise ValueError("draft release was not created")
        require(current.draft, BURNED)
        # Never --clobber. Partial uploads are reconciled by byte readback.
        for name, path in verified.files.items():
            if name not in current.assets:
                command("gh", "release", "upload", verified.version, str(path),
                        "--repo", REPOSITORY)

    def publish_release(self, verified: Verified) -> None:
        found = self.find_release(verified.version)
        if found is None or found.get("draft") is not True or type(found.get("id")) is not int:
            raise ValueError("no draft release to publish")
        self.api(f"releases/{found['id']}",
                 {"draft": False, "prerelease": True, "make_latest": "false"}, method="PATCH")

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
    # Never start a log line with "::" (workflow commands) or span lines. The
    # cap leaves room for a command failure with its full stderr excerpt.
    return re.sub(r"[^\x20-\x7e]", "?", text)[:400]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("reserve", "stage-upload", "publish", "finalize"))
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
        if args.phase == "finalize":
            finalize(verified, client)
        elif args.phase in ("reserve", "stage-upload"):
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
