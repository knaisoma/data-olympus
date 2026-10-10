"""Git-backed contract tests for STD-U-821 and the W6 adoption ruling."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from scripts import sdlc_version as engine
from scripts.adoption_ratification import (
    EXTENSION_RATIFIED,
    EXTENSION_STANDARD_FILE,
    RATIFIED,
    STANDARD_FILE,
    engine_args,
)

EXTENSION_HEADING = "## Amendment 1.5: release tooling repairs on main after a release"


def extension_flags(repo, date_text="2020-01-01"):
    """CLI flags for a valid amendment 1.5 ratification in the published heading shape."""
    extension = repo.path / ".git" / "cli-extension-standard.md"
    extension.write_text(f"{EXTENSION_HEADING}\n\nRatification: {date_text}\n\n### Why\n")
    return ["--extension-ratified", f"{date_text}:PR#456",
            "--extension-standard-file", str(extension)]


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("config", "commit.gpgsign", "false")
        self.commit("chore: initial cut")

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.path, text=True).strip()

    def commit(self, message: str) -> str:
        self.git("add", ".")
        self.git("commit", "--allow-empty", "--allow-empty-message", "-m", message)
        return self.git("rev-parse", "HEAD")

    def cut(self, tag="v1.4.2"):
        if tag:
            self.git("tag", tag)
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-b", "release/new")
        return base

    def write(self, name, text):
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def compute(self, **kwargs):
        options = dict(cwd=self.path, head="HEAD", main="main", branch="release/new",
                       adoption_ratified="2026-10-07:PR#123", today=date(2026, 10, 7))
        options |= kwargs
        if "standard_file" not in kwargs and isinstance(options["adoption_ratified"], str):
            # A standard ratified on the same date, kept outside the work tree.
            standard = self.path / ".git" / "test-standard.md"
            standard.write_text(
                f"## Amendment 1.3\n\nRatification: {options['adoption_ratified'][:10]}\n")
            options["standard_file"] = standard
        options.setdefault("extension_ratified", "2026-10-07:PR#456")
        if ("extension_standard_file" not in kwargs
                and isinstance(options["extension_ratified"], str)):
            # The amendment 1.5 standard, in the heading shape it is published with.
            extension = self.path / ".git" / "test-extension-standard.md"
            extension.write_text(
                "## Amendment 1.5: release tooling repairs on main after a release\n\n"
                f"Ratification: {options['extension_ratified'][:10]}\n\n### Why\n")
            options["extension_standard_file"] = extension
        return engine.compute_version(**options)


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


@pytest.mark.parametrize(("base", "message", "target"), [
    ("v1.4.2", "fix: repair export", "1.4.3"),
    ("v1.4.2", "feat: add export", "1.5.0"),
    ("v1.4.2", "feat!: replace export API", "2.0.0"),
    ("v0.4.2", "feat: add export", "0.4.3"),
    ("v0.4.2", "fix!: remove legacy format", "0.5.0"),
    ("v1.4.2", "docs: clarify export", "1.4.3"),
])
def test_standard_examples(repo, base, message, target):
    cut = repo.cut(base)
    repo.commit(message)
    repo.commit("docs: update guide")
    head = repo.commit("ci: check guide")
    assert repo.compute() == {
        "base": base, "B": cut, "M": cut, "H": head, "N": 3,
        "target": target, "candidate": f"{target}-rc.3",
        "pypi_version": f"{target}rc3", "promotable": True,
    }


@pytest.mark.parametrize("branch", ["release/new", "hotfix/new"])
def test_cut_not_promotable(repo, branch):
    repo.cut()
    result = repo.compute(branch=branch)
    assert result["N"] == 0
    assert not result["promotable"]
    assert result["target"] == "1.4.3"


def test_hotfix_identity(repo):
    repo.cut()
    for _ in range(3):
        repo.commit("fix: repair export")
    result = repo.compute(branch="hotfix/new")
    assert result["candidate"] == "1.4.3-hotfix.rc.3"
    assert result["pypi_version"] == "1.4.3.dev3"


@pytest.mark.parametrize("message", ["feat: add", "fix!: break", "docs: x\n\nBREAKING CHANGE: x"])
def test_hotfix_refuses_features_and_breaking(repo, message):
    repo.cut("v0.4.2")
    repo.commit(message)
    with pytest.raises(engine.VersionError, match="hotfix_scope"):
        repo.compute(branch="hotfix/new")


@pytest.mark.parametrize("message", [
    "", "Fix: repair", "fix:", "fix: ", "fix:  text", "fix:\ttext",
    "fix(): repair", "fix(BAD): repair", 'Revert "feat: add"', "not conventional",
])
def test_malformed_commit(repo, message):
    repo.cut()
    repo.commit(message)
    with pytest.raises(engine.VersionError, match="malformed_commit"):
        repo.compute()


@pytest.mark.parametrize(("body", "target"), [
    ("BREAKING CHANGE: migrate", "2.0.0"),
    ("BREAKING-CHANGE: migrate", "2.0.0"),
    ("BREAKING CHANGE:", "1.4.3"),
    ("BREAKING CHANGE:   ", "1.4.3"),
    ("mentions BREAKING CHANGE: migrate", "1.4.3"),
    (" BREAKING CHANGE: migrate", "1.4.3"),
])
def test_footer_grammar(repo, body, target):
    repo.cut()
    repo.commit(f"docs: explain\n\n{body}")
    assert repo.compute()["target"] == target


def test_rewritten_revert_does_not_subtract(repo):
    repo.cut()
    repo.commit("feat: add export")
    repo.commit("revert: feat: add export")
    assert repo.compute()["target"] == "1.5.0"


def test_merge_counts_but_synthetic_subject_does_not(repo):
    cut = repo.cut()
    repo.git("checkout", "-b", "feature", cut)
    repo.commit("feat: add export")
    repo.git("checkout", "release/new")
    repo.commit("docs: guide")
    repo.git("merge", "--no-ff", "feature", "-m", "Synthetic merge")
    result = repo.compute()
    assert result["N"] == 3
    assert result["target"] == "1.5.0"


def test_main_advanced(repo):
    repo.cut()
    repo.commit("fix: pending")
    repo.git("checkout", "main")
    repo.commit("fix: hotfix")
    repo.git("checkout", "release/new")
    with pytest.raises(engine.VersionError, match="recut_required") as error:
        repo.compute()
    assert error.value.exit_code == 3


def test_multiple_merge_bases(repo):
    repo.cut()
    a = repo.commit("fix: a")
    repo.git("checkout", "main")
    b = repo.commit("fix: b")
    tree = repo.git("rev-parse", "HEAD^{tree}")
    left = repo.git("commit-tree", tree, "-p", a, "-p", b, "-m", "Merge left")
    right = repo.git("commit-tree", tree, "-p", b, "-p", a, "-m", "Merge right")
    with pytest.raises(engine.VersionError, match="multiple_merge_bases"):
        repo.compute(head=left, main=right)


@pytest.mark.parametrize("tag", ["v01.4.2", "v1.04.2", "v1.4.02", "v1.4", "v1.4.2oops"])
def test_invalid_stable_tag(repo, tag):
    repo.cut(tag)
    with pytest.raises(engine.VersionError, match="invalid_stable_tag"):
        repo.compute()


def test_ambiguous_stable_tag(repo):
    repo.cut()
    repo.git("tag", "v1.4.3")
    with pytest.raises(engine.VersionError, match="ambiguous_stable_tag"):
        repo.compute()


def test_annotated_tag_and_rc_tag(repo):
    repo.git("tag", "-a", "v1.4.2", "-m", "Release")
    repo.cut("v1.4.3-rc.1")
    assert repo.compute()["base"] == "v1.4.2"


def test_missing_exact_tag_is_not_nearest_tag(repo):
    repo.git("tag", "v1.4.2")
    repo.commit("chore: advance")
    repo.cut(None)
    with pytest.raises(engine.VersionError, match="missing_tags_in_released_product"):
        repo.compute()


def test_bootstrap(repo):
    cut = repo.cut(None)
    assert repo.compute(bootstrap={"cut": cut, "target": "0.1.0"})["candidate"] == "0.1.0-rc.0"


@pytest.mark.parametrize(("cut", "target", "code"), [
    ("main", "0.1.0", "bad_bootstrap_cut"),
    ("0" * 40, "0.1.0", "bootstrap_mismatch"),
    (None, "0.2.0", "bad_bootstrap"),
    (None, "1.0.0", "bad_bootstrap"),
])
def test_invalid_bootstrap(repo, cut, target, code):
    base = repo.cut(None)
    with pytest.raises(engine.VersionError, match=code):
        repo.compute(bootstrap={"cut": cut or base, "target": target})


def test_ga_reads_only_head_tree(repo):
    repo.cut("v0.4.2")
    repo.write("release/GA-DECISION", "approved\n")
    assert repo.compute()["target"] == "0.4.3"
    head = repo.commit("chore: approve GA")
    repo.write("release/GA-DECISION", "rejected\n")
    assert repo.compute()["target"] == "1.0.0"
    repo.commit("chore: reject GA")
    with pytest.raises(engine.VersionError, match="bad_ga_decision"):
        repo.compute()
    assert repo.compute(head=head)["target"] == "1.0.0"


def adoption(repo, record=None):
    """Create the adoption cut with ordinary commits and an ancestor record."""
    repo.git("tag", "v0.11.0")
    anchor = repo.commit("chore: adoption anchor")
    repo.write("release/ADOPTION.json", json.dumps(
        record if record is not None else {"anchor": anchor, "base": "0.11.0"},
    ))
    cut = repo.commit("chore: record adoption")
    repo.cut(None)
    return cut


def test_adoption_accept_and_read_only_cut(repo):
    cut = adoption(repo)
    assert repo.compute()["candidate"] == "0.11.1-rc.0"
    repo.write("release/ADOPTION.json", "invalid")
    repo.commit("chore: retire adoption record")
    result = repo.compute()
    assert result["B"] == cut
    assert result["candidate"] == "0.11.1-rc.1"


@pytest.mark.parametrize("record", [
    {"anchor": "main", "base": "0.11.0"},
    {"anchor": "0" * 40, "base": "0.11.0"},
    {"anchor": "0" * 40, "base": "0.10.0"},
    {"anchor": None, "base": "0.11.0"},
    {"anchor": 123, "base": "0.11.0"},
    {"cut": "main", "base": "0.11.0"},
    {"cut": "0" * 40, "base": "0.11.0"},
    {"cut": "0" * 40, "base": "0.10.0"},
])
def test_adoption_reject_mismatch(repo, record):
    adoption(repo, record)
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute()


@pytest.mark.parametrize("kind", ["unrelated", "abbreviated", "tree", "distant", "existing"])
def test_adoption_reject_invalid_anchor_or_diff(repo, kind):
    repo.git("tag", "v0.11.0")
    anchor = repo.git("rev-parse", "HEAD")
    if kind == "unrelated":
        tree = repo.git("rev-parse", "HEAD^{tree}")
        anchor = repo.git("commit-tree", tree, "-m", "chore: unrelated")
    elif kind == "abbreviated":
        anchor = anchor[:12]
    elif kind == "tree":
        anchor = repo.git("rev-parse", "HEAD^{tree}")
    elif kind == "distant":
        repo.commit("chore: intervening empty commit")
    elif kind == "existing":
        repo.write("release/ADOPTION.json", "{}")
        anchor = repo.commit("chore: earlier record")
    repo.write("release/ADOPTION.json", json.dumps({"anchor": anchor, "base": "0.11.0"}))
    repo.commit("chore: record adoption")
    repo.cut(None)
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute()


@pytest.mark.parametrize("change", ["add", "modify", "rename"])
def test_adoption_rejects_other_paths_in_single_commit(repo, change):
    repo.git("tag", "v0.11.0")
    repo.write("existing.py", "original\n")
    anchor = repo.commit("chore: anchor")
    if change == "rename":
        repo.git("mv", "existing.py", "renamed.py")
    else:
        repo.write("source.py" if change == "add" else "existing.py", "changed\n")
    repo.write("release/ADOPTION.json", json.dumps({"anchor": anchor, "base": "0.11.0"}))
    repo.commit("chore: record adoption")
    repo.cut(None)
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute()


def test_adoption_rejects_merge_cut(repo):
    repo.git("tag", "v0.11.0")
    anchor = repo.commit("chore: anchor")
    side = repo.commit("chore: side")
    repo.write("release/ADOPTION.json", json.dumps({"anchor": anchor, "base": "0.11.0"}))
    repo.commit("chore: record adoption")
    tree = repo.git("rev-parse", "HEAD^{tree}")
    cut = repo.git("commit-tree", tree, "-p", anchor, "-p", side, "-m", "Merge side")
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute(head=cut, main=cut)


@pytest.mark.parametrize(("message", "target"), [
    ("feat: before anchor", "0.11.1"),
    ("fix!: before anchor", "0.12.0"),
    ("malformed", None),
])
def test_adoption_parses_from_stable_tag(repo, message, target):
    repo.git("tag", "v0.11.0")
    repo.commit(message)
    anchor = repo.commit("chore: anchor")
    repo.write("release/ADOPTION.json", json.dumps({"anchor": anchor, "base": "0.11.0"}))
    repo.commit("chore: record adoption")
    repo.cut(None)
    repo.commit("docs: after cut")
    if target is None:
        with pytest.raises(engine.VersionError, match="malformed_commit"):
            repo.compute()
    else:
        result = repo.compute()
        assert result["target"] == target
        assert result["N"] == 1


@pytest.mark.parametrize("tag", ["v0.9.0", "v0.100.0"])
def test_adoption_compares_stable_tags_numerically(repo, tag):
    adoption(repo)
    repo.git("tag", tag, "v0.11.0")
    if tag == "v0.9.0":
        assert repo.compute()["base"] == "v0.11.0"
    else:
        with pytest.raises(engine.VersionError, match="bad_adoption"):
            repo.compute()


# The record route after a release: main advanced past the last stable tag.
POST_RELEASE = (
    "fix(release): verify published stable files (#358)",
    "fix(release): continue a promotion after the stable tag exists (#359)",
    "docs(release): record that the pypi environments have no reviewers (#360)",
    "docs(release): the pypi environment admits only main (#361)",
)


def released_twice(repo, *, base="0.11.1", tag_s=True):
    """v0.11.0, a release squash tagged v0.11.1, four main commits, then the cut.

    The squash carries a breaking commit before it, so impact computed from
    v0.11.0 instead of v0.11.1 would name 0.12.0. Returns (squash, anchor, cut).
    """
    repo.git("tag", "-a", "v0.11.0", "-m", "Release 0.11.0")
    repo.commit("feat!: breaking change released in 0.11.1")
    squash = repo.commit("release: 0.11.1")
    if tag_s:
        repo.git("tag", "-a", "v0.11.1", "-m", "Release 0.11.1")
    for message in POST_RELEASE:
        anchor = repo.commit(message)
    repo.write("release/ADOPTION.json",
               json.dumps({"anchor": anchor, "base": base}, indent=2) + "\n")
    cut = repo.commit("chore(release): record adoption cut")
    repo.cut(None)
    return squash, anchor, cut


def test_record_route_after_a_release_simulates_the_next_cycle(repo):
    """The live shape after 0.11.1: base 0.11.1, impact from the tag, N from B."""
    _, _, cut = released_twice(repo)
    at_cut = repo.compute()
    assert at_cut == {
        "base": "v0.11.1", "B": cut, "M": cut, "H": cut, "N": 0, "target": "0.11.2",
        "candidate": "0.11.2-rc.0", "pypi_version": "0.11.2rc0", "promotable": False,
        "adoption": "ratified", "adoption_retired": False,
    }
    repo.git("rm", "-q", "release/ADOPTION.json")
    retired = repo.commit("chore(release): retire adoption record")
    result = repo.compute()
    assert (result["H"], result["N"], result["candidate"]) == (retired, 1, "0.11.2-rc.1")
    assert (result["promotable"], result["adoption_retired"]) == (True, True)
    head = repo.commit("feat(search): next cycle feature")
    assert repo.compute() == {
        "base": "v0.11.1", "B": cut, "M": cut, "H": head, "N": 2, "target": "0.11.2",
        "candidate": "0.11.2-rc.2", "pypi_version": "0.11.2rc2", "promotable": True,
        "adoption": "ratified", "adoption_retired": True,
    }
    # The impact range is v0.11.1..H: the post-release fixes and docs, the
    # record and its retirement and the feature, never the breaking commit
    # released in 0.11.1 nor the squash subject itself.
    subjects = repo.git("log", "--no-merges", "--format=%s", "v0.11.1..HEAD").splitlines()
    assert subjects == ["feat(search): next cycle feature",
                        "chore(release): retire adoption record",
                        "chore(release): record adoption cut", *reversed(POST_RELEASE)]
    repo.commit("fix!: drop the legacy format")
    assert repo.compute()["candidate"] == "0.12.0-rc.3"


def test_record_route_after_a_release_survives_its_own_promotion_tag(repo):
    """Promotion recomputes after tagging S with only its own tag ignored."""
    _, _, cut = released_twice(repo)
    repo.git("rm", "-q", "release/ADOPTION.json")
    repo.commit("chore(release): retire adoption record")
    head = repo.commit("fix: next cycle fix")
    version = repo.compute(main=cut)
    squash = repo.git("commit-tree", f"{head}^{{tree}}", "-p", cut, "-m", "release: 0.11.2")
    repo.git("update-ref", "refs/heads/main", squash)
    repo.git("tag", "-a", "v0.11.2", "-m", "Release 0.11.2", squash)
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute(main=cut)
    assert repo.compute(main=cut, ignore_tags=frozenset({"v0.11.2"})) == version
    assert version["candidate"] == "0.11.2-rc.2"
    # Ignoring the base itself leaves no valid base: the record is refused.
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute(main=cut, ignore_tags=frozenset({"v0.11.1"}))


@pytest.mark.parametrize("base", [
    "0.11.0",  # exists and is an ancestor, but v0.11.1 is higher
    "0.11.2", "0.12.0", "1.0.0",  # no such tag
    "0.11", "v0.11.1", "0.11.1-rc.1", "0.11.1+build", " 0.11.1", "0.11.1 ", "0.11.1\n",
    "00.11.1", "0.011.1", "0.11.01", "", 0.11, None, ["0.11.1"], {"v": "0.11.1"},
])
def test_record_route_after_a_release_refuses_other_bases(repo, base):
    released_twice(repo, base=base)
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute()


def test_record_without_any_stable_tag_is_refused(repo):
    """No stable tag at all: a record is refused, never treated as a first release."""
    anchor = repo.commit("fix: work")
    repo.write("release/ADOPTION.json", json.dumps({"anchor": anchor, "base": "0.11.1"}))
    repo.commit("chore(release): record adoption cut")
    repo.cut(None)
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute()


def test_record_route_after_a_release_refuses_a_missing_base_tag(repo):
    released_twice(repo, tag_s=False)
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute()


@pytest.mark.parametrize("tag", ["v0.11.2", "v0.12.0", "v1.0.0"])
def test_record_route_after_a_release_refuses_a_newer_stable_tag(repo, tag):
    """A newer stable tag anywhere, even off B's history, invalidates the record."""
    squash, _, _ = released_twice(repo)
    side = repo.git("commit-tree", f"{squash}^{{tree}}", "-p", squash, "-m", "fix: side")
    repo.git("tag", tag, side)
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute()
    repo.git("tag", "-d", tag)
    assert repo.compute()["base"] == "v0.11.1"


def test_record_route_after_a_release_refuses_a_base_off_b_history(repo):
    """v0.11.1 is the highest stable tag but tags a side commit, not an ancestor of B."""
    squash, _, _ = released_twice(repo, tag_s=False)
    side = repo.git("commit-tree", f"{squash}^{{tree}}", "-p", squash, "-m", "release: 0.11.1")
    repo.git("tag", "-a", "v0.11.1", "-m", "Release 0.11.1", side)
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute()


def test_record_route_after_a_release_ignores_the_record_on_a_tagged_cut(repo):
    _, _, cut = released_twice(repo)
    repo.git("tag", "-a", "v0.11.2", "-m", "Release 0.11.2", cut)
    result = repo.compute()
    assert (result["base"], result["B"], result["candidate"]) == ("v0.11.2", cut, "0.11.3-rc.0")
    assert "adoption" not in result


@pytest.mark.parametrize("kind", ["record_at_anchor", "extra_file", "distant_anchor"])
def test_record_route_after_a_release_keeps_the_cut_rules(repo, kind):
    repo.git("tag", "-a", "v0.11.0", "-m", "Release 0.11.0")
    repo.commit("release: 0.11.1")
    repo.git("tag", "-a", "v0.11.1", "-m", "Release 0.11.1")
    if kind == "record_at_anchor":
        # The first cycle's record was never retired: single use.
        repo.write("release/ADOPTION.json", json.dumps({"anchor": "0" * 40, "base": "0.11.0"}))
    anchor = repo.commit(POST_RELEASE[0])
    if kind == "distant_anchor":
        repo.commit(POST_RELEASE[1])
    if kind == "extra_file":
        repo.write("src/extra.py", "extra = 1\n")
    repo.write("release/ADOPTION.json", json.dumps({"anchor": anchor, "base": "0.11.1"}))
    repo.commit("chore(release): record adoption cut")
    repo.cut(None)
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute()


def test_record_route_after_a_release_refuses_a_merge_cut(repo):
    _, anchor, _ = released_twice(repo)
    side = repo.git("commit-tree", f"{anchor}^{{tree}}", "-p", anchor, "-m", "chore: side")
    tree = repo.git("rev-parse", "HEAD^{tree}")
    merge = repo.git("commit-tree", tree, "-p", anchor, "-p", side, "-m", "Merge side")
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute(head=merge, main=merge)


def test_record_route_after_a_release_keeps_ratification_and_hotfix_rules(repo):
    released_twice(repo)
    with pytest.raises(engine.VersionError, match="hotfix_scope"):
        repo.compute(branch="hotfix/new")
    with pytest.raises(engine.VersionError, match="adoption_unratified"):
        repo.compute(adoption_ratified=None)
    assert repo.compute(adoption_ratified=None, adoption_dry_run=True)["promotable"] is False


@pytest.mark.parametrize(("flags", "state"), [
    ([], None),
    (["--adoption-dry-run"], "unratified"),
    (["--adoption-ratified", "2020-01-01:PR#123"], "ratified"),
])
def test_adoption_cli_ratification(repo, flags, state):
    adoption(repo)
    if state == "ratified":
        standard = repo.path / "standard.md"
        standard.write_text("## Proposed amendment 1.3\nRatification: 2020-01-01\n")
        flags = [*flags, "--standard-file", str(standard), *extension_flags(repo)]
    repo.git("rm", "release/ADOPTION.json")
    repo.commit("fix: after cut")
    script = Path(engine.__file__)
    result = subprocess.run(
        [sys.executable, str(script), "--main", "main", "--branch", "release/new", *flags],
        cwd=repo.path, capture_output=True, text=True,
    )
    if state is None:
        assert result.returncode != 0
        assert "adoption_unratified" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert output["adoption"] == state
        assert output["promotable"] is (state == "ratified")


@pytest.mark.parametrize("standard", [None, "missing"])
def test_adoption_api_requires_the_standard_file(repo, standard):
    """The engine refuses a ratification without the standard, not only the CLI."""
    adoption(repo)
    path = None if standard is None else repo.path / ".git" / "missing.md"
    with pytest.raises(engine.VersionError, match="adoption_unratified"):
        repo.compute(standard_file=path)


def test_standard_file_not_needed_without_an_adoption_cut(repo):
    repo.cut()
    repo.commit("fix: repair export")
    assert repo.compute(standard_file=None)["candidate"] == "1.4.3-rc.1"


def test_adoption_api_requires_ratification(repo):
    adoption(repo)
    with pytest.raises(engine.VersionError, match="adoption_unratified"):
        repo.compute(adoption_ratified=None)


@pytest.mark.parametrize("ratified", [
    "", "approved", "20261007", "2026-10-07", "2026-02-30:PR#123",
    "2026-10-07:", "2026-10-07:two tokens", "2026-10-07:PR#123\n",
    "2026-1-07:PR#123", "2026-10-08:PR#123",
])
def test_adoption_rejects_invalid_ratification_date(repo, ratified):
    adoption(repo)
    with pytest.raises(engine.VersionError, match="adoption_unratified"):
        repo.compute(adoption_ratified=ratified)


def test_adoption_dry_run_env_blocks_promotion(repo):
    adoption(repo)
    repo.commit("fix: after cut")
    result = repo.compute(adoption_ratified=None, adoption_dry_run=True)
    assert result["adoption"] == "unratified"
    assert "PROMOTABLE=false\n" in engine.env_lines(result)


def test_adoption_reject_newer_stable(repo):
    adoption(repo)
    repo.commit("fix: later")
    repo.git("tag", "v0.12.0")
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute()


def test_adoption_ignores_prerelease_and_unrelated_tags(repo):
    adoption(repo)
    repo.git("tag", "v0.12.0-rc.3")
    repo.git("tag", "release-marker")
    assert repo.compute()["candidate"] == "0.11.1-rc.0"


@pytest.mark.parametrize("record", [None, {"base": "wrong"}, "invalid"])
def test_adoption_ignored_with_stable_on_cut(repo, record):
    adoption(repo, record)
    repo.git("tag", "v0.11.1")
    repo.commit("fix: repair export")
    result = repo.compute(adoption_ratified=None)
    assert result["base"] == "v0.11.1"
    assert result["candidate"] == "0.11.2-rc.1"


@pytest.mark.parametrize(("branch", "candidate", "pypi"), [
    ("release/new", "0.11.2-rc.1", "0.11.2rc1"),
    ("hotfix/new", "0.11.2-hotfix.rc.1", "0.11.2.dev1"),
])
def test_post_release_tag_ignores_retained_adoption(repo, branch, candidate, pypi):
    adoption(repo)
    repo.write("source.py", "repaired\n")
    repo.commit("fix: repair export")
    repo.git("checkout", "main")
    repo.git("merge", "--squash", "release/new")
    cut = repo.commit("release: 0.11.1")
    repo.git("tag", "v0.11.1")
    repo.git("branch", "-D", "release/new")
    repo.git("checkout", "-b", branch)
    head = repo.commit("fix: follow-up repair")
    assert repo.compute(branch=branch) == {
        "base": "v0.11.1", "B": cut, "M": cut, "H": head, "N": 1,
        "target": "0.11.2", "candidate": candidate,
        "pypi_version": pypi, "promotable": False,
    }


@pytest.mark.parametrize("entry", ["file", "executable", "symlink", "tree", "gitlink", "absent"])
def test_adoption_retirement_required_at_head(repo, entry):
    cut = adoption(repo)
    repo.git("rm", "release/ADOPTION.json")
    if entry in ("file", "executable"):
        repo.write("release/ADOPTION.json", "invalid")
        if entry == "executable":
            (repo.path / "release/ADOPTION.json").chmod(0o755)
    elif entry == "symlink":
        (repo.path / "release").mkdir(exist_ok=True)
        (repo.path / "release/ADOPTION.json").symlink_to("missing")
    elif entry == "tree":
        repo.write("release/ADOPTION.json/child", "present")
    elif entry == "gitlink":
        repo.git("update-index", "--add", "--cacheinfo", f"160000,{cut},release/ADOPTION.json")
    if entry == "gitlink":
        repo.git("commit", "-m", "chore: update adoption record")
    else:
        repo.commit("chore: update adoption record")
    result = repo.compute()
    assert result["N"] == 1
    assert result["promotable"] is (entry == "absent")
    assert result["adoption_retired"] is (entry == "absent")


@pytest.mark.parametrize("ratified", ["2026-10-06:PR#123", "2026-10-07:https://example.test/pr/123"])
def test_adoption_ratification_today_or_past(repo, ratified):
    adoption(repo)
    result = repo.compute(adoption_ratified=ratified, today=date(2026, 10, 7))
    assert result["adoption"] == "ratified"


@pytest.mark.parametrize("heading", [
    "## Amendment 1.3",
    "## Amendment 1.3: adoption cut and public-product preview",
    "## Proposed amendment 1.3",
    "## Proposed amendment 1.3: adoption cut and public-product preview",
])
@pytest.mark.parametrize("line", [
    "Ratification: 2020-01-01", "Ratification: not yet recorded",
    "Ratification: 2020-01-02", "prefix Ratification: 2020-01-01",
    "Ratification: 2020-01-01 suffix", "",
])
def test_adoption_cli_standard_ratification(repo, line, heading):
    adoption(repo)
    standard = repo.path / "standard.md"
    standard.write_text(f"# Standard\n{heading}\n{line}\n"
                        "### Adoption cut for an already released product\n")
    result = subprocess.run(
        [sys.executable, str(Path(engine.__file__)), "--main", "main", "--branch", "release/new",
         "--adoption-ratified", "2020-01-01:PR#123", "--standard-file", str(standard),
         *extension_flags(repo)],
        cwd=repo.path, capture_output=True, text=True,
    )
    if line == "Ratification: 2020-01-01":
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["adoption"] == "ratified"
    else:
        assert result.returncode == 1
        assert "adoption_unratified" in result.stderr


@pytest.mark.parametrize("text", [
    None,
    "Ratification: 2020-01-01\n",
    "Ratification: 2020-01-01\n## Proposed amendment 1.3\nRatification: not yet recorded\n",
    "## Proposed amendment 1.3\n### Other section\nRatification: 2020-01-01\n",
    "## Proposed amendment 1.3\n## Other amendment\nRatification: 2020-01-01\n",
    "## Proposed amendment 1.30\nRatification: 2020-01-01\n",
    "## Amendment 1.3\n### Other section\nRatification: 2020-01-01\n",
    "## Amendment 1.3\n## Other amendment\nRatification: 2020-01-01\n",
    "## Amendment 1.30\nRatification: 2020-01-01\n",
])
def test_adoption_cli_requires_scoped_standard_ratification(repo, text):
    adoption(repo)
    args = [sys.executable, str(Path(engine.__file__)), "--main", "main",
            "--branch", "release/new", "--adoption-ratified", "2020-01-01:PR#123"]
    if text is not None:
        standard = repo.path / "standard.md"
        standard.write_text(text)
        args.extend(["--standard-file", str(standard)])
    result = subprocess.run(args, cwd=repo.path, capture_output=True, text=True)
    assert result.returncode == 1
    assert "adoption_unratified" in result.stderr
    if text is None:
        assert "--standard-file" in result.stderr


def test_vendored_adoption_ratification(repo):
    adoption(repo)
    root = Path(__file__).resolve().parents[1]
    standard = root / STANDARD_FILE
    text = standard.read_text(encoding="utf-8")
    assert "## Amendment 1.3: adoption cut and public-product preview" in text.splitlines()
    assert "Ratification: 2026-10-07" in text.splitlines()
    assert "### Adoption cut for an already released product" in text.splitlines()
    extension = root / EXTENSION_STANDARD_FILE
    result = repo.compute(adoption_ratified=RATIFIED, standard_file=standard,
                          extension_ratified=EXTENSION_RATIFIED,
                          extension_standard_file=extension, today=date(2026, 10, 10))
    assert result["adoption"] == "ratified"

    # Workflows resolve the standard files relative to their repository root.
    repo.write(STANDARD_FILE, text)
    repo.write(EXTENSION_STANDARD_FILE, extension.read_text(encoding="utf-8"))
    result = subprocess.run(
        [sys.executable, str(Path(engine.__file__)),
         "--main", "main", "--branch", "release/new", *engine_args()],
        cwd=repo.path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["adoption"] == "ratified"


def test_vendored_adoption_rejects_future_ratification(repo):
    adoption(repo)
    standard = Path(__file__).resolve().parents[1] / STANDARD_FILE
    with pytest.raises(engine.VersionError, match="adoption_unratified"):
        repo.compute(adoption_ratified=RATIFIED, standard_file=standard, today=date(2026, 10, 6))


def test_cli_json_env_and_exit_code(repo):
    repo.cut()
    script = Path(__file__).resolve().parents[1] / "scripts/sdlc_version.py"
    args = [sys.executable, str(script), "--head", "HEAD", "--main", "main",
            "--branch", "release/new"]
    result = subprocess.run(args, cwd=repo.path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["candidate"] == "1.4.3-rc.0"
    result = subprocess.run([*args, "--format", "env"], cwd=repo.path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "RC_VERSION=1.4.3-rc.0\nPYPI_VERSION=1.4.3rc0\nTARGET=1.4.3\n" in result.stdout
    assert "N=0\nPROMOTABLE=false\n" in result.stdout
    repo.git("checkout", "main")
    repo.commit("fix: hotfix")
    repo.git("checkout", "release/new")
    result = subprocess.run(args, cwd=repo.path, capture_output=True, text=True)
    assert result.returncode == 3
    assert "recut_required" in result.stderr


@pytest.mark.parametrize("message", ["perf: speed up", "revert: fix: repair", "build: deps"])
def test_other_types_patch_floor(repo, message):
    repo.cut()
    repo.commit(message)
    assert repo.compute()["candidate"] == "1.4.3-rc.1"


def test_ga_bootstrap(repo):
    cut = repo.cut(None)
    repo.write("release/GA-DECISION", "approved")
    repo.commit("chore: approve launch")
    result = repo.compute(bootstrap={"cut": cut, "target": "1.0.0"})
    assert result["candidate"] == "1.0.0-rc.1"


def test_hotfix_requires_current_stable(repo):
    repo.cut()
    repo.commit("fix: later")
    repo.git("tag", "v1.4.3")
    with pytest.raises(engine.VersionError, match="hotfix_scope"):
        repo.compute(branch="hotfix/new")


def test_hotfix_cannot_use_adoption(repo):
    adoption(repo)
    with pytest.raises(engine.VersionError, match="hotfix_scope"):
        repo.compute(branch="hotfix/new")


@pytest.mark.parametrize("record", ["invalid", "[]", "null", '{"base": "0.11.0"}'])
def test_adoption_malformed_record(repo, record):
    repo.git("tag", "v0.11.0")
    repo.write("release/ADOPTION.json", record)
    repo.commit("chore: record adoption")
    repo.cut(None)
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute()


def test_adoption_base_must_be_ancestor(repo):
    adoption(repo)
    repo.git("tag", "-d", "v0.11.0")
    tree = repo.git("rev-parse", "HEAD^{tree}")
    unrelated = repo.git("commit-tree", tree, "-m", "chore: unrelated")
    repo.git("tag", "v0.11.0", unrelated)
    with pytest.raises(engine.VersionError, match="bad_adoption"):
        repo.compute()


def test_adoption_at_head_does_not_supply_cut_record(repo):
    repo.git("tag", "v0.11.0")
    repo.commit("chore: advance")
    cut = repo.cut(None)
    repo.write("release/ADOPTION.json", json.dumps({"anchor": cut, "base": "0.11.0"}))
    repo.commit("chore: add record too late")
    with pytest.raises(engine.VersionError, match="missing_tags_in_released_product"):
        repo.compute()


def test_no_merge_base(repo):
    repo.cut()
    tree = repo.git("rev-parse", "HEAD^{tree}")
    unrelated = repo.git("commit-tree", tree, "-m", "chore: unrelated")
    with pytest.raises(engine.VersionError, match="no_merge_base"):
        repo.compute(head=unrelated)


def test_bad_ref_and_branch(repo):
    repo.cut()
    with pytest.raises(engine.VersionError, match="bad_ref"):
        repo.compute(head="--help")
    with pytest.raises(engine.VersionError, match="bad_branch"):
        repo.compute(branch="main")


def test_cli_files_and_error_codes(repo):
    repo.cut(None)
    script = Path(__file__).resolve().parents[1] / "scripts/sdlc_version.py"
    args = [sys.executable, str(script), "--main", "main", "--branch", "release/new"]
    result = subprocess.run([*args, "--bootstrap-cut", "main"], cwd=repo.path,
                            capture_output=True, text=True)
    assert result.returncode == 4
    assert "bad_bootstrap_cut" in result.stderr
    cut = repo.git("rev-parse", "HEAD")
    result = subprocess.run([
        *args, "--bootstrap-cut", cut, "--bootstrap-target", "0.1.0",
        "--json-output", "version.json", "--env-output", "version.env",
    ], cwd=repo.path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (repo.path / "version.json").read_text() == result.stdout
    assert "RC_VERSION=0.1.0-rc.0" in (repo.path / "version.env").read_text()
    outside = repo.path / "outside"
    outside.mkdir()
    # A child of a repo is still a repo; force discovery to stop at its parent.
    import os
    result = subprocess.run(args, cwd=outside, capture_output=True, text=True,
                            env=os.environ | {"GIT_CEILING_DIRECTORIES": str(repo.path)})
    assert result.returncode == 5
    assert "git_error" in result.stderr


# STD-U-821 amendment 1.5: the second ratification, required with 1.3.
def extension_file(repo, text, name="extension.md"):
    path = repo.path / ".git" / name
    path.write_text(text)
    return path


@pytest.mark.parametrize("text", [
    f"{EXTENSION_HEADING}\nRatification: 2026-10-07\n",
    f"# Standard\n\n{EXTENSION_HEADING}\n\nStatus: RATIFIED\n\nRatification: 2026-10-07\n"
    "\n### Why\n\nRatification: 2020-01-01\n",
    "## Amendment 1.5\nRatification: 2026-10-07\n",
    "## Proposed amendment 1.5\nRatification: 2026-10-07\n",
    "## Proposed amendment 1.5: release tooling repairs\nRatification: 2026-10-07\n",
])
def test_extension_ratification_accepted(repo, text):
    adoption(repo)
    result = repo.compute(extension_standard_file=extension_file(repo, text))
    assert result["adoption"] == "ratified"


@pytest.mark.parametrize("text", [
    "",
    "Ratification: 2026-10-07\n",  # no heading at all
    "## Amendment 1.3\nRatification: 2026-10-07\n",  # the other amendment
    "## Amendment 1.4\nRatification: 2026-10-07\n",
    "## Amendment 1.50\nRatification: 2026-10-07\n",
    "## Amendment 1.5 release tooling\nRatification: 2026-10-07\n",
    "### Amendment 1.5\nRatification: 2026-10-07\n",
    "# Amendment 1.5\nRatification: 2026-10-07\n",
    "## Proposed amendment 1.5\nRatification: not yet recorded\n",
    "## Proposed amendment 1.5\n",
    f"{EXTENSION_HEADING}\n\n### Why\n\nRatification: 2026-10-07\n",  # later subsection
    f"{EXTENSION_HEADING}\n\n## Version history\n\nRatification: 2026-10-07\n",
    f"{EXTENSION_HEADING}\nRatification: 2026-10-06\n",  # date mismatch
    f"{EXTENSION_HEADING}\nRatification: 2026-10-07 suffix\n",
    f"{EXTENSION_HEADING}\nprefix Ratification: 2026-10-07\n",
    f"{EXTENSION_HEADING}\nratification: 2026-10-07\n",
], ids=["empty", "no-heading", "amendment-1.3", "amendment-1.4", "amendment-1.50",
        "no-colon", "h3-heading", "h1-heading", "unrecorded", "no-line", "later-subsection",
        "later-section", "date-mismatch", "suffix", "prefix", "lowercase"])
def test_extension_ratification_refused_standard(repo, text):
    adoption(repo)
    with pytest.raises(engine.VersionError, match="adoption_unratified: amendment 1.5"):
        repo.compute(extension_standard_file=extension_file(repo, text))


@pytest.mark.parametrize("ratified", [
    "PENDING-RATIFICATION", "NOT-YET-RATIFIED", "", "2026-10-07", "2026-10-07:",
    "2026-10-07:two tokens", "TBD:knaisoma/company-knowledge@2441459",
    "2026-02-30:PR#456", "2026-10-08:PR#456",  # invalid and future dates
])
def test_extension_ratification_refused_value(repo, ratified):
    adoption(repo)
    standard = extension_file(repo, f"{EXTENSION_HEADING}\nRatification: {ratified[:10]}\n")
    with pytest.raises(engine.VersionError, match="adoption_unratified: amendment 1.5"):
        repo.compute(extension_ratified=ratified, extension_standard_file=standard)


@pytest.mark.parametrize("value", [20261007, b"2026-10-07:PR#456", ["2026-10-07:PR#456"]])
def test_ratification_values_must_be_strings(repo, value):
    adoption(repo)
    with pytest.raises(engine.VersionError, match="amendment 1.5"):
        repo.compute(extension_ratified=value,
                     extension_standard_file=extension_file(
                         repo, f"{EXTENSION_HEADING}\nRatification: 2026-10-07\n"))
    with pytest.raises(engine.VersionError, match="amendment 1.3"):
        repo.compute(adoption_ratified=value, standard_file=repo.path / ".git" / "x.md")


@pytest.mark.parametrize("path", [None, "missing"])
def test_extension_ratification_refused_without_its_file(repo, path):
    adoption(repo)
    standard = None if path is None else repo.path / ".git" / "missing.md"
    with pytest.raises(engine.VersionError, match="adoption_unratified: amendment 1.5"):
        repo.compute(extension_standard_file=standard)


def test_extension_ratification_does_not_read_the_1_3_file(repo):
    """Each ratification is checked against its own file, never the other one."""
    adoption(repo)
    both = extension_file(repo, f"## Amendment 1.3\nRatification: 2026-10-07\n\n"
                                f"{EXTENSION_HEADING}\nRatification: 2026-10-07\n")
    assert repo.compute(standard_file=both, extension_standard_file=both)["adoption"] == (
        "ratified")
    only_13 = extension_file(repo, "## Amendment 1.3\nRatification: 2026-10-07\n", "13.md")
    with pytest.raises(engine.VersionError, match="amendment 1.5"):
        repo.compute(standard_file=only_13, extension_standard_file=only_13)


@pytest.mark.parametrize("missing", ["extension", "adoption", "both"])
def test_adoption_requires_both_ratifications(repo, missing):
    adoption(repo)
    repo.git("rm", "-q", "release/ADOPTION.json")
    repo.commit("chore(release): retire adoption record")
    options = {}
    if missing in ("extension", "both"):
        options["extension_ratified"] = None
    if missing in ("adoption", "both"):
        options["adoption_ratified"] = None
    with pytest.raises(engine.VersionError, match="adoption_unratified"):
        repo.compute(**options)
    # Dry run computes the same candidate, unratified and never promotable.
    result = repo.compute(adoption_dry_run=True, **options)
    assert (result["candidate"], result["adoption"], result["promotable"]) == (
        "0.11.1-rc.1", "unratified", False)
    both = repo.compute()
    assert (both["candidate"], both["adoption"], both["promotable"]) == (
        "0.11.1-rc.1", "ratified", True)


def test_dry_run_still_checks_a_given_extension(repo):
    adoption(repo)
    with pytest.raises(engine.VersionError, match="amendment 1.5"):
        repo.compute(adoption_dry_run=True, extension_ratified="PENDING-RATIFICATION")


def test_original_0_11_0_adoption_passes_with_both_ratifications(repo):
    """Regression: the first adoption (base 0.11.0) now needs 1.3 and 1.5."""
    cut = adoption(repo)
    repo.commit("chore(release): use the unreleased version placeholder")
    repo.git("rm", "-q", "release/ADOPTION.json")
    repo.commit("chore(release): retire adoption record")
    assert repo.compute() | {"H": None} == {
        "base": "v0.11.0", "B": cut, "M": cut, "H": None, "N": 2, "target": "0.11.1",
        "candidate": "0.11.1-rc.2", "pypi_version": "0.11.1rc2", "promotable": True,
        "adoption": "ratified", "adoption_retired": True,
    }
    with pytest.raises(engine.VersionError, match="adoption_unratified"):
        repo.compute(extension_ratified=None)


def test_extension_cli_requires_its_standard_file(repo):
    adoption(repo)
    standard = repo.path / ".git" / "cli-standard.md"
    standard.write_text("## Amendment 1.3\nRatification: 2020-01-01\n")
    base = [sys.executable, str(Path(engine.__file__)), "--main", "main",
            "--branch", "release/new", "--adoption-ratified", "2020-01-01:PR#123",
            "--standard-file", str(standard)]
    result = subprocess.run([*base, "--extension-ratified", "2020-01-01:PR#456"],
                            cwd=repo.path, capture_output=True, text=True)
    assert result.returncode == 1
    assert "--extension-standard-file" in result.stderr
    result = subprocess.run(base, cwd=repo.path, capture_output=True, text=True)
    assert result.returncode == 1
    assert "amendment 1.5 ratification missing" in result.stderr
    result = subprocess.run([*base, *extension_flags(repo)], cwd=repo.path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["adoption"] == "ratified"


def test_vendored_extension_ratification(repo):
    """The vendored 1.5 copy satisfies the engine with the real pinned constant."""
    adoption(repo)
    root = Path(__file__).resolve().parents[1]
    extension = root / EXTENSION_STANDARD_FILE
    lines = extension.read_text(encoding="utf-8").splitlines()
    assert EXTENSION_HEADING in lines
    assert f"Ratification: {EXTENSION_RATIFIED[:10]}" in lines
    assert EXTENSION_RATIFIED == "2026-10-10:knaisoma/company-knowledge@2441459"
    result = repo.compute(extension_ratified=EXTENSION_RATIFIED,
                          extension_standard_file=extension, today=date(2026, 10, 10))
    assert result["adoption"] == "ratified"
    with pytest.raises(engine.VersionError, match="amendment 1.5"):
        repo.compute(extension_ratified=EXTENSION_RATIFIED,
                     extension_standard_file=extension, today=date(2026, 10, 9))
    with pytest.raises(engine.VersionError, match="amendment 1.5"):
        repo.compute(extension_ratified=EXTENSION_RATIFIED,
                     extension_standard_file=root / STANDARD_FILE, today=date(2026, 10, 10))


# Amendment 1.5 nits: strict ancestry and malformed release history.
def test_record_refused_when_main_is_not_ahead_of_the_tag(repo):
    """Anchor at the tagged release squash: the normal cut exists, so no record."""
    repo.git("tag", "-a", "v0.11.0", "-m", "Release 0.11.0")
    anchor = repo.commit("release: 0.11.1")
    repo.git("tag", "-a", "v0.11.1", "-m", "Release 0.11.1")
    repo.write("release/ADOPTION.json", json.dumps({"anchor": anchor, "base": "0.11.1"}))
    repo.commit("chore(release): record adoption cut")
    repo.cut(None)
    with pytest.raises(engine.VersionError, match="strict ancestor of the anchor"):
        repo.compute()
    with pytest.raises(engine.VersionError, match="strict ancestor of the anchor"):
        repo.compute(adoption_dry_run=True, adoption_ratified=None, extension_ratified=None)


def test_record_accepted_one_commit_past_the_tag(repo):
    repo.git("tag", "-a", "v0.11.1", "-m", "Release 0.11.1")
    anchor = repo.commit("fix(release): repair after the tag")
    repo.write("release/ADOPTION.json", json.dumps({"anchor": anchor, "base": "0.11.1"}))
    repo.commit("chore(release): record adoption cut")
    repo.cut(None)
    assert repo.compute()["candidate"] == "0.11.2-rc.0"


@pytest.mark.parametrize("tag", [
    "v0.11.01", "v0.12", "v1", "v0.12.0.1", "v0.12.0-rc.01", "v0.12.0-", "v0.12.0+",
    "v01.0.0", "v0.12.0_rc1", "v0.12.0+build",
])
@pytest.mark.parametrize("where", ["side", "b", "anchor"])
def test_record_refused_with_a_malformed_version_tag_anywhere(repo, tag, where):
    squash, anchor, cut = released_twice(repo)
    target = {"b": cut, "anchor": anchor,
              "side": repo.git("commit-tree", f"{squash}^{{tree}}", "-p", squash,
                               "-m", "fix: side")}[where]
    repo.git("tag", tag, target)
    with pytest.raises(engine.VersionError, match="invalid_stable_tag"):
        repo.compute()
    repo.git("tag", "-d", tag)
    assert repo.compute()["base"] == "v0.11.1"


@pytest.mark.parametrize("tag", [
    "0.11.1-rc.5", "0.12.0", "version-0.12.0", "archive/release-manager-mvp",
    "benchmarks/receipt-0.11.0", "vnext", "v-0.12.0", "V0.12.0", "v0.12.0-rc.1",
    "v0.12.0-alpha", "v0.12.0-rc.1+build.5",
])
def test_record_ignores_non_version_and_strict_prerelease_tags(repo, tag):
    squash, _, _ = released_twice(repo)
    side = repo.git("commit-tree", f"{squash}^{{tree}}", "-p", squash, "-m", "fix: side")
    repo.git("tag", tag, side)
    assert repo.compute()["base"] == "v0.11.1"


# Every tag name in data-olympus history up to 0.11.1 (git tag --list on 2026-10-10).
REAL_STABLE_TAGS = (
    "v0.1.0", "v0.1.1", "v0.2.0", "v0.3.0", "v0.3.1", "v0.3.2", "v0.3.3", "v0.3.4",
    "v0.3.5", "v0.4.0", "v0.4.1", "v0.4.2", "v0.5.0", "v0.6.0", "v0.7.0", "v0.7.1",
    "v0.7.2", "v0.7.3", "v0.8.0", "v0.8.1", "v0.8.2", "v0.9.0", "v0.10.0", "v0.11.0",
    "v0.11.1",
)
REAL_OTHER_TAGS = (
    "0.5.0-rc.2", "0.6.0-rc.1", "0.6.0-rc.2", "0.6.0-rc.4", "0.7.0-rc.1", "0.7.0-rc.2",
    "0.7.1-rc.1", "0.7.2-rc.1", "0.7.3-rc.1", "0.8.0-rc.1", "0.8.0-rc.2", "0.8.1-rc.1",
    "0.8.2-rc.1", "0.9.0-rc.1", "0.9.0-rc.2", "0.10.0-rc.1", "0.10.0-rc.2", "0.10.0-rc.3",
    "0.11.0-rc.1", "0.11.0-rc.2", "0.11.1-rc.2", "0.11.1-rc.3", "0.11.1-rc.4",
    "0.11.1-rc.5", "archive/release-manager-mvp", "benchmarks/receipt-0.11.0",
)


def test_real_tag_history_passes_the_malformed_tag_rule(repo):
    """The repository's own tags are all strict or not version tags at all."""
    for stable in REAL_STABLE_TAGS:
        version = stable[1:]
        candidates = [other for other in REAL_OTHER_TAGS if other.startswith(f"{version}-rc.")]
        for candidate in candidates:
            repo.git("tag", candidate, repo.commit(f"fix: work for {candidate}"))
        repo.commit(f"release: {version}")
        repo.git("tag", "-a", stable, "-m", f"Release {version}")
    for other in REAL_OTHER_TAGS:
        if not repo.git("tag", "--list", other):
            repo.git("tag", other)
    assert set(repo.git("tag", "--list").splitlines()) == {*REAL_STABLE_TAGS, *REAL_OTHER_TAGS}
    for message in POST_RELEASE:
        anchor = repo.commit(message)
    repo.write("release/ADOPTION.json", json.dumps({"anchor": anchor, "base": "0.11.1"}))
    cut = repo.commit("chore(release): record adoption cut")
    repo.cut(None)
    result = repo.compute()
    assert (result["base"], result["B"], result["candidate"]) == ("v0.11.1", cut, "0.11.2-rc.0")
