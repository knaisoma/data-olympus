"""Offline replica of the Task 9 adoption cut across every engine caller.

The fixture mirrors the real repository: an annotated v0.11.0 tag, main one
commit ahead of it (anchor A), the record commit C that adds only
release/ADOPTION.json, release/new created at C, then the R4 placeholder and
the retirement of the record. Stage 1 (rc_decide) reads the ratification from
main's blobs; stage 2 and promotion read it from their own trusted checkout,
which in these tests is this repository.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import adoption_ratification as ratification
from scripts import rc_decide
from scripts import rc_verify_and_publish as stage
from scripts import release_record as release
from scripts.sdlc_version import VersionError, compute_version
from tests.test_rc_verify_and_publish import write_artifacts

ROOT = Path(__file__).resolve().parents[1]
MODULE_SOURCE = (ROOT / ratification.MODULE).read_text(encoding="utf-8")
STANDARD_TEXT = (ROOT / ratification.STANDARD_FILE).read_text(encoding="utf-8")


def module_with(ratified: str) -> str:
    """The trusted module with a different RATIFIED literal."""
    source, count = re.subn(r'^RATIFIED = ".*"$', f"RATIFIED = {ratified!r}",
                            MODULE_SOURCE, flags=re.MULTILINE)
    assert count == 1
    return source


class Cut:
    """Git fixture for the adoption cut, built with plain commits."""

    def __init__(self, path: Path, *, module: str = MODULE_SOURCE,
                 standard: str | None = STANDARD_TEXT):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")
        self.write("pyproject.toml", '[project]\nname = "data-olympus"\nversion = "0.11.0"\n')
        self.write(ratification.MODULE, module)
        if standard is not None:
            self.write(ratification.STANDARD_FILE, standard)
        self.commit("chore(release): 0.11.0")
        self.git("tag", "-a", "v0.11.0", "-m", "Release 0.11.0")
        self.write("src/hooks.py", "SANITIZED = True\n")
        # main is ahead of the tag, as with PR #321 and the W6 tooling.
        self.anchor = self.commit("fix(hooks): sanitize deny reasons")
        self.write("release/ADOPTION.json",
                   json.dumps({"anchor": self.anchor, "base": "0.11.0"}, indent=2) + "\n")
        self.c = self.commit("chore(release): record adoption cut")
        self.git("branch", "release/new", self.c)
        self.git("checkout", "-q", "release/new")

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.path, text=True).strip()

    def write(self, name: str, text: str) -> None:
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def placeholder(self) -> str:
        """R4: the first commit on release/new after the cut."""
        self.write("pyproject.toml",
                   '[project]\nname = "data-olympus"\nversion = "0.0.0+unreleased"\n')
        return self.commit("chore(release): use the unreleased version placeholder")

    def retire(self) -> str:
        """R1 (c): the second commit deletes the record."""
        self.git("rm", "-q", "release/ADOPTION.json")
        return self.commit("chore(release): retire adoption record")

    def preflight(self, *, branch="release/new", dry_run=False, event="push",
                  trusted: Path | None = None) -> rc_decide.Admission:
        return rc_decide.preflight(
            cwd=self.path, head="HEAD", main="refs/heads/main", branch=branch,
            branch_ref=f"refs/heads/{branch}", dry_run=dry_run, event=event,
            trusted_dir=trusted or self.path.parent / "trusted",
        )

    def engine(self, admission: rc_decide.Admission) -> dict:
        """Run the engine CLI exactly as rc-build.yml does after preflight."""
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/sdlc_version.py"), "--head", "HEAD",
             "--main", "refs/heads/main", "--branch", admission.engine_branch,
             *admission.engine_args()],
            cwd=self.path, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def trusted(self, **kwargs) -> dict:
        """The stage-two and promotion computation: trusted checkout constants."""
        return compute_version(cwd=self.path, head="HEAD", main="refs/heads/main",
                               branch="release/new",
                               **ratification.engine_kwargs(ROOT), **kwargs)


@pytest.fixture
def cut(tmp_path):
    return Cut(tmp_path / "repo")


def test_fixture_matches_the_real_tag_shape(cut):
    assert cut.git("cat-file", "-t", "v0.11.0") == "tag"
    tagged = cut.git("rev-parse", "v0.11.0^{commit}")
    assert cut.git("rev-list", "--count", f"{tagged}..{cut.anchor}") == "1"
    assert cut.git("diff", "--name-status", cut.anchor, cut.c) == "A\trelease/ADOPTION.json"
    assert cut.git("rev-parse", "release/new") == cut.c


def test_engine_identities_through_the_first_cycle(cut):
    at_cut = cut.trusted()
    assert (at_cut["candidate"], at_cut["N"], at_cut["promotable"]) == ("0.11.1-rc.0", 0, False)
    assert at_cut["B"] == cut.c
    assert (at_cut["adoption"], at_cut["adoption_retired"]) == ("ratified", False)
    cut.placeholder()
    placeholder = cut.trusted()
    assert (placeholder["candidate"], placeholder["promotable"]) == ("0.11.1-rc.1", False)
    cut.retire()
    retired = cut.trusted()
    assert retired["candidate"] == "0.11.1-rc.2"
    assert retired["pypi_version"] == "0.11.1rc2"
    assert (retired["B"], retired["M"], retired["N"]) == (cut.c, cut.c, 2)
    assert retired["promotable"] is True
    assert retired["adoption_retired"] is True


def test_stage_one_admits_the_cut_with_main_ratification(cut, tmp_path):
    admission = cut.preflight(trusted=tmp_path / "trusted")
    assert admission.engine_branch == "release/new"
    assert admission.adoption == "ratified"
    assert admission.ratified == ratification.RATIFIED
    assert admission.standard_file == (tmp_path / "trusted").resolve() / "adoption-standard.md"
    assert admission.standard_file.read_text(encoding="utf-8") == STANDARD_TEXT
    assert admission.engine_args() == [
        "--adoption-ratified", ratification.RATIFIED,
        "--standard-file", str(admission.standard_file),
    ]
    version = cut.engine(admission)
    assert (version["candidate"], version["N"], version["adoption"]) == ("0.11.1-rc.0", 0,
                                                                         "ratified")
    decision = rc_decide.decide(version, dry_run=False)
    # R2: the cut build builds and verifies, publishes nothing, is not promotable.
    assert (decision["build"], decision["publish"], decision["promotable"]) == (
        True, False, False)


def test_stage_one_env_contract(cut, tmp_path):
    lines = cut.preflight(trusted=tmp_path / "t").env().splitlines()
    assert lines == [
        "ENGINE_BRANCH=release/new", "ADOPTION_MODE=ratified",
        f"ADOPTION_RATIFIED={ratification.RATIFIED}",
        f"ADOPTION_STANDARD_FILE={(tmp_path / 't').resolve() / 'adoption-standard.md'}",
    ]


@pytest.mark.parametrize("module,standard", [
    (module_with("2026-10-06:knaisoma/company-knowledge@785bb77"), STANDARD_TEXT),
    (module_with("2099-01-01:knaisoma/company-knowledge@785bb77"), STANDARD_TEXT),
    (module_with("2026-10-07:"), STANDARD_TEXT),
    (module_with("knaisoma/company-knowledge@785bb77"), STANDARD_TEXT),
    (MODULE_SOURCE, None),
    (MODULE_SOURCE, STANDARD_TEXT.replace("Ratification: 2026-10-07", "Ratification: TBD")),
], ids=["wrong-date", "future-date", "empty-ref", "missing-date", "missing-standard",
        "unratified-standard"])
def test_stage_one_refuses_bad_main_ratification(tmp_path, module, standard):
    cut = Cut(tmp_path / "repo", module=module, standard=standard)
    with pytest.raises(VersionError, match="adoption_unratified"):
        cut.preflight()


def test_stage_one_refuses_a_stale_standard_copy(cut, tmp_path):
    """A leftover trusted file from an earlier run never satisfies a missing blob."""
    trusted = tmp_path / "trusted"
    cut.preflight(trusted=trusted)
    assert (trusted / "adoption-standard.md").exists()
    missing = Cut(tmp_path / "missing", standard=None)
    with pytest.raises(VersionError, match="adoption_unratified"):
        missing.preflight(trusted=trusted)
    assert not (trusted / "adoption-standard.md").exists()


@pytest.mark.parametrize("source", [
    MODULE_SOURCE.replace('RATIFIED = "', 'RATIFIED = "x" + "', 1),
    MODULE_SOURCE + '\nRATIFIED = "2026-10-07:other"\n',
    MODULE_SOURCE.replace('STANDARD_FILE = "', 'STANDARD_FILE = "../', 1),
    MODULE_SOURCE.replace('RATIFIED = "', 'RATIFIED = "2026-10-07:a\\nINJECTED=1 ', 1),
    "RATIFIED = (",
    MODULE_SOURCE + '\nRATIFIED, OTHER = "2026-10-07:other", 1\n',
    MODULE_SOURCE + '\nfor RATIFIED in ["2026-10-07:other"]:\n    pass\n',
    MODULE_SOURCE + "\nimport os as RATIFIED\n",
    MODULE_SOURCE + "\nfrom os import sep as STANDARD_FILE\n",
    MODULE_SOURCE + '\nif True:\n    RATIFIED = "2026-10-07:other"\n',
    MODULE_SOURCE + '\nglobals()["RATIFIED"] = "2026-10-07:other"\n',
    MODULE_SOURCE + '\nimport sys\nsys.modules[__name__].RATIFIED = "2026-10-07:other"\n',
    MODULE_SOURCE + '\n(RATIFIED := "2026-10-07:other")\n',
    MODULE_SOURCE + '\ndef f(RATIFIED=1):\n    pass\n',
    MODULE_SOURCE + '\ndel RATIFIED\n',
    MODULE_SOURCE + '\nRATIFIED += "x"\n',
    MODULE_SOURCE + '\nRATIFIED: str = "2026-10-07:other"\n',
    MODULE_SOURCE + '\nexec("RATIFIED = 1")\n',
    MODULE_SOURCE + '\nvars()["STANDARD_FILE"] = "x"\n',
], ids=["computed", "rebound", "path-escape", "newline", "syntax", "tuple-unpack",
        "for-target", "import-as", "from-import-as", "nested-if", "globals", "module-attr",
        "walrus", "argument", "delete", "augmented", "annotated", "exec", "vars"])
def test_stage_one_refuses_untrusted_module_shapes(tmp_path, source):
    cut = Cut(tmp_path / "repo", module=source)
    with pytest.raises(ValueError, match="trusted adoption ratification"):
        cut.preflight()


def test_real_module_parses_like_its_import():
    tree = ast.parse(MODULE_SOURCE)
    assert rc_decide._literal(tree, "RATIFIED") == ratification.RATIFIED
    assert rc_decide._literal(tree, "STANDARD_FILE") == ratification.STANDARD_FILE


def test_stage_one_ignores_ratification_carried_by_h(cut):
    """H is data: a release/new commit cannot change the ratification used."""
    cut.write(ratification.MODULE, module_with("2026-10-06:forged"))
    cut.write(ratification.STANDARD_FILE, "forged\n")
    cut.commit("chore(release): forge ratification")
    admission = cut.preflight()
    assert admission.ratified == ratification.RATIFIED
    assert admission.standard_file.read_text(encoding="utf-8") == STANDARD_TEXT


def test_stage_one_uses_main_even_when_h_is_correct(tmp_path):
    cut = Cut(tmp_path / "repo", module=module_with("2026-10-06:wrong"))
    cut.write(ratification.MODULE, MODULE_SOURCE)
    cut.commit("chore(release): restore ratification on the branch only")
    with pytest.raises(VersionError, match="adoption_unratified"):
        cut.preflight()


@pytest.mark.parametrize("branch", ["release/new", "feature/cut-check"])
def test_dry_run_dispatch_is_the_only_adoption_dry_run(cut, branch):
    if branch != "release/new":
        cut.git("checkout", "-q", "-b", branch)
    admission = cut.preflight(branch=branch, dry_run=True, event="workflow_dispatch")
    assert admission.adoption == "dry-run"
    assert admission.engine_args() == ["--adoption-dry-run"]
    version = cut.engine(admission)
    assert version["adoption"] == "unratified"
    assert rc_decide.decide(version, dry_run=True)["promotable"] is False


def test_push_and_real_dispatch_never_use_dry_run(cut):
    assert cut.preflight(dry_run=True, event="push").adoption == "ratified"
    assert cut.preflight(dry_run=False, event="workflow_dispatch").adoption == "ratified"


def test_non_adoption_cut_reads_nothing_from_main(tmp_path):
    from tests.test_sdlc_version import Repo

    (tmp_path / "plain").mkdir()
    repo = Repo(tmp_path / "plain")
    repo.cut()
    admission = rc_decide.preflight(
        cwd=repo.path, head="HEAD", main="refs/heads/main", branch="release/new",
        branch_ref="refs/heads/release/new", dry_run=False, event="push",
        trusted_dir=tmp_path / "trusted",
    )
    assert (admission.adoption, admission.engine_args()) == ("none", [])
    assert not (tmp_path / "trusted").exists()


def test_stage_one_after_both_commits_is_promotable(cut):
    cut.placeholder()
    cut.retire()
    version = cut.engine(cut.preflight())
    assert (version["candidate"], version["promotable"]) == ("0.11.1-rc.2", True)
    assert rc_decide.decide(version, dry_run=False)["promotable"] is True


def stage_two(cut: Cut, tmp_path: Path) -> stage.Verified:
    version = cut.engine(cut.preflight())
    provenance, event = write_artifacts(tmp_path / "artifacts", version)
    provenance["promotable"] = version["promotable"]
    (tmp_path / "artifacts" / stage.PROVENANCE).write_text(json.dumps(provenance))
    return stage.verify(tmp_path / "artifacts", cut.path, event, main="refs/heads/main",
                        branch_ref="refs/heads/release/new")


def test_stage_two_recompute_agrees_with_stage_one(cut, tmp_path):
    cut.placeholder()
    cut.retire()
    verified = stage_two(cut, tmp_path)
    assert (verified.version, verified.python_version) == ("0.11.1-rc.2", "0.11.1rc2")
    assert (verified.base, verified.main) == (cut.c, cut.c)


@pytest.mark.parametrize("commits,error", [
    (0, "N=0 never publishes"), (1, "engine refuses promotion"),
])
def test_stage_two_refuses_before_retirement(cut, tmp_path, commits, error):
    if commits:
        cut.placeholder()
    with pytest.raises(ValueError, match=error):
        stage_two(cut, tmp_path)


def test_stage_two_takes_ratification_from_its_trusted_checkout(cut, tmp_path, monkeypatch):
    cut.placeholder()
    cut.retire()
    monkeypatch.setattr(ratification, "RATIFIED", "2026-10-06:knaisoma/company-knowledge@785bb77")
    with pytest.raises(VersionError, match="adoption_unratified"):
        stage_two(cut, tmp_path)


def promote(cut: Cut, version: dict) -> dict:
    """Squash H onto B = C, as the release PR does, and prove it."""
    h = version["H"]
    cut.git("tag", version["candidate"], h)
    provenance = {
        "source_sha": h, "H": h, "B": version["B"], "M": version["M"], "N": version["N"],
        "candidate_tag": version["candidate"], "python_version": version["pypi_version"],
        "image_digest": "sha256:" + "a" * 64, "oci_archive_sha256": "b" * 64,
        "promotable": True, "dry_run": False,
        "candidate": {
            "version": version["pypi_version"], "source_sha": h,
            "source_tree_sha256": "c" * 64, "lock_sha256": "d" * 64,
            "wheel_sha256": "e" * 64, "sdist_sha256": "f" * 64,
            "wheel": f"data_olympus-{version['pypi_version']}-py3-none-any.whl",
            "sdist": f"data_olympus-{version['pypi_version']}.tar.gz",
        },
    }
    notes = release.generate_notes(cwd=cut.path, version=version)
    squash = cut.git("commit-tree", f"{h}^{{tree}}", "-p", cut.c, "-m",
                     f"release: {version['target']}\n\n{notes}")
    cut.git("update-ref", "refs/heads/main", squash)
    return release.prove_release(cwd=cut.path, squash=squash, head=h, main="refs/heads/main",
                                 candidate_tag=version["candidate"], provenance=provenance)


def test_promotion_recompute_agrees_after_retirement(cut):
    cut.placeholder()
    cut.retire()
    version = cut.engine(cut.preflight())
    record = promote(cut, version)
    assert (record["tag"], record["target"], record["B"]) == ("v0.11.1", "0.11.1", cut.c)
    assert record["candidate_tag"] == "0.11.1-rc.2"
    assert "fix(hooks): sanitize deny reasons" in record["notes"]


def test_promotion_fails_while_the_record_exists_at_h(cut):
    cut.placeholder()
    version = cut.trusted(adoption_dry_run=False)
    version = version | {"promotable": True}
    with pytest.raises(ValueError, match="ADOPTION.json must be absent at H"):
        promote(cut, version)


def forge_ratification_at_h(cut: Cut) -> None:
    """A release/new commit replacing the pinned values in H (and so in S)."""
    cut.write(ratification.MODULE, module_with("2026-10-06:forged"))
    cut.write(ratification.STANDARD_FILE, "forged\n")
    cut.commit("chore(release): forge ratification")


def test_promotion_ignores_ratification_carried_by_h(cut):
    """Promotion's checkout is S, whose tree is H: it must read M's blobs."""
    cut.placeholder()
    cut.retire()
    forge_ratification_at_h(cut)
    version = cut.trusted()
    assert version["candidate"] == "0.11.1-rc.3"
    assert promote(cut, version)["tag"] == "v0.11.1"


def test_promotion_ignores_its_own_checkout_constants(cut, monkeypatch):
    cut.placeholder()
    cut.retire()
    version = cut.engine(cut.preflight())
    monkeypatch.setattr(ratification, "RATIFIED", "2026-10-06:forged")
    monkeypatch.setattr(ratification, "STANDARD_FILE", "docs/releases/missing.md")
    assert promote(cut, version)["candidate_tag"] == "0.11.1-rc.2"


@pytest.mark.parametrize("module,standard", [
    (module_with("2026-10-06:knaisoma/company-knowledge@785bb77"), STANDARD_TEXT),
    (MODULE_SOURCE, None),
], ids=["wrong-date", "missing-standard"])
def test_promotion_refuses_bad_ratification_on_m(tmp_path, module, standard):
    cut = Cut(tmp_path / "repo", module=module, standard=standard)
    cut.placeholder()
    cut.retire()
    version = cut.trusted()  # what a correct stage 2 would have recorded
    with pytest.raises(VersionError, match="adoption_unratified"):
        promote(cut, version)


def test_release_notes_helper_accepts_the_ratified_cut(cut, capsys, monkeypatch):
    cut.placeholder()
    cut.retire()
    monkeypatch.chdir(cut.path)
    assert release.main(["notes", "--head", "HEAD", "--main", "refs/heads/main"]) == 0
    assert capsys.readouterr().out.startswith("# Release 0.11.1\n")


def test_release_notes_helper_ignores_ratification_carried_by_h(cut, capsys, monkeypatch):
    cut.placeholder()
    cut.retire()
    forge_ratification_at_h(cut)
    monkeypatch.chdir(cut.path)
    assert release.main(["notes", "--head", "HEAD", "--main", "refs/heads/main"]) == 0
    assert capsys.readouterr().out.startswith("# Release 0.11.1\n")
