"""Fix availability under the adoption policy (operator ruling, SDLC 13.2)."""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from scripts import vulnerability_gate as gate
from tests.test_vulnerability_gate import DB, DIGEST, exception, report

BEFORE = dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.UTC)
AT = dt.datetime(2026, 12, 8, 0, 0, tzinfo=dt.UTC)
PY315 = {
    "ecosystem": "python", "package": "python", "line": "3.15",
    "ga_date": "2026-10-09T00:00:00+00:00",
    "ga_source": ["https://devguide.python.org/versions/",
                  "https://www.python.org/downloads/release/python-3150/"],
    "policy_wait_days": 60, "eligible_from": "2026-12-08T00:00:00+00:00",
}


def python_match(vuln_id, severity, fixes, installed="3.13.14"):
    return {"vulnerability": {"id": vuln_id, "severity": severity,
                              "fix": {"state": "fixed", "versions": fixes}},
            "artifact": {"name": "python", "version": installed, "type": "binary"},
            "relatedVulnerabilities": []}


def run(matches, *, now=BEFORE, lines=(PY315,), exceptions=()):
    found = {p: report(p, list(matches)) for p in gate.PLATFORMS}
    for item in found.values():
        item["descriptor"]["db"]["status"]["built"] = (now - dt.timedelta(hours=1)).isoformat()
    db = dict(DB, built=(now - dt.timedelta(hours=1)).isoformat())
    return gate.gate(found, gate.validate_exceptions(
        {"schema_version": 1, "exceptions": list(exceptions)}),
        image_digest=DIGEST, now=now, db_status=db,
        adoption_lines=gate.validate_adoption_lines(
            {"schema_version": 1, "lines": [copy.deepcopy(e) for e in lines]}))


def test_fix_on_the_installed_line_blocks():
    evidence = run([python_match("CVE-2026-0001", "Medium", ["3.13.15", "3.15.0rc3"])])
    assert evidence["blocked"]
    assert {f["reason"] for f in evidence["blocking"]} == {"fix available"}
    assert evidence["fix_not_yet_adoptable"] == []


@pytest.mark.parametrize(("severity", "blocked"), [
    ("Critical", True), ("High", True), ("Unknown", True), ("Medium", False), ("Low", False)])
def test_fix_only_on_a_listed_line_not_yet_eligible_is_no_fix(severity, blocked):
    evidence = run([python_match("CVE-2026-0002", severity, ["3.15.0rc3"])])
    assert evidence["blocked"] is blocked
    assert all(f["reason"] != "fix available" for f in evidence["blocking"])
    assert evidence["fix_not_yet_adoptable"] == [
        {"id": "CVE-2026-0002", "package": "python", "version": "3.13.14", "platform": p,
         "severity": severity, "eligible_from": "2026-12-08T00:00:00+00:00",
         "label": "not-yet-eligible",
         "deferred_fixes": [{"version": "3.15.0rc3", "line": "3.15",
                             "eligible_from": "2026-12-08T00:00:00+00:00"}]}
        for p in gate.PLATFORMS]
    place = evidence["blocking"] if blocked else evidence["tracked"]
    assert all(f["fix_availability"]["eligible_from"] == PY315["eligible_from"] for f in place)


def test_downgraded_high_finding_can_be_covered_by_an_exception():
    entry = exception(finding="CVE-2026-0002", package="python")
    entry["scope"]["package_version"] = "3.13.14"
    evidence = run([python_match("CVE-2026-0002", "High", ["3.15.0rc3"])], exceptions=[entry])
    assert not evidence["blocked"] and len(evidence["excepted"]) == 2


def test_the_same_finding_blocks_as_fixable_from_eligible_from_on():
    finding = python_match("CVE-2026-0002", "Medium", ["3.15.0rc3"])
    assert not run([finding], now=AT - dt.timedelta(seconds=1))["blocked"]
    evidence = run([finding], now=AT)
    assert evidence["blocked"] and evidence["fix_not_yet_adoptable"] == []
    assert {f["reason"] for f in evidence["blocking"]} == {"fix available"}
    assert evidence["adoption_lines"] == [
        {"ecosystem": "python", "package": "python", "line": "3.15",
         "eligible_from": "2026-12-08T00:00:00+00:00", "eligible": True}]


def test_a_fix_on_an_unlisted_newer_line_blocks():
    # 3.14 is not listed (already eligible), so a fix there counts.
    evidence = run([python_match("CVE-2026-0003", "Low", ["3.14.8", "3.15.0rc3"])])
    assert evidence["blocked"] and evidence["fix_not_yet_adoptable"] == []
    # No adoption lines at all: every newer line counts.
    assert run([python_match("CVE-2026-0003", "Low", ["3.15.0rc3"])], lines=())["blocked"]


@pytest.mark.parametrize(("severity", "blocked"), [("Medium", False), ("High", True)])
def test_a_fix_only_on_older_lines_is_no_fix_labelled_older_line_only(severity, blocked):
    finding = python_match("CVE-2026-0004", severity, ["3.10.22", "3.12.15"])
    evidence = run([finding])
    assert evidence["blocked"] is blocked
    downgraded = evidence["fix_not_yet_adoptable"][0]
    assert downgraded["label"] == "older-line-only"
    assert downgraded["older_line_fixes"] == [{"version": "3.10.22", "line": "3.10"},
                                              {"version": "3.12.15", "line": "3.12"}]
    assert "eligible_from" not in downgraded
    text = gate.summary(evidence)
    assert "older-line-only" in text and "3.12.15 (3.12, older)" in text
    # Next to deferred fixes the older ones are listed too, under the other label.
    mixed = run([python_match("CVE-2026-0004", "Medium", ["3.12.15", "3.15.0rc3"])])
    item = mixed["fix_not_yet_adoptable"][0]
    assert item["label"] == "not-yet-eligible"
    assert item["older_line_fixes"] == [{"version": "3.12.15", "line": "3.12"}]


@pytest.mark.parametrize("value", [
    "3.15.0rc3|3.13.16", "3.13.16|3.15.0rc3", "3.15.0rc3, 3.13.16", "3.13.16, 3.15.0rc3",
    "3.15.0rc3,3.13.16", "3.15.0rc3 3.13.16"])
def test_combined_fix_strings_cannot_hide_the_installed_line_fix(value):
    evidence = run([python_match("CVE-2026-0007", "Medium", [value])])
    assert evidence["blocked"] and evidence["fix_not_yet_adoptable"] == []


def test_combined_fix_strings_on_deferred_lines_only_stay_deferred():
    evidence = run([python_match("CVE-2026-0008", "Medium", ["3.15.0rc3| 3.15.0rc2"])])
    assert not evidence["blocked"]
    assert [d["version"] for d in evidence["fix_not_yet_adoptable"][0]["deferred_fixes"]] == [
        "3.15.0rc3", "3.15.0rc2"]


@pytest.mark.parametrize("value", [
    "3.15.0rc3|garbage", "garbage|3.15.0rc3", "3.15.0rc3|", "|3.15.0rc3", "3.15.0rc3,,",
    "", " ", "3.15.0-rc3", "3.15.0rc", "v3.15.0", "3.15.0rc3.post", "3.15.0rc3x"])
def test_unparseable_or_empty_tokens_fail_closed(value):
    evidence = run([python_match("CVE-2026-0009", "Low", ["3.15.0rc3", value])])
    assert evidence["blocked"]
    assert {f["reason"] for f in evidence["blocking"]} == {"fix available"}


@pytest.mark.parametrize("versions", [[], ["unparsable"], ["3"]])
def test_unjudgeable_fix_versions_fail_closed(versions):
    assert run([python_match("CVE-2026-0005", "Low", versions)])["blocked"]


def test_unlisted_packages_keep_the_plain_rule():
    found = python_match("CVE-2026-0006", "Low", ["3.15.0rc3"])
    found["artifact"]["name"] = "libpython3.13"
    assert run([found])["blocked"]


def test_exceptions_still_cannot_cover_a_fixable_finding():
    entry = exception(finding="CVE-2026-0001", package="python")
    entry["scope"]["package_version"] = "3.13.14"
    evidence = run([python_match("CVE-2026-0001", "High", ["3.13.15"])], exceptions=[entry])
    assert evidence["blocked"] and evidence["excepted"] == []
    after = run([python_match("CVE-2026-0001", "High", ["3.15.0rc3"])], now=AT,
                exceptions=[entry | {"expiry": "2026-12-31", "review_date": "2026-12-01"}])
    assert after["blocked"] and after["excepted"] == []


@pytest.mark.parametrize("change", [
    {"eligible_from": "2026-12-07T00:00:00+00:00"},
    {"eligible_from": "2026-12-08"},
    {"ga_date": "2026-10-09"},
    {"ga_date": "2026-10-09T00:00:00"},
    {"policy_wait_days": 30},
    {"policy_wait_days": "60"},
    {"ecosystem": "ruby"},
    {"line": "3"}, {"line": "3.15.0"}, {"line": "3.x"},
    {"ga_source": []}, {"ga_source": ["http://python.org"]}, {"ga_source": "x"},
    {"package": ""},
    {"extra": 1},
])
def test_malformed_adoption_lines_fail_closed(change):
    with pytest.raises(ValueError):
        gate.validate_adoption_lines({"schema_version": 1, "lines": [PY315 | change]})


def test_adoption_lines_file_shape_and_duplicates():
    for payload in ({"schema_version": 2, "lines": []}, {"lines": []}, [],
                    {"schema_version": 1, "lines": [PY315, dict(PY315)]}):
        with pytest.raises(ValueError):
            gate.validate_adoption_lines(payload)
    without_source = dict(PY315)
    del without_source["ga_source"]
    with pytest.raises(ValueError):
        gate.validate_adoption_lines({"schema_version": 1, "lines": [without_source]})


def test_cli_refuses_a_malformed_adoption_lines_file(tmp_path):
    bad = tmp_path / "adoption-lines.json"
    bad.write_text(json.dumps({"schema_version": 1, "lines": [PY315 | {"policy_wait_days": 1}]}))
    assert gate.main(["validate", "--exceptions", "release/exceptions.json",
                      "--line-eligibility", str(bad)]) == 1
    bad.write_text("{not json")
    assert gate.main(["validate", "--exceptions", "release/exceptions.json",
                      "--line-eligibility", str(bad)]) == 1


def test_committed_adoption_lines_file_is_valid_and_lists_python_3_15():
    lines = gate.validate_adoption_lines(
        json.loads(Path("release/adoption-lines.json").read_text()))
    assert lines == [PY315]


def test_release_record_names_the_adoption_lines_blob_at_s(tmp_path):
    import subprocess

    from scripts import release_record
    from scripts.sdlc_version import Git
    from tests.test_release_record import Promotion
    promotion = Promotion(tmp_path)
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "exceptions.json").write_text(
        '{"schema_version": 1, "exceptions": []}\n')
    only_exceptions = promotion.commit("chore: add the exception file")
    assert "adoption_lines" not in release_record.vulnerability_gate_reference(
        Git(tmp_path), only_exceptions)
    (tmp_path / "release" / "adoption-lines.json").write_text(
        json.dumps({"schema_version": 1, "lines": [PY315]}))
    both = promotion.commit("chore: add the adoption lines")
    blob = subprocess.check_output(
        ["git", "rev-parse", f"{both}:release/adoption-lines.json"], cwd=tmp_path,
        text=True).strip()
    reference = release_record.vulnerability_gate_reference(Git(tmp_path), both)
    assert reference["adoption_lines"] == "release/adoption-lines.json"
    assert reference["adoption_lines_blob"] == blob
