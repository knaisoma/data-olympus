#!/usr/bin/env python3
"""Prove a reviewed squash and emit a release record without repository writes.

The candidate's M is the pre-squash main. Notes use precisely the engine's
non-merge range and grammar. The adapter gathers Git facts for pure validators.

Phase "initial" runs once, right after the promotion lock is acquired: main
head must equal S and the stable tag must be absent. Phase "resume" is the
recheck in later jobs, which may be re-run after a partial publication: S must
still be reachable from main, and the stable tag may exist only as an
annotated tag on S whose message is the generated notes and whose tagger is the
release App. Every other proof is identical in both phases.

The "release" command completes or verifies the GitHub release: its body must
be the generated notes and every asset must be one of this run's verified files
with the same SHA-256. Missing assets are uploaded without replacement.

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
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.rc_decide import main_ratification_kwargs  # noqa: E402
from scripts.release_artifacts import CandidateVersion  # noqa: E402
from scripts.sdlc_version import Git, VersionError, _impact, compute_version  # noqa: E402

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


PHASES = ("initial", "resume")


def validate_tag_object(
    raw: str | None, *, squash: str, tag: str, notes: str, tagger: str | None,
) -> None:
    """Accept an existing stable tag only as this workflow's own annotated tag."""
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
    if not separator or message != notes:
        raise ValueError("existing stable tag message differs from generated notes")


def validate_proof(
    *, version: dict, provenance: dict, squash: str, head: str, candidate_tag: str,
    parents: list[str], head_tree: str, squash_tree: str, main_head: str,
    rc_head: str, message: str, notes: str, adoption_present: bool, tag_exists: bool,
    phase: str = "initial", main_contains_squash: bool = False,
    tag_target: str | None = None, tag_annotated: bool = False,
    tag_object: str | None = None, tagger: str | None = None,
) -> dict:
    """Pure validation of immutable Git facts and the recomputed engine result."""
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
    if phase == "resume" and main_head != squash and not main_contains_squash:
        raise ValueError("S is no longer reachable from main head")
    if rc_head != head:
        raise ValueError("RC tag does not resolve to recorded H")
    subject, separator, body = message.rstrip("\n").partition("\n\n")
    if subject != f"release: {identity.base}":
        raise ValueError("squash subject differs from engine release target")
    if not separator or body != notes.rstrip("\n"):
        raise ValueError("squash release notes differ from generated notes")
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


def _compute(git: Git, *, cwd: str | Path, head: str, main: str, branch: str) -> dict:
    """Recompute, taking adoption ratification only from main's blobs.

    This checkout is the squash S, whose tree equals the reviewed H, so its own
    scripts/adoption_ratification.py and vendored amendment are candidate data.
    For an adoption cut the pinned values are read from the RC's recorded main
    M (the main stage 2 ran from), never from S or H; the engine validates them.
    """
    with tempfile.TemporaryDirectory(prefix="adoption-ratification-") as trusted:
        return compute_version(cwd=cwd, head=head, main=main, branch=branch,
                               **main_ratification_kwargs(git, head=head, main=main,
                                                          trusted_dir=Path(trusted)))


def prove_release(
    *, cwd: str | Path, squash: str, head: str, candidate_tag: str,
    provenance: dict, main: str = "origin/main", phase: str = "initial",
    tagger: str | None = None,
) -> dict:
    """Read Git facts and recompute against the RC's frozen pre-squash main."""
    validate_inputs(squash=squash, head=head, candidate_tag=candidate_tag)
    identity = validate_candidate(provenance, head=head, candidate_tag=candidate_tag)
    git = Git(cwd)
    if git.file(head, "release/ADOPTION.json") is not None:
        raise ValueError("release/ADOPTION.json must be absent at H")
    branch = "hotfix/new" if "-hotfix.rc." in candidate_tag else "release/new"
    version = _compute(git, cwd=cwd, head=head, main=provenance["M"], branch=branch)
    notes = generate_notes(cwd=cwd, version=version)
    main_head = git.resolve(main)
    tag_ref = f"refs/tags/{identity.stable_tag}"
    tag_exists = identity.stable_tag in git.run("tag", "--list").splitlines()
    tag_target = git.resolve(tag_ref) if tag_exists else None
    tag_annotated = tag_exists and git.run("cat-file", "-t", tag_ref).strip() == "tag"
    tag_object = git.run("cat-file", "tag", tag_ref) if tag_annotated else None
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
        tag_object=tag_object, tagger=tagger,
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


def _gh(*args: str) -> bytes:
    return subprocess.run(["gh", *args], check=True, capture_output=True).stdout


def _inspect_release(tag: str, expected: dict[str, str]) -> tuple[dict, dict[str, str]]:
    release = json.loads(_gh("release", "view", tag, "--json",
                             "tagName,body,isDraft,isPrerelease,assets"))
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        raise ValueError("release view is unreadable")
    hashes = {}
    for asset in release["assets"]:
        name = asset.get("name") if isinstance(asset, dict) else None
        if name in expected:
            content = _gh("release", "download", tag, "--pattern", name, "--output", "-")
            hashes[name] = hashlib.sha256(content).hexdigest()
    return release, hashes


def complete_release(
    *, tag: str, notes: str, record: dict, stable_provenance: dict, files: list[Path],
) -> None:
    """Complete a draft release, verify every byte, then publish it last.

    A published release is accepted only when it is already complete, since
    immutable releases cannot gain assets after publication.
    """
    expected = expected_assets(record, stable_provenance, files, tag=tag, notes=notes)
    release, hashes = _inspect_release(tag, expected)
    missing = validate_existing_release(release, tag=tag, notes=notes, expected=expected,
                                        remote_hashes=hashes, allow_draft=True)
    if missing:
        by_name = {path.name: path for path in files}
        # No --clobber: a concurrent upload of the same name fails instead.
        _gh("release", "upload", tag, *(str(by_name[name]) for name in missing))
        release, hashes = _inspect_release(tag, expected)
    validate_existing_release(release, tag=tag, notes=notes, expected=expected,
                              remote_hashes=hashes, complete=True, allow_draft=True)
    if release.get("isDraft") is True:
        _gh("release", "edit", tag, "--draft=false")
        release, hashes = _inspect_release(tag, expected)
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
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-inputs":
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
