"""Simulate the rest of the 0.11.1 promotion once its stable tag exists.

Run 37958694040 (resume_pypi) published PyPI, pushed the App's annotated
v0.11.1 on S, and then failed its own post-push recheck: the engine counted
v0.11.1 as the highest stable tag, so the adoption record (base v0.11.0)
looked invalid. Any promotion of an adoption cut, a hotfix or a first release
failed that way after tagging, because each recomputes a version whose base
is no longer the highest stable tag.

The fixture mirrors the live shape: v0.11.0, the anchor, the cut B adding
only release/ADOPTION.json (main is B, which is also the RC's recorded M), H
on release/new five commits later with the record retired (0.11.1-rc.5), S
the squash of H on B, the App's annotated v0.11.1 on S whose message is the
generated notes, and main one commit past S. A runner clone fetches it as the
workflow does and runs the real step scripts of promote-release.yml, with
fake docker, gh, curl and mcp-publisher executables standing in for GHCR,
GitHub releases, PyPI and the MCP registry. Steps that only talk to those
services in-process run the real Python with mocked clients.
Set SIMULATION_TRANSCRIPT to a path to write the transcript of the main chain.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import adoption_ratification as ratification
from scripts import release_record as release
from scripts.sdlc_version import VersionError, compute_version
from tests.test_adoption_cut import Cut
from tests.test_release_record import ANALYSES, head_runs, squash_runs

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/promote-release.yml").read_text())
REPO = "knaisoma/data-olympus"
IMAGE = "ghcr.io/knaisoma/data-olympus"
BOT_NAME = "knaisoma-data-olympus[bot]"
BOT_EMAIL = "339704040+knaisoma-data-olympus[bot]@users.noreply.github.com"
BOT = f"{BOT_NAME} <{BOT_EMAIL}>"
CANDIDATE = "sha256:628d3a861edd737e3129e908e995d260bb602aa387be22ac7ed19d4bfbd35d30"
PREVIOUS = "sha256:95beaca8782310958084140f0e6165de638430ea8ccb3486c97a68240b8ad06e"
THIRD = "sha256:" + "3" * 64
VERSION, TAG, RC = "0.11.1", "v0.11.1", "0.11.1-rc.5"
WHEEL, SDIST = "data_olympus-0.11.1-py3-none-any.whl", "data_olympus-0.11.1.tar.gz"
FILES = {WHEEL: b"stable wheel of S", SDIST: b"stable sdist of S"}
MARKER = "<!-- mcp-name: io.github.knaisoma/data-olympus -->"

FAKE_DOCKER = r'''
import json, os, sys
path = os.environ["FAKE_GHCR"]
state = json.load(open(path))
args = sys.argv[1:]
assert args[:2] == ["buildx", "imagetools"], args
def resolve(ref):
    name, at, digest = ref.partition("@")
    if at:
        return digest if digest in state["digests"] else None
    return state["tags"].get(ref.rpartition(":")[2])
if args[2] == "inspect":
    digest = resolve(args[3])
    if digest is None:
        print(f"ERROR: {args[3]}: not found", file=sys.stderr)
        sys.exit(1)
    print(digest)
    sys.exit(0)
tags, source, i = [], None, 3
while i < len(args):
    if args[i] == "--tag":
        tags.append(args[i + 1]); i += 2
    else:
        source = args[i]; i += 1
digest = resolve(source)
if digest is None:
    sys.exit(1)
for tag in tags:
    state["tags"][tag.rpartition(":")[2]] = digest
state["log"].append(["create", tags, source])
json.dump(state, open(path, "w"))
'''

FAKE_GH = r'''
import hashlib, json, os, pathlib, shutil, subprocess, sys
path = pathlib.Path(os.environ["FAKE_GH"])
state = json.loads(path.read_text())
store = path.parent / "assets"
args = sys.argv[1:]
state["log"].append(args)
def save():
    path.write_text(json.dumps(state))
if args[0] != "release":
    sys.exit(2)
sub, tag = args[1], args[2]
release = state["releases"].get(tag)
if sub == "view":
    save()
    if release is None:
        sys.stderr.write("release not found\n"); sys.exit(1)
    if "--json" in args:
        fields = args[args.index("--json") + 1].split(",")
        print(json.dumps({key: release[key] for key in fields}))
    sys.exit(0)
if sub == "create":
    if release is not None:
        sys.exit(1)
    if "--verify-tag" in args and subprocess.run(
            ["git", "-C", os.environ["FAKE_ORIGIN"], "rev-parse", "-q", "--verify",
             f"refs/tags/{tag}"], capture_output=True).returncode:
        sys.exit(1)
    notes = pathlib.Path(args[args.index("--notes-file") + 1]).read_text()
    state["releases"][tag] = {"tagName": tag, "body": notes, "isDraft": "--draft" in args,
                              "isPrerelease": False, "assets": []}
elif sub == "upload":
    if not release["isDraft"]:
        save(); sys.stderr.write("immutable release\n"); sys.exit(1)
    for name in args[3:]:
        source = pathlib.Path(name)
        if any(a["name"] == source.name for a in release["assets"]):
            save(); sys.exit(1)
        (store / tag).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, store / tag / source.name)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        release["assets"].append({"name": source.name, "digest": "sha256:" + digest})
elif sub == "download":
    name = args[args.index("--pattern") + 1]
    save()
    sys.stdout.buffer.write((store / tag / name).read_bytes())
    sys.exit(0)
elif sub == "edit":
    if "--draft=false" in args:
        release["isDraft"] = False
save()
'''

FAKE_CURL = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
url = next(a for a in args if a.startswith("https://"))
out = pathlib.Path(args[args.index("-o") + 1])
state = pathlib.Path(os.environ["FAKE_REGISTRY_DIR"])
if url.startswith("https://pypi.org/"):
    out.write_text(json.dumps({"info": {"version": "0.11.1",
                                        "description": os.environ["FAKE_PYPI_DESCRIPTION"]}}))
else:
    versions = ["0.11.0"] + (["0.11.1"] if (state / "published").exists() else [])
    out.write_text(json.dumps({"servers": [
        {"server": {"name": "io.github.knaisoma/data-olympus", "version": v}}
        for v in versions]}))
'''

FAKE_PUBLISHER = r'''
import os, pathlib, sys
state = pathlib.Path(os.environ["FAKE_REGISTRY_DIR"])
with open(state / "publisher.log", "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1:] == ["publish"]:
    (state / "published").write_text("yes")
'''


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pypi_json() -> dict:
    """The live listing of data-olympus 0.11.1: two files, no attestation files."""
    return {"info": {"version": VERSION, "description": MARKER}, "urls": [
        {"filename": name, "digests": {"sha256": sha(data)}, "yanked": False}
        for name, data in FILES.items()]}


def step(job: str, prefix: str) -> dict:
    return next(s for s in WORKFLOW["jobs"][job]["steps"]
                if s.get("name", "").startswith(prefix))


def executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)


class Live:
    """The live promotion shape after run 37958694040 pushed v0.11.1."""

    def __init__(self, tmp: Path, *, tagged: bool = True, advanced: bool = True):
        self.tmp = tmp
        self.cut = cut = Cut(tmp / "origin")
        self.origin = cut.path
        cut.placeholder()
        cut.retire()
        for name, message in (("a", "fix(release): verify the stable record"),
                              ("b", "feat(cli): report promotion state"),
                              ("c", "fix(server): keep the audit trail")):
            cut.write(f"src/{name}.py", f"{name} = 1\n")
            cut.commit(message)
        version = cut.engine(cut.preflight())
        assert (version["candidate"], version["N"]) == (RC, 5)
        self.b, self.h = cut.c, version["H"]
        cut.git("tag", "-a", RC, self.h, "-m", "candidate")
        self.notes = release.generate_notes(cwd=self.origin, version=version)
        self.s = cut.git("commit-tree", f"{self.h}^{{tree}}", "-p", self.b, "-m",
                         f"release: {VERSION}\n\n{self.notes}")
        cut.git("update-ref", "refs/heads/main", self.s)
        self.provenance = {
            "source_sha": self.h, "H": self.h, "B": self.b, "M": self.b, "N": 5,
            "candidate_tag": RC, "python_version": "0.11.1rc5", "image_digest": CANDIDATE,
            "oci_archive_sha256": "b" * 64, "promotable": True, "dry_run": False,
            "candidate": {
                "version": "0.11.1rc5", "source_sha": self.h, "source_tree_sha256": "c" * 64,
                "lock_sha256": "d" * 64, "wheel_sha256": "e" * 64, "sdist_sha256": "f" * 64,
                "wheel": "data_olympus-0.11.1rc5-py3-none-any.whl",
                "sdist": "data_olympus-0.11.1rc5.tar.gz",
            },
        }
        if tagged:
            self.app_tag()
        if advanced:
            self.advance_main()
        self.runner = tmp / "runner"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.runner)], check=True)
        self.run_git("checkout", "-q", "--detach", "origin/main")
        shutil.copytree(ROOT / "scripts", self.runner / "scripts", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
        assert self.run_git("status", "--porcelain", "--untracked-files=no") == ""
        promotion = self.runner / "to-delete/promotion/candidate"
        promotion.mkdir(parents=True)
        (promotion / "release-provenance.json").write_text(json.dumps(self.provenance))
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "python").symlink_to(sys.executable)
        executable(self.bin / "docker", FAKE_DOCKER)
        executable(self.bin / "gh", FAKE_GH)
        executable(self.bin / "curl", FAKE_CURL)
        self.ghcr = tmp / "ghcr.json"
        self.ghcr.write_text(json.dumps({
            "tags": {RC: CANDIDATE, "v0.11.0": PREVIOUS, "stable": PREVIOUS,
                     "latest": PREVIOUS},
            "digests": [CANDIDATE, PREVIOUS, THIRD], "log": []}))
        self.gh = tmp / "gh" / "state.json"
        self.gh.parent.mkdir()
        self.gh.write_text(json.dumps({"releases": {
            r: {"tagName": r, "body": "candidate", "isDraft": False, "isPrerelease": True,
                "assets": []} for r in ("0.11.1-rc.4", RC)}, "log": []}))
        self.transcript: list[str] = []
        # prove's GITHUB_TOKEN (contents: read) probably cannot see drafts.
        self.drafts_visible = False

    # Git and process helpers.
    def git(self, *args: str) -> str:
        return self.cut.git(*args)

    def tag_raw(self) -> str:
        """The tag object byte for byte (Cut.git strips the trailing newline)."""
        return subprocess.check_output(["git", "cat-file", "tag", TAG], cwd=self.origin,
                                       text=True)

    def run_git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.runner, text=True).strip()

    def app_tag(self, target: str | None = None, *, notes: str | None = None,
                who: tuple[str, str] = (BOT_NAME, BOT_EMAIL), name: str = TAG,
                lightweight: bool = False) -> None:
        """The tag create-tag pushes: annotated, verbatim notes, App identity."""
        if lightweight:
            self.git("tag", name, target or self.s)
            return
        notes_file = self.tmp / "tag-notes.md"
        notes_file.write_text(self.notes if notes is None else notes)
        self.git("-c", f"user.name={who[0]}", "-c", f"user.email={who[1]}", "tag", "-a",
                 "--cleanup=verbatim", name, target or self.s, "-F", str(notes_file))

    def advance_main(self) -> None:
        later = self.git("commit-tree", f"{self.s}^{{tree}}", "-p", self.s, "-m",
                         "fix(release): verify published stable files (#358)")
        self.git("update-ref", "refs/heads/main", later)

    def env(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update(PATH=f"{self.bin}:{env['PATH']}", FAKE_GHCR=str(self.ghcr),
                   FAKE_GH=str(self.gh), FAKE_ORIGIN=str(self.origin),
                   GITHUB_OUTPUT=str(self.tmp / "github_output"),
                   GITHUB_ENV=str(self.tmp / "github_env"), GITHUB_REPOSITORY=REPO,
                   BOT_NAME=BOT_NAME, BOT_EMAIL=BOT_EMAIL, SQUASH=self.s,
                   REVIEWED_HEAD=self.h, RC_TAG=RC, TAG=TAG, VERSION=VERSION,
                   IMAGE_DIGEST=CANDIDATE, GH_TOKEN="fake", GH_REPO=REPO)
        env.update(extra)
        return env

    def run_step(self, job: str, prefix: str, *, cwd: Path | None = None,
                 **extra: str) -> subprocess.CompletedProcess:
        """Run a step's script exactly as the runner's bash shell does."""
        script = step(job, prefix)["run"]
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                              cwd=cwd or self.runner, env=self.env(**extra),
                              capture_output=True, text=True)

    def passed(self, label: str, result: subprocess.CompletedProcess) -> None:
        assert result.returncode == 0, f"{label}\n{result.stdout}\n{result.stderr}"
        self.transcript.append(f"PASS {label}")

    # The jobs, in the order of the workflow.
    def fetch(self) -> None:
        self.run_git("fetch", "--tags", "origin", "+refs/heads/main:refs/remotes/origin/main",
                     f"refs/tags/{RC}:refs/tags/{RC}")

    def prove(self, resume_pypi: str = "true") -> subprocess.CompletedProcess:
        self.fetch()
        return self.run_step("prove", "Prove squash", RESUME_PYPI=resume_pypi)

    def check_gate(self) -> None:
        for role, sha_, pages in (("head", self.h, head_runs(self.h)),
                                  ("squash", self.s, squash_runs(self.s))):
            release.check_gate(pages, sha=sha_, role=role, analyses=ANALYSES)

    def gh_api(self, *args: str) -> bytes:
        """GitHub's git ref and release listing endpoints, read from the fakes."""
        if args[:2] == ("api", f"repos/{REPO}/git/ref/tags/{TAG}"):
            found = subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{TAG}"],
                                   cwd=self.origin, capture_output=True, text=True)
            if found.returncode:
                raise subprocess.CalledProcessError(1, "gh", stderr=b"gh: Not Found (HTTP 404)")
            kind = self.git("cat-file", "-t", f"refs/tags/{TAG}")
            return json.dumps({"ref": f"refs/tags/{TAG}", "object": {
                "sha": found.stdout.strip(), "type": kind}}).encode()
        if args[:6] == ("release", "view", TAG, "--repo", REPO, "--json"):
            found = json.loads(self.gh.read_text())["releases"].get(TAG)
            if found is None or (found["isDraft"] and not self.drafts_visible):
                raise subprocess.CalledProcessError(1, "gh", stderr=b"release not found")
            return json.dumps({key: found[key] for key in args[6].split(",")}).encode()
        if args[:4] == ("api", "--paginate", "--slurp", f"repos/{REPO}/releases"):
            releases = json.loads(self.gh.read_text())["releases"].values()
            # A read-only token does not list drafts.
            return json.dumps([[{"tag_name": r["tagName"]} for r in releases
                                if not r["isDraft"]]]).encode()
        raise AssertionError(args)

    def resume_state(self, monkeypatch, capsys) -> tuple[int, str]:
        tag_object = subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{TAG}"],
                                    cwd=self.runner, capture_output=True, text=True).stdout.strip()
        tags = json.loads(self.ghcr.read_text())["tags"]
        releases = json.loads(self.gh.read_text())["releases"]
        monkeypatch.setattr(release, "fetch_pypi", lambda _version: pypi_json())
        monkeypatch.setattr(release, "_channel_digest", lambda name: tags.get(name, ""))
        monkeypatch.setattr(release, "_gh_release_present", lambda tag, _repo: (
            tag in releases and not releases[tag]["isDraft"]))
        monkeypatch.setattr(release, "_gh", self.gh_api)
        promotion = self.runner / "to-delete/promotion"
        code = release.main(["resume-state", "--version", VERSION, "--image-digest", CANDIDATE,
                             "--repo", REPO, "--tag-object", tag_object,
                             "--notes", str(promotion / "release-notes.md"),
                             "--record", str(promotion / "release-record.json")])
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    def recheck_pypi(self, resume_pypi: str = "true") -> subprocess.CompletedProcess:
        return self.run_step("publish-pypi", "Recheck the proof", RESUME_PYPI=resume_pypi)

    def build_stable(self) -> None:
        """The stable-artifacts artifact: two files and the stable provenance."""
        dist = self.runner / "to-delete/stable/dist"
        dist.mkdir(parents=True, exist_ok=True)
        for name, data in FILES.items():
            (dist / name).write_bytes(data)
        record = json.loads((self.runner / "to-delete/promotion/release-record.json").read_text())
        stable = {"version": VERSION, "source_sha": self.s, "wheel": WHEEL, "sdist": SDIST,
                  "wheel_sha256": sha(FILES[WHEEL]), "sdist_sha256": sha(FILES[SDIST])}
        (self.runner / "to-delete/stable/release-provenance.json").write_text(
            json.dumps(record | {"stable": stable}))

    def verify_pypi(self, attestations: bool = False) -> None:
        release.verify_published(
            dist=self.runner / "to-delete/stable/dist", version=VERSION,
            stable_provenance=json.loads(
                (self.runner / "to-delete/stable/release-provenance.json").read_text()),
            attestations=attestations, fetch=lambda _version: pypi_json(),
            sleep=lambda _delay: None)

    def create_tag(self, *, allow_push: bool = False) -> subprocess.CompletedProcess:
        if not allow_push:
            # Any push fails: the existing-tag path must never push.
            self.run_git("remote", "set-url", "--push", "origin", str(self.tmp / "no-push"))
        return self.run_step("create-tag", "Recheck locked state")

    def promote_image(self) -> subprocess.CompletedProcess:
        return self.run_step("promote-image", "Move exactly the stable channels", REPO=IMAGE)

    def release_job(self) -> subprocess.CompletedProcess:
        return self.run_step("release", "Publish generated notes")

    def registry(self) -> list[subprocess.CompletedProcess]:
        work = self.tmp / "registry"
        (work / "to-delete").mkdir(parents=True)
        shutil.copy(ROOT / "server.json", work / "server.json")
        executable(work / "to-delete/mcp-publisher", FAKE_PUBLISHER)
        extra = {"FAKE_REGISTRY_DIR": str(work), "FAKE_PYPI_DESCRIPTION": f"text\n{MARKER}\n",
                 "REGISTRY": "https://registry.modelcontextprotocol.io"}
        results = [self.run_step("publish-mcp-registry", "Inject", cwd=work, **extra),
                   self.run_step("publish-mcp-registry", "Check server.json", cwd=work, **extra)]
        env_file = Path(self.env()["GITHUB_ENV"])
        server = dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                      if line.startswith("SERVER_NAME="))
        results.append(self.run_step("publish-mcp-registry", "Publish the registry entry",
                                     cwd=work, **extra, **server))
        return results


@pytest.fixture
def live(tmp_path):
    return Live(tmp_path)


def engine(live: Live, **kwargs) -> dict:
    """The promotion computation of the live shape, with the trusted ratification."""
    return compute_version(cwd=live.origin, head=live.h, main=live.b, branch="release/new",
                           **ratification.engine_kwargs(ROOT), **kwargs)


def test_live_shape_reproduces_the_failed_recheck_without_ignore_tags(live):
    """The engine alone, as the old proof called it, refuses the tagged release."""
    with pytest.raises(VersionError, match="record must name untagged cut and highest stable"):
        engine(live)
    version = engine(live, ignore_tags=frozenset({TAG}))
    assert (version["candidate"], version["base"], version["B"]) == (RC, "v0.11.0", live.b)
    assert live.git("cat-file", "-t", TAG) == "tag"
    assert live.tag_raw().endswith("\n\n" + live.notes)


def test_the_remaining_chain_passes_after_the_tag(live, monkeypatch, capsys):
    """resume_pypi dispatch after run 37958694040: every remaining step in order."""
    t = live.transcript
    tag_before = live.git("rev-parse", f"refs/tags/{TAG}")
    live.passed("prove: proof in phase resume-pypi accepts the verified App tag on S",
                live.prove())
    outputs = Path(live.env()["GITHUB_OUTPUT"]).read_text()
    assert f"tag={TAG}\nversion={VERSION}\nsource_sha={live.s}\n" in outputs
    live.check_gate()
    t.append("PASS prove: exact-source check gate (test and CodeQL on H, analyses on S)")
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 0, out
    assert "the verified stable tag exists" in out
    t.append("PASS prove: resume-state (PyPI holds both files; GitHub ref is the verified tag "
             "object; GHCR v0.11.1 absent; stable/latest on the previous digest)")
    live.passed("publish-pypi: recheck after approval, record byte equal", live.recheck_pypi())
    live.build_stable()
    live.verify_pypi(attestations=False)
    t.append("PASS publish-pypi: verify-pypi without attestations (upload skipped)")
    result = live.create_tag()
    live.passed("create-tag: recheck accepts the tag, no push, idempotent", result)
    assert f"{TAG} already exists as this workflow's annotated tag on S" in result.stdout
    assert live.git("rev-parse", f"refs/tags/{TAG}") == tag_before
    live.passed("promote-image: v0.11.1, stable and latest move to the candidate digest",
                live.promote_image())
    tags = json.loads(live.ghcr.read_text())["tags"]
    assert (tags[TAG], tags["stable"], tags["latest"]) == (CANDIDATE,) * 3
    live.passed("release: draft created, four assets uploaded, verified, published last",
                live.release_job())
    published = json.loads(live.gh.read_text())["releases"][TAG]
    assert published["isDraft"] is False
    assert published["body"] == live.notes
    assert sorted(a["name"] for a in published["assets"]) == sorted(
        [WHEEL, SDIST, "release-provenance.json", "release-record.json"])
    log = json.loads(live.gh.read_text())["log"]
    assert [entry[:2] for entry in log if entry[1] in ("create", "edit")] == [
        ["release", "create"], ["release", "edit"]]
    for index, result in enumerate(live.registry()):
        assert result.returncode == 0, (index, result.stdout, result.stderr)
    publisher = (live.tmp / "registry/publisher.log").read_text().splitlines()
    assert publisher == ["login github-oidc", "publish"]
    t.append("PASS publish-mcp-registry: server.json 0.11.1, PyPI marker, publish, read-back")
    if os.environ.get("SIMULATION_TRANSCRIPT"):
        Path(os.environ["SIMULATION_TRANSCRIPT"]).write_text("\n".join(t) + "\n")
    assert len(t) == 9


def test_resume_accepts_the_partial_states_after_the_tag(live, monkeypatch, capsys):
    """promote-image already ran, then the release is a draft with two assets."""
    assert live.prove().returncode == 0
    assert live.promote_image().returncode == 0
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 0, out
    assert live.promote_image().returncode == 0
    live.build_stable()
    state = json.loads(live.gh.read_text())
    state["releases"][TAG] = {"tagName": TAG, "body": live.notes, "isDraft": True,
                              "isPrerelease": False, "assets": []}
    live.gh.write_text(json.dumps(state))
    upload = subprocess.run(
        ["gh", "release", "upload", TAG, f"to-delete/stable/dist/{WHEEL}",
         "to-delete/promotion/release-record.json"],
        cwd=live.runner, env=live.env(), capture_output=True, text=True)
    assert upload.returncode == 0, upload.stderr
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 0, out
    assert live.release_job().returncode == 0
    final = json.loads(live.gh.read_text())["releases"][TAG]
    assert final["isDraft"] is False
    assert len(final["assets"]) == 4
    # A complete published release is verified again without any write.
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 0, out
    writes = len([e for e in json.loads(live.gh.read_text())["log"] if e[1] != "view"
                  and e[1] != "download"])
    assert live.release_job().returncode == 0
    assert writes == len([e for e in json.loads(live.gh.read_text())["log"]
                          if e[1] not in ("view", "download")])


def test_a_normal_run_tags_and_its_post_push_recheck_passes(tmp_path):
    """The exact live failure, on the normal path, and the re-run of failed jobs."""
    live = Live(tmp_path, tagged=False, advanced=False)
    assert live.prove(resume_pypi="false").returncode == 0
    assert live.recheck_pypi(resume_pypi="false").returncode == 0
    pushed = live.create_tag(allow_push=True)
    assert pushed.returncode == 0, pushed.stderr
    assert live.git("cat-file", "-t", TAG) == "tag"
    assert live.tag_raw().endswith("\n\n" + live.notes)
    # Re-run failed jobs: create-tag and publish-pypi see the tag they made.
    rerun = live.create_tag()
    assert rerun.returncode == 0, rerun.stderr
    assert "already exists" in rerun.stdout
    assert live.recheck_pypi(resume_pypi="false").returncode == 0
    # Re-running all jobs (the initial proof) is still refused.
    initial = live.prove(resume_pypi="false")
    assert initial.returncode == 1
    assert "stable tag already exists" in initial.stderr


@pytest.mark.parametrize(("change", "error"), [
    ("tag_on_h", "stable tag already exists"),
    ("tag_on_b", "stable tag already exists"),
    ("lightweight", "stable tag already exists"),
    ("wrong_tagger", "tagger differs from the release App"),
    ("wrong_message", "message differs from generated notes"),
    ("foreign_higher_tag", "record must name untagged cut and highest stable base"),
    ("nested_tag", "does not name S"),
])
def test_the_proof_fails_closed_on_any_other_tag(tmp_path, change, error):
    live = Live(tmp_path, tagged=False)
    if change == "tag_on_h":
        live.app_tag(live.h)
    elif change == "tag_on_b":
        live.app_tag(live.b)
    elif change == "lightweight":
        live.app_tag(lightweight=True)
    elif change == "wrong_tagger":
        live.app_tag(who=("github-actions[bot]",
                          "41898282+github-actions[bot]@users.noreply.github.com"))
    elif change == "wrong_message":
        live.app_tag(notes=live.notes.replace("Fixes", "Fixed"))
    elif change == "foreign_higher_tag":
        # Our tag is valid, but a second stable tag must never be ignored.
        live.app_tag()
        live.git("tag", "v0.11.2", live.s)
    elif change == "nested_tag":
        live.app_tag(name="inner")
        live.app_tag(live.git("rev-parse", "refs/tags/inner"))
    result = live.prove()
    assert result.returncode == 1
    assert error in result.stderr, result.stderr
    assert not (live.runner / "to-delete/promotion/release-record.json").exists()
    assert live.recheck_pypi().returncode == 1
    assert live.create_tag().returncode == 1


def test_only_the_releases_own_tag_may_be_ignored(live):
    facts = dict(
        version=engine(live, ignore_tags=frozenset({TAG})),
        provenance=live.provenance, squash=live.s, head=live.h, candidate_tag=RC,
        parents=[live.b], head_tree="t", squash_tree="t", main_head=live.s, rc_head=live.h,
        message=f"release: {VERSION}\n\n{live.notes}", notes=live.notes,
        adoption_present=False, tag_exists=True, phase="resume", tag_target=live.s,
        tag_annotated=True, tag_object=live.tag_raw(), tagger=BOT,
    )
    assert release.validate_proof(**facts, ignored_tags=frozenset({TAG}))["tag"] == TAG
    for ignored, overrides in (
            (frozenset({TAG, "v0.11.2"}), {}), (frozenset({"v0.11.2"}), {}),
            (frozenset({TAG}), {"tag_exists": False}),
            (frozenset({TAG}), {"phase": "initial"})):
        with pytest.raises(ValueError, match="only this release's own existing stable tag"):
            release.validate_proof(**(facts | overrides), ignored_tags=ignored)


@pytest.mark.parametrize("ignore", [
    {TAG}, [TAG], frozenset({"v0.11.*"}), frozenset({"0.11.1"}), frozenset({"v0.11.1-rc.5"}),
    frozenset({"refs/tags/v0.11.1"}), frozenset({1}),
])
def test_engine_ignore_tags_takes_exact_stable_names_only(live, ignore):
    with pytest.raises(VersionError, match="bad_ignore_tags"):
        engine(live, ignore_tags=ignore)


def test_engine_refuses_to_ignore_the_cut_tag(tmp_path):
    cut = Cut(tmp_path / "repo")
    cut.write("x.py", "x = 1\n")
    cut.commit("fix: change")
    cut.git("tag", "v0.11.5", cut.c)
    with pytest.raises(VersionError, match="an ignored tag cannot tag the cut"):
        cut.trusted(adoption_dry_run=True, ignore_tags=frozenset({"v0.11.5"}))


def test_engine_hotfix_scope_needs_the_same_ignore(tmp_path):
    """A hotfix promotion has the same post-tag failure and the same fix."""
    cut = Cut(tmp_path / "repo")
    cut.git("checkout", "-q", "-b", "hotfix/new", "v0.11.0")
    cut.git("update-ref", "refs/heads/main", cut.git("rev-parse", "v0.11.0^{commit}"))
    cut.write("x.py", "x = 1\n")
    head = cut.commit("fix: hot")
    cut.git("tag", "v0.11.1", head)
    with pytest.raises(VersionError, match="hotfix_scope"):
        compute_version(cwd=cut.path, head=head, main="refs/heads/main", branch="hotfix/new")
    version = compute_version(cwd=cut.path, head=head, main="refs/heads/main",
                              branch="hotfix/new", ignore_tags=frozenset({"v0.11.1"}))
    assert version["candidate"] == "0.11.1-hotfix.rc.1"


@pytest.mark.parametrize(("ghcr", "github_tag", "error"), [
    ({TAG: THIRD}, None, "points at another digest than the candidate"),
    ({TAG: PREVIOUS}, None, "points at another digest than the candidate"),
    ({"stable": CANDIDATE}, None, "stable points at the candidate digest but"),
    ({"latest": CANDIDATE}, None, "latest points at the candidate digest but"),
    ({}, "moved", "is not the tag the proof verified"),
    ({}, "deleted", "is not the tag the proof verified"),
])
def test_resume_state_after_the_tag_fails_closed(live, monkeypatch, capsys, ghcr,
                                                 github_tag, error):
    assert live.prove().returncode == 0
    state = json.loads(live.ghcr.read_text())
    state["tags"].update(ghcr)
    live.ghcr.write_text(json.dumps(state))
    if github_tag == "moved":
        # GitHub's ref now names another tag object than the one the proof read.
        live.git("tag", "-d", TAG)
        live.app_tag(notes=live.notes + "\n")
    elif github_tag == "deleted":
        live.git("tag", "-d", TAG)
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 1
    assert error in out


def test_resume_state_without_a_tag_is_unchanged(tmp_path, monkeypatch, capsys):
    live = Live(tmp_path, tagged=False)
    assert live.prove().returncode == 0
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 0, out
    assert "nothing after PyPI is published" in out
    state = json.loads(live.ghcr.read_text())
    state["tags"][TAG] = CANDIDATE
    live.ghcr.write_text(json.dumps(state))
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 1
    assert "GHCR tag v0.11.1 already exists" in out
    # A tag pushed on GitHub after this job's fetch is not the verified one.
    state["tags"].pop(TAG)
    live.ghcr.write_text(json.dumps(state))
    live.app_tag()
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 1
    assert "is not the tag the proof verified" in out


def test_promote_image_refuses_a_version_tag_on_a_third_digest(live):
    state = json.loads(live.ghcr.read_text())
    state["tags"][TAG] = THIRD
    live.ghcr.write_text(json.dumps(state))
    assert live.promote_image().returncode == 1
    assert json.loads(live.ghcr.read_text())["tags"]["stable"] == PREVIOUS


@pytest.mark.parametrize("foreign", ["body", "asset", "prerelease"])
def test_the_release_job_refuses_a_foreign_draft_and_never_publishes_it(live, foreign):
    assert live.prove().returncode == 0
    live.build_stable()
    draft = {"tagName": TAG, "body": live.notes, "isDraft": True, "isPrerelease": False,
             "assets": []}
    if foreign == "body":
        draft["body"] = "# Release 0.11.1\n\nForged\n"
    elif foreign == "prerelease":
        draft["isPrerelease"] = True
    state = json.loads(live.gh.read_text())
    state["releases"][TAG] = draft
    live.gh.write_text(json.dumps(state))
    if foreign == "asset":
        (live.tmp / "evil.bin").write_bytes(b"evil")
        subprocess.run(["gh", "release", "upload", TAG, str(live.tmp / "evil.bin")],
                       env=live.env(), check=True)
    result = live.release_job()
    assert result.returncode == 1
    final = json.loads(live.gh.read_text())
    assert final["releases"][TAG]["isDraft"] is True
    assert not [entry for entry in final["log"] if entry[1] == "edit"]


def put_release(live: Live, **fields) -> dict:
    """Store a v0.11.1 release in the fake GitHub, assets as {name: bytes}."""
    assets = fields.pop("assets", {})
    state = json.loads(live.gh.read_text())
    entry = {"tagName": TAG, "body": live.notes, "isDraft": False, "isPrerelease": False,
             "assets": []} | fields
    store = live.gh.parent / "assets" / TAG
    store.mkdir(parents=True, exist_ok=True)
    for name, data in assets.items():
        (store / name).write_bytes(data)
        entry["assets"].append({"name": name, "digest": "sha256:" + sha(data)})
    state["releases"][TAG] = entry
    live.gh.write_text(json.dumps(state))
    return entry


def own_assets(live: Live) -> dict[str, bytes]:
    record = (live.runner / "to-delete/promotion/release-record.json").read_bytes()
    return {**FILES, "release-provenance.json": b"{}", "release-record.json": record}


@pytest.mark.parametrize(("change", "error"), [
    ("foreign_body", "notes differ from the generated notes"),
    ("prerelease", "is a prerelease"),
    ("foreign_asset", "unexpected assets: evil.bin"),
    ("wheel_bytes", "asset data_olympus-0.11.1-py3-none-any.whl has a different hash"),
    ("record_bytes", "asset release-record.json has a different hash"),
    ("published_incomplete", "burned"),
    ("foreign_visible_draft", "notes differ from the generated notes"),
])
def test_a_visible_foreign_release_is_refused_before_any_channel_moves(
        live, monkeypatch, capsys, change, error):
    assert live.prove().returncode == 0
    assets = own_assets(live)
    fields: dict = {}
    if change == "foreign_body":
        fields["body"] = "# Release 0.11.1\n\nForged\n"
    elif change == "prerelease":
        fields["isPrerelease"] = True
    elif change == "foreign_asset":
        assets["evil.bin"] = b"evil"
    elif change == "wheel_bytes":
        assets[WHEEL] = b"another wheel"
    elif change == "record_bytes":
        assets["release-record.json"] = b"{}"
    elif change == "published_incomplete":
        assets.pop(SDIST)
    elif change == "foreign_visible_draft":
        live.drafts_visible = True
        fields.update(isDraft=True, body="# Release 0.11.1\n\nForged\n")
        assets = {}
    put_release(live, assets=assets, **fields)
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 1
    assert error in out
    # The workflow stops in prove: promote-image never runs, channels stay.
    assert json.loads(live.ghcr.read_text())["tags"]["stable"] == PREVIOUS


@pytest.mark.parametrize("shape", ["visible_partial_draft", "complete_published"])
def test_this_releases_own_visible_release_is_accepted(live, monkeypatch, capsys, shape):
    assert live.prove().returncode == 0
    assets = own_assets(live)
    if shape == "visible_partial_draft":
        live.drafts_visible = True
        put_release(live, isDraft=True, assets={WHEEL: assets[WHEEL],
                                               "release-record.json":
                                               assets["release-record.json"]})
    else:
        put_release(live, assets=assets)
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 0, out


def test_an_unreadable_visible_release_fails_closed(live, monkeypatch, capsys):
    assert live.prove().returncode == 0
    put_release(live, assets=own_assets(live))
    real = live.gh_api

    def broken(*args):
        if args[:2] == ("release", "view"):
            raise subprocess.CalledProcessError(1, "gh", stderr=b"HTTP 502")
        return real(*args)

    live.gh_api = broken
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 1
    assert "cannot read the GitHub release v0.11.1" in out


def test_resume_state_needs_the_notes_and_record_with_a_tag(live, capsys):
    assert live.prove().returncode == 0
    code = release.main(["resume-state", "--version", VERSION, "--image-digest", CANDIDATE,
                         "--repo", REPO, "--tag-object", "a" * 40])
    assert code == 1
    assert "--notes and --record are required" in capsys.readouterr().err


def test_a_release_view_naming_another_tag_is_refused(live, monkeypatch, capsys):
    """The view answered for v0.11.1 must describe v0.11.1 itself."""
    assert live.prove().returncode == 0
    put_release(live, assets=own_assets(live), tagName="v0.11.0")
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 1
    assert "is unreadable or names another tag" in out


def test_a_tag_listed_twice_is_refused(live, monkeypatch, capsys):
    assert live.prove().returncode == 0
    put_release(live, assets=own_assets(live))
    real = live.gh_api

    def twice(*args):
        if args[:2] == ("api", "--paginate"):
            return json.dumps([[{"tag_name": TAG}], [{"tag_name": TAG}]]).encode()
        return real(*args)

    live.gh_api = twice
    code, out = live.resume_state(monkeypatch, capsys)
    assert code == 1
    assert "more than one release for v0.11.1" in out


def test_a_gh_timeout_is_unreadable_and_fails_closed(monkeypatch):
    calls = []

    def hang(*args, **kwargs):
        calls.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(release.subprocess, "run", hang)
    with pytest.raises(OSError, match="timed out after 60 s"):
        release._gh("release", "view", TAG)
    assert release._release_view(TAG, REPO) is None
    assert release._gh_tag_object(TAG, REPO) is None
    assert release._release_tags(REPO) is None
    assert calls == [60.0] * 4
