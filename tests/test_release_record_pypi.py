"""Stable PyPI verification and the resume_pypi external-state proof.

The fixtures reproduce the first live promotion (run 37942200995): the PyPI
publish action writes `<file>.publish.attestation` next to each uploaded file,
so the dist directory holds four entries while PyPI lists two.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import release_record as release

VERSION = "0.11.1"
WHEEL = "data_olympus-0.11.1-py3-none-any.whl"
SDIST = "data_olympus-0.11.1.tar.gz"
CANDIDATE = "sha256:628d3a861edd737e3129e908e995d260bb602aa387be22ac7ed19d4bfbd35d30"
PREVIOUS = "sha256:95beaca8" + "0" * 56


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


FILES = {WHEEL: b"stable wheel bytes", SDIST: b"stable sdist bytes"}
HASHES = {name: sha(data) for name, data in FILES.items()}


def pypi(files=None, *, version=VERSION, yanked=False):
    """The shape of https://pypi.org/pypi/data-olympus/<version>/json."""
    files = HASHES if files is None else files
    return {"info": {"version": version}, "urls": [
        {"filename": name, "digests": {"sha256": digest, "md5": "0" * 32},
         "yanked": yanked, "packagetype": "bdist_wheel" if name.endswith(".whl") else "sdist"}
        for name, digest in files.items()
    ]}


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    root.mkdir()
    for name, data in FILES.items():
        (root / name).write_bytes(data)
        (root / (name + ".publish.attestation")).write_text('{"version": 1}')
    return root


PROVENANCE = {"stable": {"version": VERSION, "wheel": WHEEL, "sdist": SDIST,
                         "wheel_sha256": HASHES[WHEEL], "sdist_sha256": HASHES[SDIST]}}


def verify(dist, *, payloads=None, attestations=True, provenance=None):
    payloads = [pypi()] if payloads is None else payloads
    reads, sleeps = [], []

    def fetch(version):
        reads.append(version)
        value = payloads[min(len(reads), len(payloads)) - 1]
        if isinstance(value, Exception):
            raise value
        return value

    release.verify_published(dist=dist, version=VERSION, attestations=attestations,
                             stable_provenance=PROVENANCE if provenance is None else provenance,
                             fetch=fetch, sleep=sleeps.append)
    return reads, sleeps


def test_the_live_failure_shape_verifies(dist):
    """Wheel, sdist and both attestations locally; PyPI lists only the two files."""
    assert len(list(dist.iterdir())) == 4
    assert len(pypi()["urls"]) == 2
    reads, sleeps = verify(dist)
    assert reads == [VERSION]
    assert sleeps == []


def test_the_old_verifier_rule_would_refuse_the_live_shape(dist):
    """Regression: comparing every local file with PyPI can never succeed."""
    every_local = {path.name: sha(path.read_bytes()) for path in dist.iterdir()}
    assert every_local != release.pypi_files(pypi(), version=VERSION)


@pytest.mark.parametrize("name", ["extra.whl", "data_olympus-0.11.1-py3-none-any.whl.sig",
                                  "release-provenance.json", ".hidden"])
def test_an_unexpected_local_file_fails_closed(dist, name):
    (dist / name).write_text("x")
    with pytest.raises(ValueError, match="unexpected files"):
        verify(dist)


@pytest.mark.parametrize("name", [WHEEL, SDIST])
def test_a_missing_attestation_fails_when_the_upload_ran(dist, name):
    (dist / (name + ".publish.attestation")).unlink()
    with pytest.raises(ValueError, match="missing"):
        verify(dist)


def test_an_empty_attestation_fails(dist):
    (dist / (WHEEL + ".publish.attestation")).write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        verify(dist)


def test_attestations_must_be_absent_when_the_upload_was_skipped(dist):
    with pytest.raises(ValueError, match="unexpected files"):
        verify(dist, attestations=False)
    for name in FILES:
        (dist / (name + ".publish.attestation")).unlink()
    assert verify(dist, attestations=False)[0] == [VERSION]


def test_a_local_file_that_differs_from_the_stable_provenance_fails(dist):
    (dist / WHEEL).write_bytes(b"other bytes")
    with pytest.raises(ValueError, match="differs from the stable provenance"):
        verify(dist)


@pytest.mark.parametrize("remote", [
    {WHEEL: "0" * 64, SDIST: HASHES[SDIST]},
    {WHEEL: HASHES[WHEEL]},
    {**HASHES, "data_olympus-0.11.1-cp313-cp313-manylinux_2_17_x86_64.whl": "1" * 64},
    {**HASHES, WHEEL + ".publish.attestation": "2" * 64},
])
def test_wrong_missing_or_extra_remote_files_fail_after_the_bounded_retries(dist, remote):
    with pytest.raises(ValueError, match="do not match the build of S"):
        verify(dist, payloads=[pypi(remote)])


def test_pypi_listing_lag_is_awaited(dist):
    reads, sleeps = verify(dist, payloads=[None, OSError("timeout"), pypi({WHEEL: HASHES[WHEEL]}),
                                           pypi()])
    assert len(reads) == 4
    assert sleeps == [release.PYPI_DELAY] * 3


def test_pypi_never_listing_the_release_fails_closed(dist):
    with pytest.raises(ValueError, match="does not list"):
        reads, _ = verify(dist, payloads=[None])
    with pytest.raises(OSError):
        verify(dist, payloads=[OSError("down")])


def test_local_faults_are_not_retried(dist):
    (dist / "extra").write_text("x")
    calls = []
    with pytest.raises(ValueError, match="unexpected files"):
        release.verify_published(dist=dist, version=VERSION, attestations=True,
                                 stable_provenance=PROVENANCE,
                                 fetch=lambda version: calls.append(version),
                                 sleep=calls.append)
    assert calls == []


@pytest.mark.parametrize("change", [
    {"wheel": "data_olympus-0.11.2-py3-none-any.whl"}, {"sdist": "../" + SDIST},
    {"wheel": SDIST}, {"version": "0.11.2"}, {"sdist_sha256": "bad"},
])
def test_stable_provenance_names_and_hashes_are_validated(dist, change):
    provenance = {"stable": PROVENANCE["stable"] | change}
    with pytest.raises(ValueError):
        verify(dist, provenance=provenance)


@pytest.mark.parametrize(("payload", "error"), [
    (pypi(version="0.11.2"), "version differs"),
    (pypi(yanked=True), "yanked"),
    ({"info": {"version": VERSION}, "urls": [
        {key: value for key, value in entry.items() if key != "yanked"}
        for entry in pypi()["urls"]]}, "no yanked state"),
    ({"info": {"version": VERSION}, "urls": [{"filename": WHEEL}]}, "unreadable"),
    ({"urls": []}, "unreadable"),
    ({"info": {"version": VERSION}, "urls": pypi()["urls"] * 2}, "twice"),
])
def test_pypi_listing_is_parsed_strictly(payload, error):
    with pytest.raises(ValueError, match=error):
        release.pypi_files(payload, version=VERSION)


def test_verify_cli_reports_and_refuses(dist, tmp_path, monkeypatch, capsys):
    provenance = tmp_path / "release-provenance.json"
    provenance.write_text(json.dumps(PROVENANCE))
    monkeypatch.setattr(release, "fetch_pypi", lambda _version: pypi())
    argv = ["verify-pypi", "--dist", str(dist), "--stable-provenance", str(provenance),
            "--version", VERSION]
    assert release.main([*argv, "--attestations", "required"]) == 0
    assert release.main([*argv, "--attestations", "absent"]) == 1
    assert "unexpected files" in capsys.readouterr().err
    script = Path(release.__file__).resolve()
    for value in ("True", "yes", "", "true"):
        result = subprocess.run([sys.executable, str(script), *argv, "--attestations", value],
                                capture_output=True, text=True)
        assert result.returncode == 2, value


# resume_pypi: PyPI already holds the release, nothing after it is published.

def state(**changes):
    facts = {
        "version": VERSION, "image_digest": CANDIDATE, "pypi": pypi(), "ghcr_tag": False,
        "channels": {"stable": PREVIOUS, "latest": PREVIOUS}, "release": False,
        "release_tags": ["0.11.1-rc.5", "v0.11.0"], "git_tag": False,
    } | changes
    return release.validate_resume_state(**facts)


def test_resume_state_accepts_the_live_state():
    assert state() == {"wheel": WHEEL, "sdist": SDIST}
    # A channel that was never created is not on the candidate either.
    assert state(channels={"stable": "", "latest": PREVIOUS})["wheel"] == WHEEL


@pytest.mark.parametrize(("changes", "error"), [
    ({"pypi": None}, "resume_pypi is impossible"),
    ({"pypi": pypi({WHEEL: HASHES[WHEEL]})}, "lacks the stable sdist"),
    ({"pypi": pypi({SDIST: HASHES[SDIST]})}, "lacks the stable wheel"),
    ({"pypi": pypi({**HASHES, "data_olympus-0.11.1-py3-none-win_amd64.whl": "3" * 64})},
     "unexpected file"),
    ({"pypi": pypi({**HASHES, "data_olympus-0.11.1.zip": "3" * 64})}, "unexpected file"),
    ({"pypi": pypi(yanked=True)}, "yanked"),
    ({"git_tag": True}, "Git tag v0.11.1 already exists"),
    ({"git_tag": None}, "cannot read the Git tag"),
    ({"release": True}, "GitHub release v0.11.1 already exists"),
    ({"release": None}, "cannot read the GitHub release"),
    ({"release_tags": ["v0.11.1"]}, "GitHub release for v0.11.1 is listed"),
    ({"release_tags": None}, "cannot list"),
    ({"ghcr_tag": True}, "GHCR tag v0.11.1 already exists"),
    ({"ghcr_tag": None}, "cannot read the GHCR tag"),
    ({"channels": {"stable": CANDIDATE, "latest": PREVIOUS}}, "stable already points"),
    ({"channels": {"stable": PREVIOUS, "latest": CANDIDATE}}, "latest already points"),
    ({"channels": {"stable": None, "latest": PREVIOUS}}, "cannot read the GHCR stable"),
    ({"channels": {"stable": "garbage", "latest": PREVIOUS}}, "unreadable"),
    ({"channels": {"stable": PREVIOUS}}, "stable and latest"),
    ({"image_digest": "sha256:bad"}, "image digest"),
    ({"version": "0.11.1rc5"}, "stable version"),
])
def test_resume_state_refuses(changes, error):
    with pytest.raises(ValueError, match=error):
        state(**changes)


def test_resume_state_cli_reads_every_registry(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(release, "fetch_pypi", lambda version: calls.append(version) or pypi())
    monkeypatch.setattr(release, "_ghcr_present", lambda tag: calls.append(tag) or False)
    monkeypatch.setattr(release, "_channel_digest", lambda channel: calls.append(channel)
                        or PREVIOUS)
    monkeypatch.setattr(release, "_gh_release_present",
                        lambda tag, repo: calls.append(("release", tag, repo)) or False)
    monkeypatch.setattr(release, "_gh_tag_present",
                        lambda tag, repo: calls.append(("tag", tag, repo)) or False)
    monkeypatch.setattr(release, "_release_tags",
                        lambda repo: calls.append(("listing", repo)) or [])
    argv = ["resume-state", "--version", VERSION, "--image-digest", CANDIDATE,
            "--repo", "knaisoma/data-olympus"]
    assert release.main(argv) == 0
    repo = "knaisoma/data-olympus"
    assert calls == [VERSION, "v0.11.1", "stable", "latest", ("release", "v0.11.1", repo),
                     ("listing", repo), ("tag", "v0.11.1", repo)]
    assert WHEEL in capsys.readouterr().out
    monkeypatch.setattr(release, "fetch_pypi", lambda _version: None)
    assert release.main(argv) == 1
    assert "resume_pypi is impossible" in capsys.readouterr().err


def test_release_listing_includes_drafts_and_fails_closed(monkeypatch):
    pages = [[{"tag_name": "v0.11.0", "draft": False}], [{"tag_name": "v0.11.1", "draft": True}]]
    monkeypatch.setattr(release, "_gh", lambda *_args: json.dumps(pages).encode())
    assert release._release_tags("knaisoma/data-olympus") == ["v0.11.0", "v0.11.1"]

    def broken(*_args):
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(release, "_gh", broken)
    assert release._release_tags("knaisoma/data-olympus") is None
    monkeypatch.setattr(release, "_gh", lambda *_args: b"{}")
    assert release._release_tags("knaisoma/data-olympus") is None


@pytest.mark.parametrize(("returncode", "stdout", "stderr", "expected"), [
    (0, PREVIOUS + "\n", "", PREVIOUS),
    (1, "", "ERROR: ghcr.io/knaisoma/data-olympus:stable: not found", ""),
    (1, "", "MANIFEST_UNKNOWN: manifest unknown", ""),
    (1, "", "denied: permission", None),
])
def test_channel_digest_reads_fail_closed(monkeypatch, returncode, stdout, stderr, expected):
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **_kwargs:
                        subprocess.CompletedProcess(args, returncode, stdout, stderr))
    assert release._channel_digest("stable") == expected
