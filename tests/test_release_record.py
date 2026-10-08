"""Offline promotion proofs against real Git history."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import release_record as release
from scripts.sdlc_version import compute_version


class Promotion:
    def __init__(self, path):
        self.path = path
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("config", "commit.gpgsign", "false")
        self.b = self.commit("chore: base")
        self.git("tag", "v0.4.2")
        self.git("checkout", "-b", "release/new")
        (path / "payload").write_text("reviewed\n")
        self.h = self.commit("feat: add export\n\nExport structured records.")
        self.version = compute_version(
            cwd=path, head=self.h, main=self.b, branch="release/new",
        )
        self.tag = self.version["candidate"]
        self.git("tag", self.tag)
        self.provenance = {
            "source_sha": self.h, "H": self.h, "B": self.b, "M": self.b, "N": 1,
            "candidate_tag": self.tag, "python_version": "0.4.3rc1",
            "image_digest": "sha256:" + "a" * 64, "oci_archive_sha256": "b" * 64,
            "promotable": True, "dry_run": False,
            "candidate": {
                "version": "0.4.3rc1", "source_sha": self.h,
                "source_tree_sha256": "c" * 64, "lock_sha256": "d" * 64,
                "wheel_sha256": "e" * 64, "sdist_sha256": "f" * 64,
                "wheel": "data_olympus-0.4.3rc1-py3-none-any.whl",
                "sdist": "data_olympus-0.4.3rc1.tar.gz",
            },
        }
        self.notes = release.generate_notes(cwd=path, version=self.version)
        self.s = self.squash()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.path, text=True).strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def squash(self, *, parents=None, tree=None, message=None):
        args = ["commit-tree", tree or self.git("rev-parse", f"{self.h}^{{tree}}")]
        for parent in parents if parents is not None else [self.b]:
            args.extend(["-p", parent])
        args.extend(["-m", message or f"release: 0.4.3\n\n{self.notes}"])
        sha = self.git(*args)
        self.git("update-ref", "refs/heads/main", sha)
        return sha

    def prove(self, **overrides):
        return release.prove_release(**({
            "cwd": self.path, "head": self.h, "squash": self.s, "main": "main",
            "candidate_tag": self.tag, "provenance": self.provenance,
        } | overrides))


@pytest.fixture
def promotion(tmp_path):
    return Promotion(tmp_path)


def test_valid_proof_is_read_only_and_records_original_main(promotion):
    before = promotion.git("show-ref")
    result = promotion.prove()
    assert result == {
        "schema_version": 1, "H": promotion.h, "S": promotion.s,
        "B": promotion.b, "M": promotion.b, "tag": "v0.4.3", "target": "0.4.3",
        "image_digest": "sha256:" + "a" * 64, "candidate_tag": "0.4.3-rc.1",
        "candidate_version": "0.4.3rc1", "notes": promotion.notes,
    }
    assert promotion.git("show-ref") == before


@pytest.mark.parametrize(("change", "error"), [
    ("two_parents", "sole parent"), ("wrong_parent", "sole parent"),
    ("tree", "tree"), ("base", "B"), ("subject", "subject"),
    ("notes", "notes"), ("adoption", "ADOPTION"), ("existing_tag", "already exists"),
    ("not_promotable", "promotable"), ("main_moved", "main head"),
    ("tag_head", "RC tag"), ("recorded_head", "recorded H"),
])
def test_each_proof_refuses(promotion, change, error):
    p = promotion
    if change == "two_parents":
        p.s = p.squash(parents=[p.b, p.h])
    elif change == "wrong_parent":
        p.s = p.squash(parents=[p.h])
    elif change == "tree":
        p.s = p.squash(tree=p.git("rev-parse", f"{p.b}^{{tree}}"))
    elif change == "base":
        p.provenance["B"] = p.h
    elif change == "subject":
        p.s = p.squash(message=f"release: 0.4.4\n\n{p.notes}")
    elif change == "notes":
        p.s = p.squash(message="release: 0.4.3\n\nForged notes")
    elif change == "adoption":
        p.git("checkout", "release/new")
        (p.path / "release").mkdir()
        (p.path / "release/ADOPTION.json").write_text("{}")
        p.h = p.commit("chore: add adoption")
        p.provenance["H"] = p.h
        p.provenance["source_sha"] = p.h
        p.provenance["candidate"]["source_sha"] = p.h
        p.git("tag", "-f", p.tag, p.h)
        p.s = p.squash()
    elif change == "existing_tag":
        p.git("tag", "v0.4.3", p.s)
    elif change == "not_promotable":
        p.provenance["promotable"] = False
    elif change == "main_moved":
        p.squash(parents=[p.s])
    elif change == "tag_head":
        p.git("tag", "-f", p.tag, p.b)
    elif change == "recorded_head":
        p.provenance["H"] = p.b
    with pytest.raises(ValueError, match=error):
        p.prove()


@pytest.mark.parametrize(("field", "value"), [
    ("promotable", "true"), ("promotable", 1), ("dry_run", True), ("dry_run", 0),
    ("N", True), ("N", 0), ("N", 2), ("image_digest", "sha256:bad"),
    ("image_digest", "sha256:" + "a" * 64 + "\ninjected=true"),
    ("M", "main"), ("oci_archive_sha256", "bad"), ("python_version", "0.4.4rc1"),
    ("candidate_tag", "0.4.3-rc.01"),
])
def test_candidate_coordinates_fail_closed(promotion, field, value):
    promotion.provenance[field] = value
    with pytest.raises(ValueError):
        promotion.prove()


def test_candidate_hashes_and_paths_validated(promotion):
    for field in ("source_tree_sha256", "lock_sha256", "wheel_sha256", "sdist_sha256"):
        provenance = copy.deepcopy(promotion.provenance)
        provenance["candidate"][field] = "bad"
        with pytest.raises(ValueError, match=field):
            promotion.prove(provenance=provenance)
    for name in ("../outside.whl", "asset\nINJECTED=value"):
        provenance = copy.deepcopy(promotion.provenance)
        provenance["candidate"]["wheel"] = name
        with pytest.raises(ValueError, match="wheel"):
            promotion.prove(provenance=provenance)


@pytest.mark.parametrize("optional_outputs", [True, False])
def test_cli_writes_record_notes_and_safe_outputs(promotion, optional_outputs):
    p = promotion
    provenance = p.path / "candidate.json"
    provenance.write_text(json.dumps(p.provenance))
    script = Path(release.__file__).resolve()
    command = [
        sys.executable, str(script), "prove", "--head", p.h, "--squash", p.s,
        "--main", "main", "--candidate-tag", p.tag,
        "--candidate-provenance", str(provenance), "--output", "record.json",
    ]
    if optional_outputs:
        command.extend(["--notes-output", "notes.md", "--env-output", "outputs"])
    result = subprocess.run(command, cwd=p.path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((p.path / "record.json").read_text()) == p.prove()
    if not optional_outputs:
        assert not (p.path / "outputs").exists()
        assert not (p.path / "notes.md").exists()
        return
    assert (p.path / "notes.md").read_text() == p.notes
    assert (p.path / "outputs").read_text() == (
        f"tag=v0.4.3\nversion=0.4.3\nsource_sha={p.s}\n"
        f"image_digest=sha256:{'a' * 64}\nrc_tag={p.tag}\n"
    )


def test_cli_refusal_writes_no_outputs(promotion):
    p = promotion
    p.provenance["promotable"] = False
    (p.path / "candidate.json").write_text(json.dumps(p.provenance))
    result = subprocess.run([
        sys.executable, str(Path(release.__file__).resolve()), "prove", "--head", p.h,
        "--squash", p.s, "--main", "main", "--candidate-tag", p.tag,
        "--candidate-provenance", "candidate.json", "--output", "record.json",
    ], cwd=p.path, capture_output=True, text=True)
    assert result.returncode == 1
    assert "promotable" in result.stderr
    assert result.stdout == ""
    assert not (p.path / "record.json").exists()


def test_notes_grouping_retains_details_and_engine_grammar():
    notes = release.render_notes(target="2.0.0", messages=[
        "docs: clarify behavior\n\nmentions BREAKING CHANGE: no bump\n",
        "fix: repair (#12)\n",
        "feat: export\n\nSee https://example.invalid/issues/42\n",
        "fix!: replace API\n\nBREAKING-CHANGE: use v2\nRun upgrade tool.\n",
    ])
    assert notes.index("Breaking changes") < notes.index("Features") < notes.index("Fixes")
    assert notes.index("Fixes") < notes.index("Other changes")
    assert "  BREAKING-CHANGE: use v2\n  Run upgrade tool." in notes
    assert "https://example.invalid/issues/42" in notes
    assert "fix: repair (#12)" in notes
    with pytest.raises(release.VersionError, match="malformed_commit"):
        release.render_notes(target="2.0.0", messages=["Not conventional"])


def test_notes_use_engine_nonmerge_range(promotion):
    p = promotion
    p.git("checkout", "-b", "topic", p.b)
    p.commit("fix: on side branch")
    p.git("checkout", "release/new")
    p.git("merge", "--no-ff", "topic", "-m", "Synthetic merge")
    p.h = p.git("rev-parse", "HEAD")
    version = compute_version(cwd=p.path, head=p.h, main=p.b, branch="release/new")
    assert version["N"] == 3
    notes = release.generate_notes(cwd=p.path, version=version)
    assert "feat: add export" in notes
    assert "fix: on side branch" in notes
    assert "Synthetic merge" not in notes
    assert "chore: base" not in notes


def test_hotfix_proof_uses_dev_mapping(promotion):
    p = promotion
    p.git("checkout", "-b", "hotfix/new", p.b)
    (p.path / "payload").write_text("repaired\n")
    p.h = p.commit("fix: repair export")
    p.tag = "0.4.3-hotfix.rc.1"
    p.git("tag", p.tag)
    p.provenance.update(H=p.h, source_sha=p.h, candidate_tag=p.tag, python_version="0.4.3.dev1")
    p.provenance["candidate"].update(source_sha=p.h, version="0.4.3.dev1")
    p.version = compute_version(cwd=p.path, head=p.h, main=p.b, branch="hotfix/new")
    p.notes = release.generate_notes(cwd=p.path, version=p.version)
    p.s = p.squash()
    assert p.prove()["candidate_version"] == "0.4.3.dev1"


def test_resume_phase_accepts_advanced_main_and_own_annotated_tag(promotion):
    p = promotion
    initial = p.prove()
    later = p.squash(parents=[p.s], message="fix: unrelated later change")
    assert p.git("rev-parse", "main") == later
    p.git("tag", "-a", "v0.4.3", p.s, "-m", "Release 0.4.3")
    assert p.prove(phase="resume") == initial
    with pytest.raises(ValueError, match="main head"):
        p.prove()


@pytest.mark.parametrize(("change", "error"), [
    ("lightweight_tag", "already exists"), ("tag_elsewhere", "already exists"),
    ("rewritten_main", "no longer reachable"),
])
def test_resume_phase_still_fails_closed(promotion, change, error):
    p = promotion
    if change == "lightweight_tag":
        p.git("tag", "v0.4.3", p.s)
    elif change == "tag_elsewhere":
        p.git("tag", "-a", "v0.4.3", p.h, "-m", "Release 0.4.3")
    else:
        p.git("update-ref", "refs/heads/main", p.b)
    with pytest.raises(ValueError, match=error):
        p.prove(phase="resume")


def test_unknown_phase_is_refused(promotion):
    with pytest.raises(ValueError, match="phase"):
        promotion.prove(phase="later")
