"""The adoption record route reused after a release, across every engine caller.

After 0.11.1 was promoted, main carried more commits (#358 to #365; the
fixture models the first four release tooling repairs), so
the next cycle could not be cut at a stable tag. STD-U-821 amendment 1.5
(company-knowledge #236, ratified 2026-10-10) reuses the amendment 1.3 record
route for that case: the record names the highest stable tag as its base, and
the engine requires both ratifications. The fixture runs the first
adoption cycle to its squash S tagged v0.11.1, advances main past S, records
the second cut with base 0.11.1 and drives stage one, stage two and the
promotion proof, including its recheck once the new stable tag exists.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from scripts import adoption_ratification as ratification
from scripts import rc_decide
from scripts import release_record as release
from scripts.sdlc_version import VersionError, compute_version
from tests.test_adoption_cut import ROOT, Cut, promote, stage_two
from tests.test_promotion_resume_after_tag import BOT, BOT_EMAIL, BOT_NAME

if TYPE_CHECKING:
    from pathlib import Path

POST_RELEASE = (
    "fix(release): verify published stable files without their attestations (#358)",
    "fix(release): continue a promotion after the stable tag exists (#359)",
    "docs(release): record that the pypi environments have no reviewers (#360)",
    "docs(release): the pypi environment admits only main (#361)",
)


def released(path: Path, *, base: str = "0.11.1") -> Cut:
    """First cycle promoted and tagged v0.11.1, main advanced, second cut recorded."""
    cut = Cut(path)
    cut.placeholder()
    cut.retire()
    first = promote(cut, cut.engine(cut.preflight()))
    assert first["tag"] == "v0.11.1"
    squash = cut.git("rev-parse", "refs/heads/main")
    cut.git("tag", "-a", "v0.11.1", "-m", "Release 0.11.1", squash)
    cut.git("checkout", "-q", "-B", "main", squash)
    cut.git("branch", "-q", "-D", "release/new")
    for index, message in enumerate(POST_RELEASE):
        cut.write(f"docs/post-release-{index}.md", f"{message}\n")
        anchor = cut.commit(message)
    cut.anchor = anchor
    cut.write("release/ADOPTION.json",
              json.dumps({"anchor": anchor, "base": base}, indent=2) + "\n")
    cut.c = cut.commit("chore(release): record adoption cut")
    cut.git("checkout", "-q", "-b", "release/new")
    return cut


@pytest.fixture
def second(tmp_path):
    return released(tmp_path / "repo")


def test_second_cut_shape(second):
    assert second.git("describe", "--tags", "--abbrev=0", second.c) == "v0.11.1"
    assert second.git("rev-list", "--count", f"v0.11.1..{second.anchor}") == "4"
    assert second.git("diff", "--name-status", second.anchor, second.c) == (
        "A\trelease/ADOPTION.json")
    assert second.git("tag", "--points-at", second.c) == ""


def test_stage_one_and_two_agree_through_the_second_cycle(second, tmp_path):
    admission = second.preflight()
    assert (admission.adoption, admission.engine_branch) == ("ratified", "release/new")
    at_cut = second.engine(admission)
    assert at_cut == second.trusted()
    assert (at_cut["base"], at_cut["B"], at_cut["candidate"], at_cut["promotable"]) == (
        "v0.11.1", second.c, "0.11.2-rc.0", False)
    second.retire()
    second.write("src/next.py", "NEXT = True\n")
    second.commit("feat(search): next cycle feature")
    version = second.engine(second.preflight())
    assert (version["candidate"], version["pypi_version"], version["N"]) == (
        "0.11.2-rc.2", "0.11.2rc2", 2)
    assert (version["promotable"], version["adoption_retired"]) == (True, True)
    assert rc_decide.decide(version, dry_run=False)["promotable"] is True
    verified = stage_two(second, tmp_path)
    assert (verified.version, verified.base, verified.main) == ("0.11.2-rc.2", second.c,
                                                                second.c)


def test_promotion_proves_and_rechecks_after_its_own_tag(second, tmp_path):
    second.retire()
    second.write("src/next.py", "NEXT = True\n")
    second.commit("fix(server): next cycle fix")
    version = second.engine(second.preflight())
    record = promote(second, version)
    assert (record["tag"], record["target"], record["B"]) == ("v0.11.2", "0.11.2", second.c)
    assert record["candidate_tag"] == "0.11.2-rc.2"
    notes = record["notes"]
    # Notes run over v0.11.1..H: the post-release main commits are present, the
    # first cycle's commits and the 0.11.1 squash are not.
    for message in POST_RELEASE:
        assert f"- {message}" in notes
    assert "sanitize deny reasons" not in notes
    assert "release: 0.11.1" not in notes
    squash = second.git("rev-parse", "refs/heads/main")
    notes_file = tmp_path / "notes.md"
    notes_file.write_text(notes)
    second.git("-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "tag", "-a",
               "--cleanup=verbatim", "v0.11.2", squash, "-F", str(notes_file))
    provenance = {
        "source_sha": version["H"], "H": version["H"], "B": version["B"], "M": version["M"],
        "N": version["N"], "candidate_tag": version["candidate"],
        "python_version": version["pypi_version"], "image_digest": "sha256:" + "a" * 64,
        "oci_archive_sha256": "b" * 64, "promotable": True, "dry_run": False,
        "candidate": {
            "version": version["pypi_version"], "source_sha": version["H"],
            "source_tree_sha256": "c" * 64, "lock_sha256": "d" * 64,
            "wheel_sha256": "e" * 64, "sdist_sha256": "f" * 64,
            "wheel": f"data_olympus-{version['pypi_version']}-py3-none-any.whl",
            "sdist": f"data_olympus-{version['pypi_version']}.tar.gz",
        },
    }
    rechecked = release.prove_release(
        cwd=second.path, squash=squash, head=version["H"], main="refs/heads/main",
        candidate_tag=version["candidate"], provenance=provenance, phase="resume", tagger=BOT)
    assert (rechecked["tag"], rechecked["notes"]) == ("v0.11.2", notes)
    # The engine alone, without the verified tag ignored, refuses: single use.
    # Promotion recomputes against the RC's recorded M, which is B.
    def recompute(**kwargs):
        return compute_version(cwd=second.path, head=version["H"], main=version["M"],
                               branch="release/new", **ratification.engine_kwargs(ROOT),
                               **kwargs)

    assert recompute(ignore_tags=frozenset({"v0.11.2"})) == version
    with pytest.raises(VersionError, match="bad_adoption"):
        recompute()


@pytest.mark.parametrize("base", ["0.11.0", "0.11.2", "v0.11.1", "0.11"])
def test_stage_one_refuses_a_second_cut_without_the_highest_base(tmp_path, base):
    cut = released(tmp_path / "repo", base=base)
    with pytest.raises(VersionError, match="bad_adoption"):
        cut.preflight()


def retire_with_a_fix(cut: Cut) -> str:
    """The single commit on release/new: a fix that also retires the record."""
    cut.git("rm", "-q", "release/ADOPTION.json")
    cut.write("src/next.py", "FIXED = True\n")
    return cut.commit("fix(server): next cycle fix")


def test_next_cycle_is_promotable_only_with_both_ratifications(second, tmp_path):
    """Base 0.11.1, anchor four commits past the tag, record, one fix: 0.11.2-rc.1."""
    head = retire_with_a_fix(second)
    admission = second.preflight()
    version = second.engine(admission)
    assert version == second.trusted()
    assert {key: version[key] for key in ("base", "B", "M", "H", "N", "candidate",
                                          "pypi_version", "promotable", "adoption")} == {
        "base": "v0.11.1", "B": second.c, "M": second.c, "H": head, "N": 1,
        "candidate": "0.11.2-rc.1", "pypi_version": "0.11.2rc1", "promotable": True,
        "adoption": "ratified",
    }
    assert rc_decide.decide(version, dry_run=False)["promotable"] is True
    kwargs = ratification.engine_kwargs(ROOT)

    def compute(**changes):
        return compute_version(cwd=second.path, head="HEAD", main="refs/heads/main",
                               branch="release/new", **(kwargs | changes))

    for missing in ({"extension_ratified": None}, {"adoption_ratified": None},
                    {"extension_ratified": None, "adoption_ratified": None}):
        with pytest.raises(VersionError, match="adoption_unratified"):
            compute(**missing)
        dry = compute(adoption_dry_run=True, **missing)
        assert (dry["candidate"], dry["adoption"], dry["promotable"]) == (
            "0.11.2-rc.1", "unratified", False)
    for forged in ({"extension_ratified": "PENDING-RATIFICATION"},
                   {"extension_ratified": "2026-10-09:knaisoma/company-knowledge@2441459"},
                   {"extension_standard_file": ROOT / ratification.STANDARD_FILE}):
        with pytest.raises(VersionError, match="amendment 1.5"):
            compute(**forged)
    verified = stage_two(second, tmp_path)
    assert (verified.version, verified.base) == ("0.11.2-rc.1", second.c)
    assert promote(second, version)["tag"] == "v0.11.2"


def test_next_cycle_without_main_ahead_of_the_tag_is_refused(tmp_path):
    """A record whose anchor is the tagged squash itself (main not ahead) is refused."""
    cut = Cut(tmp_path / "repo")
    cut.placeholder()
    cut.retire()
    promote(cut, cut.engine(cut.preflight()))
    squash = cut.git("rev-parse", "refs/heads/main")
    cut.git("tag", "-a", "v0.11.1", "-m", "Release 0.11.1", squash)
    cut.git("checkout", "-q", "-B", "main", squash)
    cut.git("branch", "-q", "-D", "release/new")
    cut.write("release/ADOPTION.json",
              json.dumps({"anchor": squash, "base": "0.11.1"}, indent=2) + "\n")
    cut.c = cut.commit("chore(release): record adoption cut")
    cut.git("checkout", "-q", "-b", "release/new")
    with pytest.raises(VersionError, match="strict ancestor of the anchor"):
        cut.preflight()
