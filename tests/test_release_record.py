"""Offline promotion proofs against real Git history."""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import release_record as release
from scripts.sdlc_version import compute_version

BOT = "sdlc-release[bot] <123+sdlc-release[bot]@users.noreply.github.com>"


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

    def stable_tag(self, target=None, *, notes=None, who=BOT, name="v0.4.3"):
        """Create an annotated tag the way create-tag does (verbatim notes)."""
        user, email = who[:-1].split(" <")
        (self.path / "tag-notes.md").write_text(self.notes if notes is None else notes)
        self.git("-c", f"user.name={user}", "-c", f"user.email={email}", "tag", "-a",
                 "--cleanup=verbatim", name, target or self.s, "-F", "tag-notes.md")

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


PHASE_INDEPENDENT = [
    ("two_parents", "sole parent"), ("wrong_parent", "sole parent"),
    ("tree", "tree mismatch"), ("base", "computed B differs"),
    ("engine_base", "computed B differs"), ("subject", "subject"),
    ("notes", "notes"), ("adoption", "ADOPTION"), ("existing_tag", "already exists"),
    ("not_promotable", "candidate must be promotable"),
    ("engine_not_promotable", "engine result must be promotable"),
    ("tag_head", "RC tag"), ("recorded_head", "recorded H"),
]


@pytest.mark.parametrize(("change", "error", "phase"), [
    *((change, error, phase) for phase in release.PHASES
      for change, error in PHASE_INDEPENDENT),
    # Both resume phases deliberately accept these two; see the resume tests.
    ("main_moved", "main head", "initial"),
    ("existing_annotated_tag", "already exists", "initial"),
])
def test_each_proof_refuses(promotion, monkeypatch, change, error, phase):
    """Every proof except the main head and own-tag rules is phase independent."""
    p = promotion
    if change == "two_parents":
        p.s = p.squash(parents=[p.b, p.h])
    elif change == "wrong_parent":
        p.s = p.squash(parents=[p.h])
    elif change == "tree":
        p.s = p.squash(tree=p.git("rev-parse", f"{p.b}^{{tree}}"))
    elif change == "base":
        p.provenance["B"] = p.h
    elif change == "engine_base":
        # S's sole parent equals the RC-recorded B, which is not the engine's
        # cut: only the engine B check can refuse this.
        p.git("checkout", "-b", "forged", p.b)
        forged = p.commit("chore: forged cut")
        p.provenance["B"] = forged
        p.s = p.squash(parents=[forged])
        assert p.git("rev-list", "--parents", "-n", "1", p.s).split()[1:] == [forged]
    elif change == "engine_not_promotable":
        real = release.compute_version
        monkeypatch.setattr(release, "compute_version",
                            lambda **kwargs: real(**kwargs) | {"promotable": False})
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
    elif change == "existing_annotated_tag":
        p.stable_tag()
    elif change == "not_promotable":
        p.provenance["promotable"] = False
    elif change == "main_moved":
        p.squash(parents=[p.s])
    elif change == "tag_head":
        p.git("tag", "-f", p.tag, p.b)
    elif change == "recorded_head":
        p.provenance["H"] = p.b
    with pytest.raises(ValueError, match=error):
        p.prove(phase=phase, tagger=BOT)


def test_engine_base_check_is_isolated_from_parent_check(promotion):
    """The engine B guard alone refuses a forged RC B that S also uses."""
    p = promotion
    p.git("checkout", "-b", "forged", p.b)
    forged = p.commit("chore: forged cut")
    p.provenance["B"] = forged
    p.s = p.squash(parents=[forged])
    facts = dict(
        version=p.version, provenance=p.provenance, squash=p.s, head=p.h,
        candidate_tag=p.tag, parents=[forged], head_tree="t", squash_tree="t",
        main_head=p.s, rc_head=p.h, message=f"release: 0.4.3\n\n{p.notes}",
        notes=p.notes, adoption_present=False, tag_exists=False,
    )
    with pytest.raises(ValueError, match="computed B differs"):
        release.validate_proof(**facts)


@pytest.mark.parametrize("version_change", [
    {"promotable": False}, {"promotable": False, "N": 0}, {"promotable": "true"},
])
def test_engine_non_promotable_result_is_refused(promotion, version_change):
    """R2: an N=0 or adoption cut stays refused even if the RC record says promotable."""
    p = promotion
    with pytest.raises(ValueError, match="engine result must be promotable"):
        release.validate_proof(
            version=p.version | version_change, provenance=p.provenance, squash=p.s,
            head=p.h, candidate_tag=p.tag, parents=[p.b], head_tree="t", squash_tree="t",
            main_head=p.s, rc_head=p.h, message=f"release: 0.4.3\n\n{p.notes}",
            notes=p.notes, adoption_present=False, tag_exists=False,
        )


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
    p.stable_tag()
    raw = subprocess.check_output(["git", "cat-file", "tag", "v0.4.3"], cwd=p.path, text=True)
    assert raw.endswith("\n\n" + p.notes)
    assert p.prove(phase="resume", tagger=BOT) == initial
    # The initial phase refuses the tag before the engine runs, then main.
    with pytest.raises(ValueError, match="stable tag already exists"):
        p.prove()
    p.git("tag", "-d", "v0.4.3")
    with pytest.raises(ValueError, match="main head"):
        p.prove()


@pytest.mark.parametrize(("change", "error"), [
    ("lightweight_tag", "already exists"), ("tag_elsewhere", "already exists"),
    ("rewritten_main", "no longer reachable"),
    ("stale_squash", "sole parent"), ("stale_release_tagged", "already exists"),
    ("tag_message", "message differs"), ("tag_default_cleanup", "message differs"),
    ("tag_tagger", "tagger differs"), ("tag_without_expected_tagger", "tagger identity"),
    ("tag_object_renamed", "does not name S"),
])
def test_resume_phase_still_fails_closed(promotion, change, error):
    p = promotion
    tagger = BOT
    if change == "lightweight_tag":
        p.git("tag", "v0.4.3", p.s)
    elif change == "tag_elsewhere":
        p.stable_tag(p.h)
    elif change == "rewritten_main":
        p.git("update-ref", "refs/heads/main", p.b)
    elif change == "stale_squash":
        # Replaying an older commit still on main as S.
        p.s = p.b
    elif change == "stale_release_tagged":
        # Two valid squashes of the same release are on main; the tagged one
        # is the published release, and the other is replayed as S.
        stale = p.s
        fresh = p.squash(message=f"release: 0.4.3\n\n{p.notes}\n")
        assert fresh != stale
        p.stable_tag(fresh)
        p.squash(parents=[stale, fresh], message="chore: join")
        p.s = stale
    elif change == "tag_message":
        p.stable_tag(notes="# Release 0.4.3\n\nForged notes\n")
    elif change == "tag_default_cleanup":
        (p.path / "n.md").write_text(p.notes)
        p.git("-c", "user.name=sdlc-release[bot]",
              "-c", "user.email=123+sdlc-release[bot]@users.noreply.github.com",
              "tag", "-a", "v0.4.3", p.s, "-F", "n.md")
    elif change == "tag_tagger":
        p.stable_tag(who="Someone Else <someone@example.invalid>")
    elif change == "tag_without_expected_tagger":
        p.stable_tag()
        tagger = None
    elif change == "tag_object_renamed":
        p.stable_tag(name="v9.9.9")
        p.git("update-ref", "refs/tags/v0.4.3", p.git("rev-parse", "refs/tags/v9.9.9"))
    with pytest.raises(ValueError, match=error):
        p.prove(phase="resume", tagger=tagger)


@pytest.mark.parametrize("tagger", ["no-email", "a <b>\nc <d>", "<only@email>"])
def test_tagger_identity_is_validated(promotion, tagger):
    p = promotion
    p.stable_tag()
    with pytest.raises(ValueError, match="tagger"):
        p.prove(phase="resume", tagger=tagger)


def cli_prove(p, phase, output):
    (p.path / "candidate.json").write_text(json.dumps(p.provenance))
    return subprocess.run([
        sys.executable, str(Path(release.__file__).resolve()), "prove", "--phase", phase,
        "--head", p.h, "--squash", p.s, "--main", "main", "--candidate-tag", p.tag,
        "--candidate-provenance", "candidate.json", "--tagger", BOT, "--output", output,
    ], cwd=p.path, capture_output=True, text=True)


def test_resume_record_is_byte_identical_only_for_the_same_release(promotion):
    """The workflow's cmp of initial and resume records detects any drift."""
    p = promotion
    assert cli_prove(p, "initial", "initial.json").returncode == 0
    p.squash(parents=[p.s], message="fix: unrelated later change")
    p.stable_tag()
    assert cli_prove(p, "resume", "resume.json").returncode == 0
    assert (p.path / "initial.json").read_bytes() == (p.path / "resume.json").read_bytes()
    p.provenance["image_digest"] = "sha256:" + "9" * 64
    assert cli_prove(p, "resume", "drift.json").returncode == 0
    assert (p.path / "initial.json").read_bytes() != (p.path / "drift.json").read_bytes()


def test_resume_pypi_accepts_s_on_an_advanced_main_without_a_stable_tag(promotion):
    """The resume after a PyPI upload: main moved on (the fix merged), no tag yet."""
    p = promotion
    initial = p.prove()
    later = p.squash(parents=[p.s], message="fix(release): verify published stable files")
    assert p.git("rev-parse", "main") == later
    assert p.git("merge-base", "--is-ancestor", p.s, "main") == ""
    assert p.prove(phase="resume-pypi") == initial
    assert p.prove(phase="resume-pypi", tagger=BOT) == initial
    with pytest.raises(ValueError, match="main head no longer equals S"):
        p.prove()


@pytest.mark.parametrize(("change", "error"), [
    ("lightweight_tag", "already exists"),
    ("tag_elsewhere", "already exists"),
    ("s_not_in_main", "no longer reachable"),
    ("s_beside_main", "no longer reachable"),
])
def test_resume_pypi_refuses_a_foreign_tag_or_an_s_outside_main(promotion, change, error):
    p = promotion
    if change == "lightweight_tag":
        p.git("tag", "v0.4.3", p.s)
    elif change == "tag_elsewhere":
        p.stable_tag(p.h)
    elif change == "s_not_in_main":
        p.git("update-ref", "refs/heads/main", p.b)
    elif change == "s_beside_main":
        p.git("checkout", "-b", "other", p.b)
        p.git("update-ref", "refs/heads/main", p.commit("fix: unrelated"))
    with pytest.raises(ValueError, match=error):
        p.prove(phase="resume-pypi", tagger=BOT)


def test_resume_pypi_record_is_byte_identical_to_the_original_record(promotion):
    """publish-pypi compares the resumed record with the one prove wrote."""
    p = promotion
    assert cli_prove(p, "initial", "initial.json").returncode == 0
    p.squash(parents=[p.s], message="fix: unrelated later change")
    assert cli_prove(p, "resume-pypi", "resumed.json").returncode == 0
    assert (p.path / "initial.json").read_bytes() == (p.path / "resumed.json").read_bytes()
    # After the App's own tag exists the resumed record is still the same.
    p.stable_tag()
    tagged = cli_prove(p, "resume-pypi", "tagged.json")
    assert tagged.returncode == 0, tagged.stderr
    assert (p.path / "initial.json").read_bytes() == (p.path / "tagged.json").read_bytes()
    p.git("tag", "-d", "v0.4.3")
    p.stable_tag(notes="# Release 0.4.3\n\nForged notes\n")
    refused = cli_prove(p, "resume-pypi", "forged.json")
    assert refused.returncode == 1
    assert "message differs" in refused.stderr
    assert not (p.path / "forged.json").exists()


def test_unknown_phase_is_refused(promotion):
    with pytest.raises(ValueError, match="phase"):
        promotion.prove(phase="later")


@pytest.mark.parametrize(("header", "error"), [
    ("object " + "0" * 40 + "\ntype commit\ntag v0.4.3", "does not name S"),
    ("object {s}\ntype tree\ntag v0.4.3", "does not name S"),
    ("object {s}\ntype commit\ntag v0.4.4", "does not name S"),
    ("object {s}\ntype commit\ntag v0.4.3\ntag v0.4.3", "duplicate header"),
    ("object {s}\ntype commit\ntag v0.4.3", "tagger differs"),
])
def test_tag_object_headers_are_checked(header, error):
    s = "a" * 40
    raw = header.format(s=s) + "\n\n# Release 0.4.3\n"
    with pytest.raises(ValueError, match=error):
        release.validate_tag_object(raw, squash=s, tag="v0.4.3",
                                    notes="# Release 0.4.3\n", tagger=BOT)


ANALYSES = "Analyze (python),Analyze (actions),Analyze (javascript-typescript)"
ANALYSIS_NAMES = ANALYSES.split(",")
H_SHA, S_SHA = "a" * 40, "b" * 40


def check_run(name, sha, run_id=1, *, slug="github-actions", status="completed",
              conclusion="success"):
    return {"id": run_id, "name": name, "head_sha": sha, "app": {"slug": slug},
            "status": status, "conclusion": conclusion}


def head_runs(sha=H_SHA):
    """The real shape on a release pull request head: aggregate CodeQL from GHAS."""
    runs = [check_run("CodeQL", sha, 1, slug="github-advanced-security")]
    runs += [check_run(name, sha, 2 + i) for i, name in enumerate(ANALYSIS_NAMES)]
    runs.append(check_run("test", sha, 9))
    return [{"total_count": len(runs), "check_runs": runs}]


def squash_runs(sha=S_SHA):
    """The real shape on a push to main: no aggregate CodeQL, all github-actions."""
    names = ["test", "test-python-3-14", "doc-consistency-guard", "update-uv-graph",
             *ANALYSIS_NAMES]
    runs = [check_run(name, sha, 1 + i) for i, name in enumerate(names)]
    return [{"total_count": len(runs), "check_runs": runs}]


def shape(role, sha=None):
    if role == "head":
        return head_runs(sha or H_SHA), sha or H_SHA
    return squash_runs(sha or S_SHA), sha or S_SHA


def drop(pages, name):
    pages[0]["check_runs"] = [r for r in pages[0]["check_runs"] if r["name"] != name]
    return pages


def test_real_shapes_pass():
    release.check_gate(head_runs(), sha=H_SHA, role="head", analyses=ANALYSES)
    release.check_gate(squash_runs(), sha=S_SHA, role="squash", analyses=ANALYSES)


def test_squash_without_codeql_is_accepted():
    """GitHub produces the aggregate CodeQL only on pull request heads, never on S."""
    pages = squash_runs()
    assert not any(r["name"] == "CodeQL" for r in pages[0]["check_runs"])
    release.check_gate(pages, sha=S_SHA, role="squash", analyses=ANALYSES)


@pytest.mark.parametrize("name", ANALYSIS_NAMES)
def test_squash_missing_an_analysis_is_refused(name):
    with pytest.raises(ValueError, match="missing required check: " + re.escape(name)):
        release.check_gate(drop(squash_runs(), name), sha=S_SHA, role="squash",
                           analyses=ANALYSES)


def test_squash_with_codeql_but_no_analyses_is_refused():
    pages = squash_runs()
    for name in ANALYSIS_NAMES:
        drop(pages, name)
    pages[0]["check_runs"].append(
        check_run("CodeQL", S_SHA, 50, slug="github-advanced-security"))
    with pytest.raises(ValueError, match=r"missing required check: Analyze \("):
        release.check_gate(pages, sha=S_SHA, role="squash", analyses=ANALYSES)


def test_squash_codeql_that_ran_must_succeed():
    pages = squash_runs()
    pages[0]["check_runs"].append(check_run("CodeQL", S_SHA, 50, conclusion="neutral",
                                            slug="github-advanced-security"))
    with pytest.raises(ValueError, match="did not succeed: CodeQL"):
        release.check_gate(pages, sha=S_SHA, role="squash", analyses=ANALYSES)


@pytest.mark.parametrize("fault", ["missing", "failed", "in_progress", "other_sha", "evil_app"])
def test_squash_analysis_must_exist_and_succeed(fault):
    pages = squash_runs()
    runs = pages[0]["check_runs"]
    victim = next(r for r in runs if r["name"] == "Analyze (actions)")
    if fault == "missing":
        runs.remove(victim)
    elif fault == "failed":
        victim["conclusion"] = "failure"
    elif fault == "in_progress":
        victim["status"], victim["conclusion"] = "in_progress", None
    elif fault == "evil_app":
        victim["app"]["slug"] = "evil"
    else:
        victim["head_sha"] = H_SHA
    with pytest.raises(ValueError, match=r"Analyze \(actions\)"):
        release.check_gate(pages, sha=S_SHA, role="squash", analyses=ANALYSES)


def test_analysis_from_advanced_security_app_is_refused():
    """Only the aggregate CodeQL may come from github-advanced-security."""
    pages = squash_runs()
    victim = next(r for r in pages[0]["check_runs"] if r["name"] == "Analyze (python)")
    victim["app"]["slug"] = "github-advanced-security"
    with pytest.raises(ValueError, match=r"missing required check: Analyze \(python\)"):
        release.check_gate(pages, sha=S_SHA, role="squash", analyses=ANALYSES)


def test_head_without_codeql_is_refused():
    with pytest.raises(ValueError, match="missing required check: CodeQL"):
        release.check_gate(drop(head_runs(), "CodeQL"), sha=H_SHA, role="head",
                           analyses=ANALYSES)


@pytest.mark.parametrize("slug", ["evil", "github-advanced-securityx", "", None,
                                  "github-actions"])
def test_head_codeql_from_the_wrong_app_is_refused(slug):
    pages = head_runs()
    victim = next(r for r in pages[0]["check_runs"] if r["name"] == "CodeQL")
    victim["app"]["slug"] = slug
    with pytest.raises(ValueError, match="missing required check: CodeQL"):
        release.check_gate(pages, sha=H_SHA, role="head", analyses=ANALYSES)


@pytest.mark.parametrize("slug", ["github-advanced-security", "github-code-scanning"])
def test_head_codeql_accepted_apps(slug):
    pages = head_runs()
    next(r for r in pages[0]["check_runs"] if r["name"] == "CodeQL")["app"]["slug"] = slug
    release.check_gate(pages, sha=H_SHA, role="head", analyses=ANALYSES)


def test_head_codeql_for_another_sha_is_refused():
    pages = head_runs()
    next(r for r in pages[0]["check_runs"] if r["name"] == "CodeQL")["head_sha"] = S_SHA
    with pytest.raises(ValueError, match="missing required check: CodeQL"):
        release.check_gate(pages, sha=H_SHA, role="head", analyses=ANALYSES)


def test_head_without_test_is_refused():
    with pytest.raises(ValueError, match="missing required check: test"):
        release.check_gate(drop(head_runs(), "test"), sha=H_SHA, role="head",
                           analyses=ANALYSES)


def test_head_analyses_are_optional_but_must_succeed_when_present():
    pages = head_runs()
    for name in ANALYSIS_NAMES:
        drop(pages, name)
    release.check_gate(pages, sha=H_SHA, role="head", analyses=ANALYSES)
    pages[0]["check_runs"].append(
        check_run("Analyze (python)", H_SHA, 50, conclusion="cancelled"))
    with pytest.raises(ValueError, match=r"did not succeed: Analyze \(python\)"):
        release.check_gate(pages, sha=H_SHA, role="head", analyses=ANALYSES)


@pytest.mark.parametrize("role", ["head", "squash"])
def test_wrong_app_slug_is_refused(role):
    pages, sha = shape(role)
    for run in pages[0]["check_runs"]:
        run["app"]["slug"] = "impostor-app"
    with pytest.raises(ValueError, match="missing required check: "):
        release.check_gate(pages, sha=sha, role=role, analyses=ANALYSES)


@pytest.mark.parametrize(("role", "name"), [
    ("head", "test"), ("head", "CodeQL"), ("squash", "test"), ("squash", "Analyze (python)"),
])
def test_latest_run_by_id_wins(role, name):
    pages, sha = shape(role)
    slug = "github-advanced-security" if name == "CodeQL" else "github-actions"
    pages.append({"check_runs": [check_run(name, sha, 90, slug=slug, conclusion="failure")]})
    with pytest.raises(ValueError, match="did not succeed: " + re.escape(name)):
        release.check_gate(pages, sha=sha, role=role, analyses=ANALYSES)
    pages.append({"check_runs": [check_run(name, sha, 91, slug=slug)]})
    release.check_gate(pages, sha=sha, role=role, analyses=ANALYSES)


def test_stale_success_does_not_mask_a_newer_failure():
    pages = squash_runs()
    victim = next(r for r in pages[0]["check_runs"] if r["name"] == "Analyze (actions)")
    victim["id"], victim["conclusion"] = 99, "failure"
    pages[0]["check_runs"].append(check_run("Analyze (actions)", S_SHA, 3))
    with pytest.raises(ValueError, match=r"did not succeed: Analyze \(actions\)"):
        release.check_gate(pages, sha=S_SHA, role="squash", analyses=ANALYSES)


@pytest.mark.parametrize("analyses", [
    "", " Analyze (python)", "Analyze (python) ", "Analyze (python), Analyze (actions)",
    "Analyze (python),,Analyze (actions)", "Analyze (python),",
])
@pytest.mark.parametrize("role", ["head", "squash"])
def test_whitespace_or_empty_analysis_names_are_refused(analyses, role):
    pages, sha = shape(role)
    with pytest.raises(ValueError, match="invalid required analysis names"):
        release.check_gate(pages, sha=sha, role=role, analyses=analyses)


@pytest.mark.parametrize("pages", [{}, [{"check_runs": None}], [[]], [{"check_runs": ["x"]}]])
def test_unreadable_check_runs_are_refused(pages):
    with pytest.raises(ValueError, match="unreadable check runs"):
        release.check_gate(pages, sha=S_SHA, role="squash", analyses=ANALYSES)


def test_check_gate_validates_sha_and_role():
    with pytest.raises(ValueError, match="check SHA"):
        release.check_gate(squash_runs(), sha="b" * 39, role="squash", analyses=ANALYSES)
    with pytest.raises(ValueError, match="head or squash"):
        release.check_gate(squash_runs(), sha=S_SHA, role="main", analyses=ANALYSES)


def test_checks_cli_applies_the_role_rules(tmp_path, capsys):
    head, squash = tmp_path / "head.json", tmp_path / "squash.json"
    head.write_text(json.dumps(head_runs()))
    squash.write_text(json.dumps(squash_runs()))
    base = ["checks", "--analyses", ANALYSES]
    assert release.main([*base, "--role", "head", "--sha", H_SHA,
                         "--check-runs", str(head)]) == 0
    assert release.main([*base, "--role", "squash", "--sha", S_SHA,
                         "--check-runs", str(squash)]) == 0
    head.write_text(json.dumps(drop(head_runs(), "CodeQL")))
    assert release.main([*base, "--role", "head", "--sha", H_SHA,
                         "--check-runs", str(head)]) == 1
    assert "missing required check: CodeQL" in capsys.readouterr().err


NOT_SUCCESS = ["neutral", "skipped", "cancelled", "timed_out", "action_required", "stale",
               "failure", None]


@pytest.mark.parametrize("conclusion", NOT_SUCCESS)
@pytest.mark.parametrize(("role", "name"), [
    ("head", "test"), ("head", "CodeQL"), ("head", "Analyze (python)"),
    ("squash", "test"), *[("squash", name) for name in ANALYSIS_NAMES],
])
def test_only_a_successful_conclusion_passes(role, name, conclusion):
    """Neutral, skipped and the other non-success conclusions never pass, on H or S.

    On H the analyses are optional, but one that ran must succeed.
    """
    pages, sha = shape(role)
    victim = next(r for r in pages[0]["check_runs"] if r["name"] == name)
    victim["conclusion"] = conclusion
    with pytest.raises(ValueError, match="did not succeed: " + re.escape(name)):
        release.check_gate(pages, sha=sha, role=role, analyses=ANALYSES)
    victim["conclusion"] = "success"
    release.check_gate(pages, sha=sha, role=role, analyses=ANALYSES)


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending"])
@pytest.mark.parametrize("role", ["head", "squash"])
def test_a_success_conclusion_on_an_unfinished_run_does_not_pass(role, status):
    pages, sha = shape(role)
    next(r for r in pages[0]["check_runs"] if r["name"] == "test")["status"] = status
    with pytest.raises(ValueError, match="did not succeed: test"):
        release.check_gate(pages, sha=sha, role=role, analyses=ANALYSES)


def test_notes_command_writes_a_file_and_does_not_echo_the_body(promotion):
    p = promotion
    script = Path(release.__file__).resolve()
    base = [sys.executable, str(script), "notes", "--head", p.h, "--main", p.b,
            "--branch", "release/new"]
    refused = subprocess.run(base, cwd=p.path, capture_output=True, text=True)
    assert refused.returncode != 0
    assert "--output" in refused.stderr
    done = subprocess.run([*base, "--output", "notes.md"], cwd=p.path,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    written = (p.path / "notes.md").read_text()
    assert "feat: add export" in written
    assert "feat: add export" not in done.stdout
    assert done.stdout.strip() == f"wrote {len(written.encode())} bytes to notes.md"
