"""Pure logic for the SemVer release computation (STD-U-810 pre-1.0 mapping)."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

from scripts.compute_release import bump_for, classify, next_rc_number, next_rc_tag, next_version

_REPO = pathlib.Path(__file__).resolve().parents[1]


def test_classify_plain_feat() -> None:
    assert classify("feat(search): add synonyms", "") == ("feat", False)


def test_classify_breaking_bang() -> None:
    assert classify("feat(api)!: drop v0 endpoint", "") == ("feat", True)


def test_classify_breaking_footer() -> None:
    assert classify(
        "refactor: rework store", "BREAKING CHANGE: migration required"
    ) == ("refactor", True)


def test_classify_non_conventional_returns_none() -> None:
    assert classify("Merge pull request #55 from x", "") == (None, False)


def test_feat_bumps_minor_pre_1_0() -> None:
    # data-olympus adopts the features-as-minor mapping (STD-U-810 §3.1.1 opt-in).
    bump, changes = bump_for([("feat: x", "")], functional_changed=False)
    assert bump == "minor"
    assert changes["features"] == ["feat: x"]


def test_fix_bumps_patch() -> None:
    bump, _ = bump_for([("fix: y", "")], functional_changed=True)
    assert bump == "patch"


def test_breaking_bumps_minor_and_wins() -> None:
    commits = [("feat: a", ""), ("fix: b", ""), ("feat!: c", "")]
    bump, changes = bump_for(commits, functional_changed=True)
    assert bump == "minor"
    assert changes["breaking"] == ["feat!: c"]


def test_breaking_bang_alone_bumps_minor_not_major_pre_1_0() -> None:
    # Isolated from feat->minor: a breaking change with NO feat commit must still
    # map to minor (never major) pre-1.0. Guards the breaking branch on its own,
    # so a regression there cannot hide behind a feature commit that is also minor.
    bump, changes = bump_for([("refactor!: rework store", "")], functional_changed=False)
    assert bump == "minor"
    assert changes["breaking"] == ["refactor!: rework store"]


def test_breaking_footer_alone_bumps_minor_not_major_pre_1_0() -> None:
    bump, changes = bump_for(
        [("refactor: rework store", "BREAKING CHANGE: migration required")],
        functional_changed=False,
    )
    assert bump == "minor"
    assert changes["breaking"] == ["refactor: rework store"]


def test_chore_only_no_functional_change_is_none() -> None:
    bump, _ = bump_for([("chore: deps", ""), ("ci: bump", "")], functional_changed=False)
    assert bump == "none"


def test_chore_only_with_functional_change_floors_to_patch() -> None:
    bump, _ = bump_for([("chore: touch src", "")], functional_changed=True)
    assert bump == "patch"


def test_non_conventional_commits_ignored() -> None:
    bump, _ = bump_for([("Merge branch main", "")], functional_changed=False)
    assert bump == "none"


def test_next_version_patch() -> None:
    assert next_version("0.1.1", "patch") == "0.1.2"


def test_next_version_minor_resets_patch() -> None:
    assert next_version("0.1.5", "minor") == "0.2.0"


def test_next_version_none_is_unchanged() -> None:
    assert next_version("0.1.1", "none") == "0.1.1"


def test_script_runs_as_direct_path() -> None:
    r = subprocess.run(
        [sys.executable, "scripts/compute_release.py"],
        cwd=_REPO, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout).get("bump") in {"none", "patch", "minor"}


def test_next_rc_number_starts_at_1_when_none_exist() -> None:
    assert next_rc_number("0.5.0", []) == 1


def test_next_rc_number_is_one_past_highest_for_that_base() -> None:
    assert next_rc_number("0.5.0", ["0.5.0-rc.1", "0.5.0-rc.2"]) == 3


def test_next_rc_number_ignores_other_bases_and_v_prefix() -> None:
    # 0.4.0 rc is a different base; the v-prefixed 0.5.0-rc.1 counts.
    assert next_rc_number("0.5.0", ["0.4.0-rc.9", "v0.5.0-rc.1"]) == 2


def test_next_rc_number_ignores_non_rc_and_malformed() -> None:
    assert next_rc_number("0.5.0", ["0.5.0", "latest", "0.5.0-rc.x", "junk"]) == 1


def test_next_rc_tag_composes_base_and_number() -> None:
    assert next_rc_tag("0.5.0", ["0.5.0-rc.1"]) == "0.5.0-rc.2"


def test_current_rc_tag_empty_when_none_exist() -> None:
    from scripts.compute_release import current_rc_tag
    assert current_rc_tag("0.5.0", ["0.4.0-rc.9", "v0.5.0"]) == ""


def test_current_rc_tag_returns_highest_for_base() -> None:
    from scripts.compute_release import current_rc_tag
    assert current_rc_tag("0.5.0", ["0.5.0-rc.1", "v0.5.0-rc.3", "0.5.0-rc.2"]) == "0.5.0-rc.3"
