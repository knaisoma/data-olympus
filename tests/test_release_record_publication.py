"""A resumed promotion accepts only the GitHub release this workflow wrote."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import release_record as release

NOTES = "# Release 0.4.3\n\n## Features\n\n- feat: add export\n"
TAG = "v0.4.3"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def run(tmp_path):
    files = {
        "data_olympus-0.4.3-py3-none-any.whl": b"wheel bytes",
        "data_olympus-0.4.3.tar.gz": b"sdist bytes",
    }
    record = {
        "schema_version": 1, "H": "a" * 40, "S": "b" * 40, "B": "c" * 40, "M": "c" * 40,
        "tag": TAG, "target": "0.4.3", "candidate_tag": "0.4.3-rc.1",
        "candidate_version": "0.4.3rc1", "image_digest": "sha256:" + "d" * 64,
        "notes": NOTES,
    }
    stable = {
        "source_sha": "b" * 40, "wheel": "data_olympus-0.4.3-py3-none-any.whl",
        "sdist": "data_olympus-0.4.3.tar.gz",
        "wheel_sha256": sha(files["data_olympus-0.4.3-py3-none-any.whl"]),
        "sdist_sha256": sha(files["data_olympus-0.4.3.tar.gz"]),
    }
    provenance = {key: record[key] for key in
                  ("H", "S", "B", "M", "tag", "candidate_tag", "image_digest")}
    provenance["stable"] = stable
    files["release-provenance.json"] = json.dumps(provenance).encode()
    files["release-record.json"] = json.dumps(record).encode()
    paths = []
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
        paths.append(tmp_path / name)
    return record, provenance, paths


def expected(run):
    record, provenance, paths = run
    return release.expected_assets(record, provenance, paths, tag=TAG, notes=NOTES)


class FakeGitHub:
    """Offline stand-in for the gh CLI: one existing release, no replacement."""

    def __init__(self, assets, *, body=NOTES, draft=False, prerelease=False, digests=False,
                 forged_digest=False, hidden_views=0, upload_lag=0):
        self.assets = dict(assets)
        self.body, self.draft, self.prerelease, self.digests = body, draft, prerelease, digests
        self.forged_digest = forged_digest
        self.uploads: list[list[str]] = []
        self.events: list[str] = []
        # Listing lag: the first hidden_views views report the release absent,
        # and uploaded assets stay unlisted for upload_lag views.
        self.hidden_views, self.upload_lag = hidden_views, upload_lag
        self.lagging: set[str] = set()
        self.views = 0

    def __call__(self, *args):
        if args[:2] == ("release", "view"):
            self.views += 1
            if self.hidden_views:
                self.hidden_views -= 1
                raise subprocess.CalledProcessError(1, "gh", stderr=b"release not found")
            listed = {name: data for name, data in self.assets.items()
                      if name not in self.lagging}
            if self.lagging:
                self.upload_lag -= 1
                if not self.upload_lag:
                    self.lagging.clear()
            assets = [{"name": name} | ({"digest": "sha256:" + sha(data)} if self.digests else {})
                      for name, data in listed.items()]
            if self.forged_digest:
                # The API digest disagrees with the downloaded bytes.
                assets[0]["digest"] = "sha256:" + "0" * 64
            return json.dumps({"tagName": args[2], "body": self.body, "isDraft": self.draft,
                               "isPrerelease": self.prerelease, "assets": assets}).encode()
        if args[:2] == ("release", "download"):
            return self.assets[args[4]]
        if args[:2] == ("release", "edit"):
            assert args[2:] == (TAG, "--draft=false") and self.draft
            self.draft = False
            self.events.append("publish")
            return b""
        if args[:2] == ("release", "upload"):
            assert "--clobber" not in args
            # Immutable releases refuse new assets once published.
            assert self.draft, "upload to a published release"
            self.uploads.append(list(args[3:]))
            self.events.append("upload")
            for path in args[3:]:
                if Path(path).name in self.assets:
                    raise subprocess.CalledProcessError(1, "gh")
                self.assets[Path(path).name] = Path(path).read_bytes()
                if self.upload_lag:
                    self.lagging.add(Path(path).name)
            return b""
        raise AssertionError(args)


def complete(run, sleeps=None):
    record, provenance, paths = run
    release.complete_release(tag=TAG, notes=NOTES, record=record,
                             stable_provenance=provenance, files=paths,
                             sleep=(sleeps if sleeps is not None else []).append)


def test_expected_assets_bind_every_file_to_the_record(run):
    names = expected(run)
    assert set(names) == {"data_olympus-0.4.3-py3-none-any.whl", "data_olympus-0.4.3.tar.gz",
                          "release-provenance.json", "release-record.json"}


@pytest.mark.parametrize("change", [
    "wheel_hash", "sdist_name", "provenance_digest", "provenance_s", "stable_source",
    "record_notes", "record_tag", "extra_file", "missing_record",
])
def test_expected_assets_refuse_unbound_files(run, change):
    record, provenance, paths = run
    if change == "wheel_hash":
        provenance["stable"]["wheel_sha256"] = "0" * 64
    elif change == "sdist_name":
        provenance["stable"]["sdist"] = "data_olympus-9.9.9.tar.gz"
    elif change == "provenance_digest":
        provenance["image_digest"] = "sha256:" + "0" * 64
    elif change == "provenance_s":
        provenance["S"] = "0" * 40
    elif change == "stable_source":
        provenance["stable"]["source_sha"] = "0" * 40
    elif change == "record_notes":
        record["notes"] = "forged\n"
    elif change == "record_tag":
        record["tag"] = "v0.4.4"
    elif change == "extra_file":
        extra = paths[0].parent / "extra.txt"
        extra.write_text("x")
        paths.append(extra)
    else:
        paths.pop()
    with pytest.raises(ValueError):
        release.expected_assets(record, provenance, paths, tag=TAG, notes=NOTES)


@pytest.mark.parametrize("digests", [False, True])
def test_resume_uploads_only_missing_assets_after_verifying_existing(run, monkeypatch, digests):
    _, _, paths = run
    published = {path.name: path.read_bytes() for path in paths[:2]}
    github = FakeGitHub(published, digests=digests, draft=True)
    monkeypatch.setattr(release, "_gh", github)
    complete(run)
    assert [[Path(item).name for item in upload] for upload in github.uploads] == [
        ["release-provenance.json", "release-record.json"],
    ]
    assert github.assets == {path.name: path.read_bytes() for path in paths}
    # Draft first: every asset is uploaded and verified before publication.
    assert github.events == ["upload", "publish"] and github.draft is False


def test_complete_release_is_idempotent(run, monkeypatch):
    _, _, paths = run
    github = FakeGitHub({path.name: path.read_bytes() for path in paths})
    monkeypatch.setattr(release, "_gh", github)
    complete(run)
    assert github.uploads == [] and github.events == []


def test_complete_draft_is_published_without_uploads(run, monkeypatch):
    _, _, paths = run
    github = FakeGitHub({path.name: path.read_bytes() for path in paths}, draft=True)
    monkeypatch.setattr(release, "_gh", github)
    complete(run)
    assert github.events == ["publish"]


@pytest.mark.parametrize("present", [0, 2])
def test_published_release_with_missing_assets_is_burned(run, monkeypatch, present):
    """Immutable releases: a published release can never be completed."""
    _, _, paths = run
    github = FakeGitHub({path.name: path.read_bytes() for path in paths[:present]})
    monkeypatch.setattr(release, "_gh", github)
    with pytest.raises(ValueError, match="burned"):
        complete(run)
    assert github.events == []


@pytest.mark.parametrize(("change", "error"), [
    ("tampered_asset", "different hash"), ("tampered_record", "different hash"),
    ("tampered_digest_field", "different hash"), ("foreign_asset", "unexpected assets"),
    ("body", "notes differ"), ("prerelease", "prerelease"),
])
def test_existing_release_must_be_this_workflows_own(run, monkeypatch, change, error):
    """Mutation style: any foreign byte in an existing release fails before upload."""
    _, _, paths = run
    assets = {path.name: path.read_bytes() for path in paths[:2]}
    options = {}
    if change == "tampered_asset":
        assets[paths[0].name] = b"substituted wheel"
    elif change == "tampered_record":
        assets["release-record.json"] = b"{}"
    elif change == "foreign_asset":
        assets["backdoor.sh"] = b"echo"
    elif change == "prerelease":
        options[change] = True
    elif change == "body":
        options["body"] = "# Release 0.4.3\n\nWritten by someone else\n"
    github = FakeGitHub(assets, forged_digest=change == "tampered_digest_field", **options)
    monkeypatch.setattr(release, "_gh", github)
    with pytest.raises(ValueError, match=error):
        complete(run)
    assert github.uploads == [] and github.events == []


@pytest.mark.parametrize("change", ["tampered_asset", "body", "prerelease"])
def test_foreign_draft_is_never_published(run, monkeypatch, change):
    _, _, paths = run
    assets = {path.name: path.read_bytes() for path in paths[:2]}
    options = {"draft": True}
    if change == "tampered_asset":
        assets[paths[0].name] = b"substituted wheel"
    elif change == "body":
        options["body"] = "forged\n"
    else:
        options["prerelease"] = True
    github = FakeGitHub(assets, **options)
    monkeypatch.setattr(release, "_gh", github)
    with pytest.raises(ValueError):
        complete(run)
    assert github.events == [] and github.draft is True


def test_body_line_endings_and_trailing_newline_are_not_payload(run):
    names = expected(run)
    remote = {name: digest for name, digest in names.items()}
    view = {"tagName": TAG, "body": NOTES.rstrip("\n").replace("\n", "\r\n"),
            "isDraft": False, "isPrerelease": False,
            "assets": [{"name": name} for name in names]}
    assert release.validate_existing_release(
        view, tag=TAG, notes=NOTES, expected=names, remote_hashes=remote, complete=True,
    ) == []


def test_concurrent_upload_of_a_missing_asset_fails(run, monkeypatch):
    _, _, paths = run
    github = FakeGitHub({path.name: path.read_bytes() for path in paths[:3]}, draft=True)
    real = github.__call__

    def racing(*args):
        if args[:2] == ("release", "upload"):
            github.assets["release-record.json"] = b"raced"
        return real(*args)

    monkeypatch.setattr(release, "_gh", racing)
    with pytest.raises(subprocess.CalledProcessError):
        complete(run)


def test_final_verification_requires_every_asset_after_upload(run, monkeypatch):
    """An upload that exits 0 but leaves an asset missing must still fail."""
    _, _, paths = run
    github = FakeGitHub({path.name: path.read_bytes() for path in paths[:2]}, draft=True)
    real = github.__call__

    def lossy(*args):
        result = real(*args)
        if args[:2] == ("release", "upload"):
            del github.assets["release-record.json"]
        return result

    monkeypatch.setattr(release, "_gh", lossy)
    with pytest.raises(ValueError, match="missing assets"):
        complete(run)
    assert "publish" not in github.events and github.draft is True


def test_cli_release_command_reports_refusal(run, monkeypatch, capsys):
    record, provenance, paths = run
    folder = paths[0].parent
    (folder / "notes.md").write_text(NOTES)
    (folder / "stable.json").write_text(json.dumps(provenance))
    github = FakeGitHub({paths[0].name: b"tampered"})
    monkeypatch.setattr(release, "_gh", github)
    code = release.main(["release", "--tag", TAG, "--record", str(paths[3]),
                         "--stable-provenance", str(folder / "stable.json"),
                         "--notes", str(folder / "notes.md"), *map(str, paths)])
    assert code == 1
    assert "different hash" in capsys.readouterr().err
    assert github.uploads == []


def test_draft_shown_only_on_the_third_listing_is_accepted(run, monkeypatch):
    """Listing lag right after `gh release create --draft` is tolerated, bounded."""
    _, _, paths = run
    github = FakeGitHub({}, draft=True, hidden_views=2)
    monkeypatch.setattr(release, "_gh", github)
    sleeps: list[float] = []
    complete(run, sleeps)
    assert sleeps == [1.0, 2.0]
    assert github.events == ["upload", "publish"] and github.draft is False
    assert github.assets == {path.name: path.read_bytes() for path in paths}


def test_draft_never_listed_is_refused_after_the_bounded_attempts(run, monkeypatch):
    github = FakeGitHub({}, draft=True, hidden_views=10**6)
    monkeypatch.setattr(release, "_gh", github)
    sleeps: list[float] = []
    with pytest.raises(ValueError, match="draft release was not created"):
        complete(run, sleeps)
    assert sleeps == list(release.LISTING_DELAYS)
    assert sum(sleeps) == pytest.approx(60.0)
    assert github.views == len(release.LISTING_DELAYS) + 1
    assert github.uploads == [] and github.events == []


def test_uploaded_assets_listed_late_are_awaited_before_publication(run, monkeypatch):
    _, _, paths = run
    github = FakeGitHub({path.name: path.read_bytes() for path in paths[:2]}, draft=True,
                        upload_lag=2)
    monkeypatch.setattr(release, "_gh", github)
    sleeps: list[float] = []
    complete(run, sleeps)
    assert sleeps == [1.0, 2.0]
    assert github.events == ["upload", "publish"]


def test_other_view_failures_are_never_read_as_absence(run, monkeypatch):
    def broken(*_args):
        raise subprocess.CalledProcessError(1, "gh", stderr=b"HTTP 502: Bad Gateway")

    monkeypatch.setattr(release, "_gh", broken)
    sleeps: list[float] = []
    with pytest.raises(subprocess.CalledProcessError):
        complete(run, sleeps)
    assert sleeps == []
