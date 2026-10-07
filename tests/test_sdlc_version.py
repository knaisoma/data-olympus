"""Git-backed contract tests for STD-U-821 and the W6 adoption ruling."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from scripts import sdlc_version as engine


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
        return engine.compute_version(**(options | kwargs))


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


@pytest.mark.parametrize(("flags", "state"), [
    ([], None),
    (["--adoption-dry-run"], "unratified"),
    (["--adoption-ratified", "2020-01-01:PR#123"], "ratified"),
])
def test_adoption_cli_ratification(repo, flags, state):
    adoption(repo)
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


@pytest.mark.parametrize("line", [
    "Ratification: 2020-01-01", "Ratification: not yet recorded",
    "Ratification: 2020-01-02", "prefix Ratification: 2020-01-01",
    "Ratification: 2020-01-01 suffix", "",
])
def test_adoption_cli_standard_ratification(repo, line):
    adoption(repo)
    standard = repo.path / "standard.md"
    standard.write_text(f"# Standard\n{line}\n")
    result = subprocess.run(
        [sys.executable, str(Path(engine.__file__)), "--main", "main", "--branch", "release/new",
         "--adoption-ratified", "2020-01-01:PR#123", "--standard-file", str(standard)],
        cwd=repo.path, capture_output=True, text=True,
    )
    if line == "Ratification: 2020-01-01":
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["adoption"] == "ratified"
    else:
        assert result.returncode == 1
        assert "adoption_unratified" in result.stderr


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
