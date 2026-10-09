#!/usr/bin/env python3
"""Prove a reviewed squash and emit a release record without repository writes.

The candidate's M is the pre-squash main. Notes use precisely the engine's
non-merge range and grammar. The adapter gathers Git facts for pure validators.

Phase "initial" runs once, right after the promotion lock is acquired: main
head must equal S and the stable tag must be absent. Phase "resume" is the
recheck in later jobs, which may be re-run after a partial publication: S must
still be reachable from main, and the stable tag may exist only as an
annotated tag on S whose message is the generated notes and whose tagger is the
release App. Phase "resume-pypi" replaces "initial" when a promotion stopped
after its PyPI upload (dispatch input resume_pypi): S must be reachable from
main, which may have advanced, and the stable tag is accepted exactly as in
"resume". Every other proof is identical in all phases.

The stable tag on S is a released version to the engine, so once it exists
the recomputed release would see a stable version above its own base (an
adoption record names the highest stable tag, a hotfix the current one). The
resume phases therefore pass that one tag, and nothing else, as ignore_tags,
and only after verifying it: annotated, its object is S, its tag field names
it, its tagger is the release App, and (once the notes exist) its message is
byte for byte the generated notes. Any other tag state fails closed.

The "resume-state" command proves the external state resume-pypi requires:
PyPI already holds exactly the stable wheel and sdist. Without a stable tag,
the GHCR version tag, the stable and latest channels, the GitHub release and
the Git tag show that nothing after PyPI was published. With the stable tag the
proof verified, the Git tag on GitHub must be that very tag object, the GHCR
version tag absent or on the candidate digest, and a channel may be on the
candidate only if the version tag is (the order promote-image writes them);
a GitHub release visible to the job's token must already be this release's
(notes, not a prerelease, only this run's assets), so a foreign one is refused
before any channel moves; the release job checks every byte again. The
"verify-pypi" command compares the stable files built from S with PyPI,
ignoring nothing but the upload's attestation files, which must be present
exactly when the upload ran.

The "release" command completes or verifies the GitHub release: its body must
be the generated notes and every asset must be one of this run's verified files
with the same SHA-256. Missing assets are uploaded without replacement.

The "checks" command decides whether the exact-source check runs of the
reviewed H or the squash S satisfy the promotion gate (see check_gate).

Immutable releases are enabled on the repository, so assets can be added only
while the release is a draft. The workflow creates the release as a draft;
this command uploads the missing assets, verifies every byte, and only then
publishes it. A published release that lacks an asset cannot be completed: the
version is burned and needs a new stable version.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.rc_decide import main_ratification_kwargs  # noqa: E402
from scripts.release_artifacts import CandidateVersion  # noqa: E402
from scripts.sdlc_version import Git, VersionError, _impact, compute_version  # noqa: E402
from scripts.version_free import _gh_release_present  # noqa: E402

SHA = re.compile(r"[0-9a-f]{40}")
HASH = re.compile(r"[0-9a-f]{64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
NUMBER = r"(?:0|[1-9][0-9]*)"
CANDIDATE = re.compile(rf"{NUMBER}\.{NUMBER}\.{NUMBER}-(?:hotfix\.)?rc\.[1-9][0-9]*")
TAGGER = re.compile(r"[^<>\n]+ <[^<>\n]+>")
ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


def _match(pattern: re.Pattern, value: object, name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")
    return value


def validate_inputs(*, squash: str, head: str, candidate_tag: str) -> None:
    """Reject option-like refs, abbreviated SHAs and output injections."""
    _match(SHA, squash, "S")
    _match(SHA, head, "H")
    _match(CANDIDATE, candidate_tag, "candidate tag")


def validate_candidate(provenance: dict, *, head: str, candidate_tag: str) -> CandidateVersion:
    """Validate the stage-one receipt before using its values as coordinates."""
    if not isinstance(provenance, dict):
        raise ValueError("candidate provenance must be an object")
    _match(CANDIDATE, candidate_tag, "candidate tag")
    identity = CandidateVersion.from_tag(candidate_tag)
    if provenance.get("H") != head:
        raise ValueError("reviewed H differs from recorded H")
    for name in ("B", "H", "M", "source_sha"):
        _match(SHA, provenance.get(name), name)
    if provenance["source_sha"] != head:
        raise ValueError("candidate source_sha differs from recorded H")
    if provenance.get("candidate_tag") != candidate_tag:
        raise ValueError("candidate tag differs from provenance")
    if provenance.get("python_version") != identity.pypi_version:
        raise ValueError("candidate Python version differs from provenance")
    if type(provenance.get("N")) is not int or provenance["N"] != identity.number:
        raise ValueError("candidate N must equal its positive RC number")
    if provenance.get("promotable") is not True:
        raise ValueError("candidate must be promotable")
    if provenance.get("dry_run") is not False:
        raise ValueError("candidate must explicitly not be a dry_run")
    _match(DIGEST, provenance.get("image_digest"), "image_digest")
    _match(HASH, provenance.get("oci_archive_sha256"), "oci_archive_sha256")
    receipt = provenance.get("candidate")
    if not isinstance(receipt, dict):
        raise ValueError("candidate receipt must be an object")
    if receipt.get("source_sha") != head or receipt.get("version") != identity.pypi_version:
        raise ValueError("candidate receipt source/version mismatch")
    for name in ("source_tree_sha256", "lock_sha256", "wheel_sha256", "sdist_sha256"):
        _match(HASH, receipt.get(name), name)
    for name in ("wheel", "sdist"):
        _match(re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*"), receipt.get(name), name)
    return identity


def render_notes(*, target: str, messages: list[str]) -> str:
    """Pure deterministic notes, retaining commit bodies and migration details."""
    sections: dict[int, list[str]] = {3: [], 2: [], 1: [], 0: []}
    for message in messages:
        impact = _impact(message)
        lines = message.rstrip("\n").splitlines()
        entry = "- " + lines[0]
        if len(lines) > 1:
            entry += "\n" + "\n".join("  " + line if line else "" for line in lines[1:])
        sections[impact].append(entry)
    parts = [f"# Release {target}"]
    for impact, title in ((3, "Breaking changes"), (2, "Features"), (1, "Fixes"),
                          (0, "Other changes")):
        if sections[impact]:
            parts.append(f"## {title}\n\n" + "\n\n".join(sections[impact]))
    return "\n\n".join(parts) + "\n"


def generate_notes(*, cwd: str | Path, version: dict) -> str:
    git = Git(cwd)
    # Keep the range identical even for engine callers preparing adoption notes.
    start = git.resolve(f"refs/tags/{version['base']}") if "adoption" in version else version["B"]
    raw = git.run("log", "--no-merges", "-z", "--format=%B", f"{start}..{version['H']}")
    return render_notes(target=version["target"], messages=raw.split("\0")[:-1])


PHASES = ("initial", "resume", "resume-pypi")


def validate_tag_header(
    raw: str | None, *, squash: str, tag: str, tagger: str | None,
) -> str:
    """Check everything of an existing stable tag object but its message.

    Returns the message. The message is the generated notes, which exist only
    after the engine ran; validate_tag_object adds that comparison.
    """
    if tagger is None:
        raise ValueError("an existing stable tag requires the expected tagger identity")
    _match(TAGGER, tagger, "tagger")
    if not isinstance(raw, str):
        raise ValueError("existing stable tag object is unreadable")
    header, separator, message = raw.partition("\n\n")
    fields: dict[str, str] = {}
    for line in header.split("\n"):
        key, _, value = line.partition(" ")
        if key in fields:
            raise ValueError("existing stable tag has a duplicate header")
        fields[key] = value
    if (fields.get("object") != squash or fields.get("type") != "commit"
            or fields.get("tag") != tag):
        raise ValueError("existing stable tag object does not name S")
    stamp = re.escape(tagger) + r" [0-9]+ [+-][0-9]{4}"
    if re.fullmatch(stamp, fields.get("tagger", "")) is None:
        raise ValueError("existing stable tag tagger differs from the release App")
    if not separator:
        raise ValueError("existing stable tag message differs from generated notes")
    return message


def validate_tag_object(
    raw: str | None, *, squash: str, tag: str, notes: str, tagger: str | None,
) -> None:
    """Accept an existing stable tag only as this workflow's own annotated tag."""
    if validate_tag_header(raw, squash=squash, tag=tag, tagger=tagger) != notes:
        raise ValueError("existing stable tag message differs from generated notes")


def verified_ignore_tags(
    *, phase: str, squash: str, tag: str, tag_exists: bool, tag_target: str | None,
    tag_annotated: bool, tag_object: str | None, tagger: str | None,
) -> frozenset[str]:
    """The engine's ignore_tags: this release's own tag, once verified, else nothing.

    Runs before the engine, so it checks every property but the message;
    validate_proof then requires the message to be the generated notes. A tag
    that fails any check is never ignored: the proof fails closed here.
    """
    if not tag_exists:
        return frozenset()
    if phase == "initial" or tag_target != squash or not tag_annotated:
        raise ValueError("stable tag already exists")
    validate_tag_header(tag_object, squash=squash, tag=tag, tagger=tagger)
    return frozenset({tag})


def validate_proof(
    *, version: dict, provenance: dict, squash: str, head: str, candidate_tag: str,
    parents: list[str], head_tree: str, squash_tree: str, main_head: str,
    rc_head: str, message: str, notes: str, adoption_present: bool, tag_exists: bool,
    phase: str = "initial", main_contains_squash: bool = False,
    tag_target: str | None = None, tag_annotated: bool = False,
    tag_object: str | None = None, tagger: str | None = None,
    ignored_tags: frozenset[str] = frozenset(),
) -> dict:
    """Pure validation of immutable Git facts and the recomputed engine result.

    ignored_tags are the tags the engine result was computed without; only
    this release's own stable tag may be among them, and only when it exists
    and passes the full tag validation below.
    """
    if phase not in PHASES:
        raise ValueError("phase must be initial or resume")
    validate_inputs(squash=squash, head=head, candidate_tag=candidate_tag)
    identity = validate_candidate(provenance, head=head, candidate_tag=candidate_tag)
    if adoption_present:
        raise ValueError("release/ADOPTION.json must be absent at H")
    if version.get("promotable") is not True:
        raise ValueError("engine result must be promotable")
    if version.get("B") != provenance["B"]:
        raise ValueError("computed B differs from RC recorded B")
    for name in ("H", "M", "N"):
        if version.get(name) != provenance[name]:
            raise ValueError(f"computed {name} differs from RC provenance")
    if (version.get("candidate") != candidate_tag or version.get("target") != identity.base
            or version.get("pypi_version") != identity.pypi_version):
        raise ValueError("engine version differs from candidate coordinates")
    if parents != [provenance["B"]]:
        raise ValueError("S must have the RC recorded B as its sole parent")
    if head_tree != squash_tree:
        raise ValueError("reviewed H and squash S tree mismatch")
    if phase == "initial" and main_head != squash:
        raise ValueError("main head no longer equals S")
    if phase != "initial" and main_head != squash and not main_contains_squash:
        raise ValueError("S is no longer reachable from main head")
    if rc_head != head:
        raise ValueError("RC tag does not resolve to recorded H")
    subject, separator, body = message.rstrip("\n").partition("\n\n")
    if subject != f"release: {identity.base}":
        raise ValueError("squash subject differs from engine release target")
    if not separator or body != notes.rstrip("\n"):
        raise ValueError("squash release notes differ from generated notes")
    if ignored_tags and (ignored_tags != {identity.stable_tag} or not tag_exists
                         or phase == "initial"):
        raise ValueError("only this release's own existing stable tag may be ignored")
    if tag_exists:
        if phase == "initial" or tag_target != squash or not tag_annotated:
            raise ValueError("stable tag already exists")
        validate_tag_object(tag_object, squash=squash, tag=identity.stable_tag,
                            notes=notes, tagger=tagger)
    return {
        "schema_version": 1, "H": head, "S": squash, "B": provenance["B"],
        "M": provenance["M"], "tag": identity.stable_tag, "target": identity.base,
        "candidate_tag": candidate_tag, "candidate_version": identity.pypi_version,
        "image_digest": provenance["image_digest"], "notes": notes,
    }


def _compute(git: Git, *, cwd: str | Path, head: str, main: str, branch: str,
             ignore_tags: frozenset[str] = frozenset()) -> dict:
    """Recompute, taking adoption ratification only from main's blobs.

    This checkout is the squash S, whose tree equals the reviewed H, so its own
    scripts/adoption_ratification.py and vendored amendment are candidate data.
    For an adoption cut the pinned values are read from the RC's recorded main
    M (the main stage 2 ran from), never from S or H; the engine validates them.
    """
    with tempfile.TemporaryDirectory(prefix="adoption-ratification-") as trusted:
        return compute_version(cwd=cwd, head=head, main=main, branch=branch,
                               ignore_tags=ignore_tags,
                               **main_ratification_kwargs(git, head=head, main=main,
                                                          trusted_dir=Path(trusted)))


def prove_release(
    *, cwd: str | Path, squash: str, head: str, candidate_tag: str,
    provenance: dict, main: str = "origin/main", phase: str = "initial",
    tagger: str | None = None,
) -> dict:
    """Read Git facts and recompute against the RC's frozen pre-squash main."""
    if phase not in PHASES:
        raise ValueError("phase must be initial, resume or resume-pypi")
    validate_inputs(squash=squash, head=head, candidate_tag=candidate_tag)
    identity = validate_candidate(provenance, head=head, candidate_tag=candidate_tag)
    git = Git(cwd)
    if git.file(head, "release/ADOPTION.json") is not None:
        raise ValueError("release/ADOPTION.json must be absent at H")
    branch = "hotfix/new" if "-hotfix.rc." in candidate_tag else "release/new"
    # Tag facts first: the engine may ignore the stable tag only once verified.
    tag_ref = f"refs/tags/{identity.stable_tag}"
    tag_exists = identity.stable_tag in git.run("tag", "--list").splitlines()
    tag_target = git.resolve(tag_ref) if tag_exists else None
    tag_annotated = tag_exists and git.run("cat-file", "-t", tag_ref).strip() == "tag"
    tag_object = git.run("cat-file", "tag", tag_ref) if tag_annotated else None
    ignored = verified_ignore_tags(
        phase=phase, squash=squash, tag=identity.stable_tag, tag_exists=tag_exists,
        tag_target=tag_target, tag_annotated=tag_annotated, tag_object=tag_object,
        tagger=tagger)
    version = _compute(git, cwd=cwd, head=head, main=provenance["M"], branch=branch,
                       ignore_tags=ignored)
    notes = generate_notes(cwd=cwd, version=version)
    main_head = git.resolve(main)
    return validate_proof(
        version=version, provenance=provenance, squash=squash, head=head,
        candidate_tag=candidate_tag,
        parents=git.run("rev-list", "--parents", "-n", "1", squash).split()[1:],
        head_tree=git.run("rev-parse", f"{head}^{{tree}}").strip(),
        squash_tree=git.run("rev-parse", f"{squash}^{{tree}}").strip(),
        main_head=main_head, rc_head=git.resolve(f"refs/tags/{candidate_tag}"),
        message=git.run("show", "-s", "--format=%B", squash), notes=notes,
        adoption_present=False, tag_exists=tag_exists, phase=phase,
        main_contains_squash=git.ancestor(squash, main_head),
        tag_target=tag_target, tag_annotated=tag_annotated,
        tag_object=tag_object, tagger=tagger, ignored_tags=ignored,
    )


def env_lines(record: dict) -> str:
    """Only validated single-line fields may enter GITHUB_OUTPUT."""
    identity = CandidateVersion.from_tag(_match(CANDIDATE, record["candidate_tag"], "RC tag"))
    if record["tag"] != identity.stable_tag or record["target"] != identity.base:
        raise ValueError("record tag/version mismatch")
    sha = _match(SHA, record["S"], "S")
    digest = _match(DIGEST, record["image_digest"], "image_digest")
    return (f"tag={identity.stable_tag}\nversion={identity.base}\nsource_sha={sha}\n"
            f"image_digest={digest}\nrc_tag={identity.git_tag}\n")


CHECK_ROLES = ("head", "squash")
CHECK_APPS = ("github-actions", "github-code-scanning")
# GitHub publishes the aggregate CodeQL check run from the advanced security
# app (the only observed producer); code scanning is kept as the alternate slug.
# A workflow (github-actions) can post any check run name, so it is refused.
CODEQL_APPS = ("github-advanced-security", "github-code-scanning")


def _check_apps(name: str) -> tuple[str, ...]:
    return CODEQL_APPS if name == "CodeQL" else CHECK_APPS


def _check_names(analyses: str) -> list[str]:
    if not isinstance(analyses, str):
        raise ValueError("invalid required analysis names")
    names = analyses.split(",")
    if any(not name.strip() or name != name.strip() for name in names):
        raise ValueError("invalid required analysis names")
    return names


def required_checks(role: str, analyses: str) -> set[str]:
    """Check run names the promotion gate requires on H ("head") or S ("squash").

    GitHub produces the aggregate "CodeQL" check run only on pull request
    heads; a push to main gets only the per-language analyses. H, the head of
    the release pull request, therefore needs "test" and the aggregate
    "CodeQL"; S, the push to main, needs "test" and every configured language
    analysis. The proof shows that the tree of S equals the tree of H, so the
    aggregate result on H and the analyses of S cover the same code. The
    configured list is validated for both roles, so an empty or malformed
    variable fails closed.
    """
    if role not in CHECK_ROLES:
        raise ValueError("check role must be head or squash")
    names = _check_names(analyses)
    if role == "head":
        return {"test", "CodeQL"}
    return {"test", *names}


def check_gate(pages: object, *, sha: str, role: str, analyses: str) -> None:
    """Require the latest matching run of every required check to have succeeded.

    pages is the output of `gh api --paginate --slurp .../check-runs`. A run
    counts only when its head_sha is exactly sha and its app is GitHub Actions
    or code scanning (for the aggregate "CodeQL", only GitHub Advanced
    Security or code scanning); among those, the highest id (the latest run) decides, and only
    status "completed" with conclusion "success" passes (neutral, skipped,
    cancelled, timed_out, action_required and stale do not). A check that is
    not required for the role (the analyses on H, "CodeQL" on S) is optional,
    but one that did run must also have succeeded.
    """
    _match(SHA, sha, "check SHA")
    required = required_checks(role, analyses)
    optional = {"test", "CodeQL", *_check_names(analyses)} - required
    if not isinstance(pages, list) or not all(
            isinstance(page, dict) and isinstance(page.get("check_runs"), list)
            for page in pages):
        raise ValueError("unreadable check runs")
    runs = [run for page in pages for run in page["check_runs"]]
    if not all(isinstance(run, dict) for run in runs):
        raise ValueError("unreadable check runs")
    for name in sorted(required | optional):
        matches = [run for run in runs if run.get("name") == name
                   and run.get("head_sha") == sha
                   and isinstance(run.get("app"), dict)
                   and run["app"].get("slug") in _check_apps(name)
                   and type(run.get("id")) is int]
        if not matches:
            if name in optional:
                continue
            raise ValueError("missing required check: " + name)
        latest = max(matches, key=lambda run: run["id"])
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            raise ValueError("required check did not succeed: " + name)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expected_assets(
    record: dict, stable_provenance: dict, files: list[Path], *, tag: str, notes: str,
) -> dict[str, str]:
    """Bind every release asset to the release record and the verified stable build."""
    if record.get("tag") != tag or record.get("notes") != notes:
        raise ValueError("release tag or notes differ from the release record")
    if not isinstance(stable_provenance, dict):
        raise ValueError("stable provenance must be an object")
    for key in ("H", "S", "B", "M", "tag", "candidate_tag", "image_digest"):
        if stable_provenance.get(key) != record.get(key):
            raise ValueError(f"stable provenance {key} differs from the release record")
    stable = stable_provenance.get("stable")
    if not isinstance(stable, dict) or stable.get("source_sha") != record["S"]:
        raise ValueError("stable provenance does not describe a build of S")
    expected: dict[str, str] = {}
    for path in files:
        name = _match(ASSET, path.name, "release asset name")
        if name in expected:
            raise ValueError(f"duplicate release asset {name}")
        expected[name] = _sha256(path)
    for kind in ("wheel", "sdist"):
        name = _match(ASSET, stable.get(kind), f"stable {kind}")
        if expected.get(name) != _match(HASH, stable.get(f"{kind}_sha256"), f"{kind} hash"):
            raise ValueError(f"stable {kind} asset differs from the stable provenance")
    for name in ("release-provenance.json", "release-record.json"):
        if name not in expected:
            raise ValueError(f"release asset {name} is required")
    if len(expected) != 4:
        raise ValueError("release assets must be exactly the stable files and both records")
    return expected


PYPI_PROJECT = "data-olympus"
ATTESTATION = ".publish.attestation"
PYPI_ATTEMPTS = 12
PYPI_DELAY = 5.0
CHANNELS = ("stable", "latest")
IMAGE = "ghcr.io/knaisoma/data-olympus"
STABLE_VERSION = re.compile(rf"{NUMBER}\.{NUMBER}\.{NUMBER}")


def _stable_version(version: object) -> str:
    return _match(STABLE_VERSION, version, "stable version")


def stable_dist_kind(name: str, version: str) -> str | None:
    """Return "wheel" or "sdist" for this version's distribution names, else None."""
    project = "data_olympus-" + re.escape(_stable_version(version))
    if re.fullmatch(project + r"-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl",
                    name):
        return "wheel"
    if re.fullmatch(project + r"\.tar\.gz", name):
        return "sdist"
    return None


def pypi_files(payload: object, *, version: str) -> dict[str, str]:
    """Map the files of one PyPI release (its JSON API "urls") to their SHA-256.

    The release must be this version, every file must carry a readable name and
    SHA-256 digest, none may be yanked, and names are unique.
    """
    _stable_version(version)
    if (not isinstance(payload, dict) or not isinstance(payload.get("info"), dict)
            or not isinstance(payload.get("urls"), list)):
        raise ValueError("unreadable PyPI release")
    if payload["info"].get("version") != version:
        raise ValueError("PyPI release version differs from the stable version")
    files: dict[str, str] = {}
    for entry in payload["urls"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("digests"), dict):
            raise ValueError("unreadable PyPI release file")
        name = _match(ASSET, entry.get("filename"), "PyPI file name")
        if name in files:
            raise ValueError(f"PyPI release lists {name} twice")
        if entry.get("yanked") is not False:
            raise ValueError(f"PyPI file {name} is yanked or has no yanked state")
        files[name] = _match(HASH, entry["digests"].get("sha256"), "PyPI file hash")
    return files


def require_stable_inventory(files: dict[str, str], *, version: str) -> dict[str, str]:
    """Exactly one wheel and one sdist of this version, nothing else: {kind: name}."""
    kinds: dict[str, str] = {}
    for name in sorted(files):
        kind = stable_dist_kind(name, version)
        if kind is None or kind in kinds:
            raise ValueError(f"PyPI release holds an unexpected file: {name}")
        kinds[kind] = name
    missing = sorted({"wheel", "sdist"} - set(kinds))
    if missing:
        raise ValueError("PyPI release lacks the stable " + " and ".join(missing))
    return kinds


def local_stable_files(dist: Path, stable_provenance: object, *, version: str,
                       attestations: bool) -> dict[str, str]:
    """The built wheel and sdist of S with their SHA-256, after checking the directory.

    The directory must hold exactly the wheel and sdist named by the stable
    provenance, with its hashes, and, when the upload ran, the two
    `<file>.publish.attestation` files the PyPI publish action writes next to
    them (non-empty). Without an upload they must be absent. Anything else in
    the directory fails closed.
    """
    if not isinstance(stable_provenance, dict) or not isinstance(
            stable_provenance.get("stable"), dict):
        raise ValueError("stable provenance does not describe a stable build")
    stable = stable_provenance["stable"]
    if stable.get("version") != _stable_version(version):
        raise ValueError("stable provenance version differs from the stable version")
    expected: dict[str, str] = {}
    for kind in ("wheel", "sdist"):
        name = _match(ASSET, stable.get(kind), f"stable {kind}")
        if stable_dist_kind(name, version) != kind:
            raise ValueError(f"stable {kind} name is not this version's {kind}")
        expected[name] = _match(HASH, stable.get(f"{kind}_sha256"), f"stable {kind} hash")
    allowed = set(expected)
    if attestations:
        allowed |= {name + ATTESTATION for name in expected}
    present = {path.name: path for path in dist.iterdir()}
    unexpected = sorted(set(present) - allowed)
    if unexpected:
        raise ValueError("unexpected files next to the stable distributions: "
                         + ", ".join(unexpected))
    missing = sorted(allowed - set(present))
    if missing:
        raise ValueError("missing next to the stable distributions: " + ", ".join(missing))
    for name, path in present.items():
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"{name} is not a regular file")
        if name.endswith(ATTESTATION):
            if path.stat().st_size == 0:
                raise ValueError(f"{name} is empty")
        elif _sha256(path) != expected[name]:
            raise ValueError(f"{name} differs from the stable provenance")
    return expected


def compare_published(local: dict[str, str], remote: dict[str, str]) -> None:
    """PyPI must hold exactly the local wheel and sdist, byte for byte."""
    if remote != local:
        foreign = sorted(set(remote) - set(local))
        absent = sorted(set(local) - set(remote))
        differ = sorted(name for name in set(local) & set(remote)
                        if local[name] != remote[name])
        raise ValueError("stable PyPI files do not match the build of S: "
                         f"unexpected {foreign}, missing {absent}, different hash {differ}")


def fetch_pypi(version: str) -> object | None:
    """The PyPI JSON of one release, None when PyPI reports it absent (404)."""
    url = f"https://pypi.org/pypi/{PYPI_PROJECT}/{_stable_version(version)}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def verify_published(
    *, dist: Path, stable_provenance: object, version: str, attestations: bool,
    fetch: Callable[[str], object | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Check the local directory once, then wait for PyPI to list exactly those files.

    Local faults (extra files, missing attestations, wrong hashes) fail at
    once. PyPI's JSON can lag behind an upload, so an absent, unreadable or
    different listing is read again within PYPI_ATTEMPTS, then fails closed.
    """
    local = local_stable_files(dist, stable_provenance, version=version,
                               attestations=attestations)
    read = fetch_pypi if fetch is None else fetch
    for attempt in range(PYPI_ATTEMPTS):
        try:
            payload = read(version)
            if payload is None:
                raise ValueError(f"PyPI does not list data-olympus {version}")
            compare_published(local, pypi_files(payload, version=version))
            return
        except (OSError, ValueError):
            if attempt == PYPI_ATTEMPTS - 1:
                raise
            sleep(PYPI_DELAY)


def validate_resume_state(
    *, version: str, image_digest: str, pypi: object | None, ghcr_tag: str | None,
    channels: dict[str, str | None], release: bool | None,
    release_tags: list[str] | None, git_tag: str | None, tag_object: str = "",
    release_view: object = False, notes: str | None = None,
    record_sha256: str | None = None,
) -> dict[str, str]:
    """Pure check of what resume_pypi needs: PyPI done, nothing unexpected after it.

    pypi is the PyPI JSON of the stable version (None: absent). ghcr_tag and
    channels map the GHCR version tag and the stable and latest channels to
    their current digest ("" when explicitly absent, None when unreadable).
    git_tag is the object the GitHub ref of the stable tag names ("" absent,
    None unreadable). tag_object is the annotated tag object the proof verified
    in this job ("" when the proof saw no stable tag). release is True, False
    or None (unreadable, which fails closed) for a release visible by tag;
    release_tags lists the tag names of every release the token can list;
    drafts appear only to a token with push access, so a read-only token
    probably misses them and the release job's refusal of a foreign draft is
    the backstop.

    Without a stable tag nothing after PyPI may exist. With the verified tag
    the states promote-image and the release job leave are accepted: the
    version tag absent or on the candidate digest (never on another one), a
    channel on the candidate only when the version tag is, since promote-image
    writes the version tag first. A release visible to this token
    (release_view: the `gh release view` JSON, False when not found, None when
    unreadable) must already be this release's, checked before promote-image
    can move a channel (validate_visible_release); the release job checks
    every byte again. Returns the PyPI inventory {kind: name}.
    """
    _stable_version(version)
    _match(DIGEST, image_digest, "image digest")
    if tag_object:
        _match(SHA, tag_object, "verified tag object")
    tag = f"v{version}"
    if pypi is None:
        raise ValueError(f"PyPI does not hold data-olympus {version}: resume_pypi is "
                         "impossible; dispatch without it (the normal path)")
    inventory = require_stable_inventory(pypi_files(pypi, version=version), version=version)
    facts = (("GHCR tag " + tag, ghcr_tag), ("GitHub release " + tag, release),
             ("Git tag " + tag, git_tag))
    for name, present in facts:
        if present is None:
            raise ValueError(f"cannot read the {name} (fail closed)")
    if release_tags is None:
        raise ValueError("cannot list the GitHub releases (fail closed)")
    if git_tag != tag_object:
        raise ValueError(f"Git tag {tag} on GitHub is not the tag the proof verified "
                         f"(GitHub: {git_tag or 'absent'}, verified: {tag_object or 'none'})")
    if set(channels) != set(CHANNELS):
        raise ValueError("stable and latest channel digests are required")
    for channel in CHANNELS:
        digest = channels[channel]
        if digest is None:
            raise ValueError(f"cannot read the GHCR {channel} channel (fail closed)")
        if digest and DIGEST.fullmatch(digest) is None:
            raise ValueError(f"GHCR {channel} channel digest is unreadable")
    if not tag_object:
        if ghcr_tag:
            raise ValueError(f"GHCR tag {tag} already exists: resume_pypi before the stable "
                             "tag is refused")
        if release:
            raise ValueError(f"GitHub release {tag} already exists before the stable tag")
        if tag in release_tags:
            raise ValueError(f"a GitHub release for {tag} is listed (the token lists drafts "
                             "only with push access)")
        for channel in CHANNELS:
            if channels[channel] == image_digest:
                raise ValueError(f"GHCR {channel} already points at the candidate digest")
        return inventory
    if ghcr_tag not in ("", image_digest):
        raise ValueError(f"GHCR tag {tag} points at another digest than the candidate")
    for channel in CHANNELS:
        if channels[channel] == image_digest and ghcr_tag != image_digest:
            raise ValueError(f"GHCR {channel} points at the candidate digest but {tag} "
                             "does not")
    if release_view is None:
        raise ValueError(f"cannot read the GitHub release {tag} (fail closed)")
    if release_tags.count(tag) > 1:
        raise ValueError(f"GitHub lists more than one release for {tag}")
    if release or tag in release_tags or release_view is not False:
        if release_view is False:
            raise ValueError(f"GitHub release {tag} is listed but cannot be read")
        if notes is None:
            raise ValueError("the generated notes are required to check a visible release")
        validate_visible_release(release_view, tag=tag, notes=notes,
                                 pypi_hashes=pypi_files(pypi, version=version),
                                 record_sha256=record_sha256)
    return inventory


RECORD_ASSETS = ("release-provenance.json", "release-record.json")


def validate_visible_release(
    view: object, *, tag: str, notes: str, pypi_hashes: dict[str, str],
    record_sha256: str | None,
) -> None:
    """Refuse a visible release that is not this release's, before any channel moves.

    The body must be the generated notes, the release not a prerelease, every
    asset one of the four this run uploads (the PyPI wheel and sdist and the
    two records), and an asset digest, when GitHub reports one, equal to the
    PyPI hash or this run's release record. A published release must already
    hold all four: assets can never be added to an immutable release.
    """
    if (not isinstance(view, dict) or not isinstance(view.get("assets"), list)
            or view.get("tagName") != tag):
        raise ValueError(f"GitHub release {tag} is unreadable or names another tag")
    if view.get("isPrerelease") is not False:
        raise ValueError(f"GitHub release {tag} is a prerelease")
    if view.get("isDraft") not in (True, False):
        raise ValueError(f"GitHub release {tag} has no draft state")
    if _normalize_body(view.get("body")) != notes.rstrip("\n"):
        raise ValueError(f"GitHub release {tag} notes differ from the generated notes")
    expected = {**pypi_hashes, RECORD_ASSETS[0]: None, RECORD_ASSETS[1]: record_sha256}
    names = [asset.get("name") if isinstance(asset, dict) else None
             for asset in view["assets"]]
    if len(names) != len(set(names)):
        raise ValueError(f"GitHub release {tag} has duplicate asset names")
    foreign = sorted(str(name) for name in names if name not in expected)
    if foreign:
        raise ValueError(f"GitHub release {tag} has unexpected assets: " + ", ".join(foreign))
    for asset in view["assets"]:
        wanted = expected[asset["name"]]
        digest = asset.get("digest")
        if digest is not None and wanted is not None and digest != "sha256:" + wanted:
            raise ValueError(f"GitHub release {tag} asset {asset['name']} has a different hash")
    missing = sorted(set(expected) - set(names))
    if missing and view["isDraft"] is False:
        raise ValueError(BURNED + ": missing " + ", ".join(missing))


def _release_view(tag: str, repo: str) -> object:
    """`gh release view` JSON of a visible release, False if not found, None unreadable."""
    try:
        raw = _gh("release", "view", tag, "--repo", repo, "--json",
                  "tagName,body,isDraft,isPrerelease,assets")
    except subprocess.CalledProcessError as error:
        stderr = error.stderr if isinstance(error.stderr, bytes) else b""
        return False if NOT_FOUND in stderr else None
    except OSError:
        return None
    try:
        view = json.loads(raw)
    except ValueError:
        return None
    return view if isinstance(view, dict) else None


def _gh_tag_object(tag: str, repo: str) -> str | None:
    """The tag object the GitHub ref of an annotated tag names; "" if absent.

    A lightweight ref returns a value that never equals a tag object SHA. An
    unreadable or unexpected answer is None (fail closed).
    """
    try:
        raw = _gh("api", f"repos/{repo}/git/ref/tags/{tag}")
    except subprocess.CalledProcessError as error:
        stderr = error.stderr if isinstance(error.stderr, bytes) else b""
        return "" if b"HTTP 404" in stderr else None
    except OSError:
        return None
    try:
        ref = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(ref, dict) or ref.get("ref") != f"refs/tags/{tag}":
        return None
    target = ref.get("object")
    if not isinstance(target, dict):
        return None
    sha, kind = target.get("sha"), target.get("type")
    if not isinstance(sha, str) or SHA.fullmatch(sha) is None:
        return None
    if kind != "tag":
        return f"{kind}:{sha}"
    return sha


def _channel_digest(channel: str) -> str | None:
    """The digest behind ghcr.io/knaisoma/data-olympus:<channel>, "" if absent."""
    reference = f"{IMAGE}:{channel}"
    try:
        out = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", reference,
             "--format", "{{.Manifest.Digest}}"],
            capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode == 0:
        return out.stdout.strip()
    diagnostic = f"{out.stdout}\n{out.stderr}".lower()
    if ("manifest unknown" in diagnostic or "no such manifest" in diagnostic
            or f"{reference}: not found" in diagnostic):
        return ""
    return None


def _release_tags(repo: str) -> list[str] | None:
    """Tag names of every release the token lists, drafts included when visible."""
    try:
        raw = _gh("api", "--paginate", "--slurp", f"repos/{repo}/releases")
        pages = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        return None
    tags = [entry.get("tag_name") for page in pages for entry in page
            if isinstance(entry, dict)]
    return [tag for tag in tags if isinstance(tag, str)]


def gather_resume_state(*, version: str, image_digest: str, repo: str,
                        tag_object: str = "", notes: str | None = None,
                        record_sha256: str | None = None) -> dict[str, str]:
    tag = f"v{_stable_version(version)}"
    return validate_resume_state(
        version=version, image_digest=image_digest, pypi=fetch_pypi(version),
        ghcr_tag=_channel_digest(tag), channels={c: _channel_digest(c) for c in CHANNELS},
        release=_gh_release_present(tag, repo), release_tags=_release_tags(repo),
        git_tag=_gh_tag_object(tag, repo), tag_object=tag_object,
        release_view=_release_view(tag, repo) if tag_object else False,
        notes=notes, record_sha256=record_sha256,
    )


def _normalize_body(body: object) -> str:
    if not isinstance(body, str):
        raise ValueError("release body is unreadable")
    return body.replace("\r\n", "\n").rstrip("\n")


BURNED = ("published stable release is incomplete and immutable: this version is burned "
          "and needs a new stable version")


def validate_existing_release(
    release: dict, *, tag: str, notes: str, expected: dict[str, str],
    remote_hashes: dict[str, str], complete: bool = False, allow_draft: bool = False,
) -> list[str]:
    """Return missing asset names; refuse any release this workflow did not write."""
    if release.get("tagName") != tag:
        raise ValueError("release tag differs from the stable tag")
    drafts = (True, False) if allow_draft else (False,)
    if release.get("isDraft") not in drafts or release.get("isPrerelease") is not False:
        raise ValueError("existing stable release must be published and not a prerelease")
    if _normalize_body(release.get("body")) != notes.rstrip("\n"):
        raise ValueError("existing release notes differ from the generated notes")
    names = [asset.get("name") for asset in release.get("assets", [])]
    if len(names) != len(set(names)):
        raise ValueError("existing release has duplicate asset names")
    foreign = sorted(str(name) for name in names if name not in expected)
    if foreign:
        raise ValueError("existing release has unexpected assets: " + ", ".join(foreign))
    for asset in release.get("assets", []):
        name = asset["name"]
        digest = asset.get("digest")
        if digest is not None and digest != "sha256:" + expected[name]:
            raise ValueError(f"existing release asset {name} has a different hash")
        if remote_hashes.get(name) != expected[name]:
            raise ValueError(f"existing release asset {name} has a different hash")
    missing = sorted(set(expected) - set(names))
    if missing and release.get("isDraft") is False:
        # Immutable releases: assets can never be added after publication.
        raise ValueError(BURNED + ": missing " + ", ".join(missing))
    if complete and missing:
        raise ValueError("release is missing assets: " + ", ".join(missing))
    return missing


GH_TIMEOUT = 60.0


def _gh(*args: str) -> bytes:
    """Run gh; a call that exceeds GH_TIMEOUT is an OSError, so readers fail closed."""
    try:
        return subprocess.run(["gh", *args], check=True, capture_output=True,
                              timeout=GH_TIMEOUT).stdout
    except subprocess.TimeoutExpired as error:
        raise OSError(f"gh {args[0] if args else ''} timed out after {GH_TIMEOUT:g} s") \
            from error


# The release listing behind `gh release view` (a draft is not served by the
# tags endpoint) can lag a moment behind `gh release create` and `upload`.
# Reads are repeated after these delays (about 60 s in total), then the
# ordinary checks fail closed.
LISTING_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
NOT_FOUND = b"release not found"


def _poll[T](read: Callable[[], T], ready: Callable[[T], bool], *,
          sleep: Callable[[float], None]) -> T:
    """Return the first ready read, or the last read after the bounded delays."""
    value = read()
    for delay in LISTING_DELAYS:
        if ready(value):
            break
        sleep(delay)
        value = read()
    return value


def _inspect_release(tag: str, expected: dict[str, str]
                     ) -> tuple[dict, dict[str, str]] | None:
    """Read the release and hash its assets; None only when gh reports it absent."""
    try:
        raw = _gh("release", "view", tag, "--json", "tagName,body,isDraft,isPrerelease,assets")
    except subprocess.CalledProcessError as error:
        if isinstance(error.stderr, bytes) and NOT_FOUND in error.stderr:
            return None
        raise
    release = json.loads(raw)
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        raise ValueError("release view is unreadable")
    hashes = {}
    for asset in release["assets"]:
        name = asset.get("name") if isinstance(asset, dict) else None
        if name in expected:
            content = _gh("release", "download", tag, "--pattern", name, "--output", "-")
            hashes[name] = hashlib.sha256(content).hexdigest()
    return release, hashes


def _listed(found: tuple[dict, dict[str, str]] | None, names: set[str]) -> bool:
    if found is None:
        return False
    assets = found[0].get("assets", [])
    return names <= {asset.get("name") for asset in assets if isinstance(asset, dict)}


def _read_release(tag: str, expected: dict[str, str], names: set[str], *,
                  sleep: Callable[[float], None]) -> tuple[dict, dict[str, str]]:
    """Poll until the release is listed with every name in names, within the bound."""
    found = _poll(lambda: _inspect_release(tag, expected),
                  lambda value: _listed(value, names), sleep=sleep)
    if found is None:
        raise ValueError("draft release was not created")
    return found


def complete_release(
    *, tag: str, notes: str, record: dict, stable_provenance: dict, files: list[Path],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Complete a draft release, verify every byte, then publish it last.

    A published release is accepted only when it is already complete, since
    immutable releases cannot gain assets after publication. The first read
    after the workflow's `gh release create --draft` and the read after the
    upload tolerate listing lag within LISTING_DELAYS; every check still
    runs on the final read.
    """
    expected = expected_assets(record, stable_provenance, files, tag=tag, notes=notes)
    release, hashes = _read_release(tag, expected, set(), sleep=sleep)
    missing = validate_existing_release(release, tag=tag, notes=notes, expected=expected,
                                        remote_hashes=hashes, allow_draft=True)
    if missing:
        by_name = {path.name: path for path in files}
        # No --clobber: a concurrent upload of the same name fails instead.
        _gh("release", "upload", tag, *(str(by_name[name]) for name in missing))
        release, hashes = _read_release(tag, expected, set(expected), sleep=sleep)
    validate_existing_release(release, tag=tag, notes=notes, expected=expected,
                              remote_hashes=hashes, complete=True, allow_draft=True)
    if release.get("isDraft") is True:
        _gh("release", "edit", tag, "--draft=false")
        release, hashes = _read_release(tag, expected, set(), sleep=sleep)
    validate_existing_release(release, tag=tag, notes=notes, expected=expected,
                              remote_hashes=hashes, complete=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prove", "validate-inputs"):
        command = commands.add_parser(name)
        for arg in ("squash", "head", "candidate-tag"):
            command.add_argument(f"--{arg}", required=True)
        if name == "prove":
            command.add_argument("--main", default="origin/main")
            command.add_argument("--phase", choices=PHASES, default="initial")
            command.add_argument("--tagger")
            for arg in ("candidate-provenance", "output", "notes-output", "env-output"):
                command.add_argument(f"--{arg}", required=arg in ("candidate-provenance", "output"),
                                     type=Path)
    notes = commands.add_parser("notes")
    notes.add_argument("--head", required=True)
    notes.add_argument("--main", default="origin/main")
    notes.add_argument("--branch", choices=("release/new", "hotfix/new"), default="release/new")
    notes.add_argument("--output", type=Path)
    published = commands.add_parser("release")
    published.add_argument("--tag", required=True)
    for arg in ("record", "stable-provenance", "notes"):
        published.add_argument(f"--{arg}", required=True, type=Path)
    published.add_argument("assets", type=Path, nargs="+")
    checks = commands.add_parser("checks")
    checks.add_argument("--role", required=True, choices=CHECK_ROLES)
    checks.add_argument("--sha", required=True)
    checks.add_argument("--analyses", required=True)
    checks.add_argument("--check-runs", required=True, type=Path)
    verify = commands.add_parser("verify-pypi")
    verify.add_argument("--dist", required=True, type=Path)
    verify.add_argument("--stable-provenance", required=True, type=Path)
    verify.add_argument("--version", required=True)
    verify.add_argument("--attestations", required=True, choices=("required", "absent"))
    state = commands.add_parser("resume-state")
    state.add_argument("--version", required=True)
    state.add_argument("--image-digest", required=True)
    state.add_argument("--repo", required=True)
    # The local stable tag object the proof of this job verified; empty: none.
    state.add_argument("--tag-object", default="")
    # This run's generated notes and release record, to check a visible release.
    state.add_argument("--notes", type=Path)
    state.add_argument("--record", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify-pypi":
            verify_published(
                dist=args.dist, version=args.version,
                stable_provenance=json.loads(args.stable_provenance.read_text(encoding="utf-8")),
                attestations=args.attestations == "required",
            )
            print(f"PyPI holds exactly the stable wheel and sdist of {args.version}")
        elif args.command == "resume-state":
            if args.tag_object and (args.notes is None or args.record is None):
                raise ValueError("--notes and --record are required with --tag-object")
            inventory = gather_resume_state(
                version=args.version, repo=args.repo, image_digest=args.image_digest,
                tag_object=args.tag_object,
                notes=args.notes.read_text(encoding="utf-8") if args.notes else None,
                record_sha256=_sha256(args.record) if args.record else None)
            after = ("the verified stable tag exists and every later item is absent "
                     "or this release's" if args.tag_object
                     else "nothing after PyPI is published")
            print(f"PyPI holds {inventory['wheel']} and {inventory['sdist']}; "
                  f"{after} for v{args.version}")
        elif args.command == "checks":
            check_gate(json.loads(args.check_runs.read_text(encoding="utf-8")),
                       sha=args.sha, role=args.role, analyses=args.analyses)
        elif args.command == "validate-inputs":
            validate_inputs(squash=args.squash, head=args.head, candidate_tag=args.candidate_tag)
        elif args.command == "notes":
            version = _compute(Git(Path.cwd()), cwd=Path.cwd(), head=args.head,
                               main=args.main, branch=args.branch)
            rendered = generate_notes(cwd=Path.cwd(), version=version)
            if args.output:
                args.output.write_text(rendered, encoding="utf-8")
            print(rendered, end="")
        elif args.command == "release":
            complete_release(
                tag=args.tag, notes=args.notes.read_text(encoding="utf-8"),
                record=json.loads(args.record.read_text(encoding="utf-8")),
                stable_provenance=json.loads(args.stable_provenance.read_text(encoding="utf-8")),
                files=args.assets,
            )
        else:
            record = prove_release(
                cwd=Path.cwd(), squash=args.squash, head=args.head, main=args.main,
                candidate_tag=args.candidate_tag, phase=args.phase, tagger=args.tagger,
                provenance=json.loads(args.candidate_provenance.read_text(encoding="utf-8")),
            )
            outputs = env_lines(record)
            args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8")
            if args.notes_output:
                args.notes_output.write_text(record["notes"], encoding="utf-8")
            if args.env_output:
                args.env_output.write_text(outputs, encoding="utf-8")
    except (ValueError, VersionError, OSError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
