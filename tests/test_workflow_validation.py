"""Publication input validation and immutable ref binding."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/workflow_validation.py"


def module():
    assert SCRIPT.exists(), "publication validator is missing"
    spec = importlib.util.spec_from_file_location("workflow_validation", SCRIPT)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize("tag", ["", "x\ny", "x\r", "x\t", "x/y", "-x", "a" * 129])
def test_rejects_invalid_oci_tag(tag):
    with pytest.raises(ValueError):
        module().oci_tag(tag)


@pytest.mark.parametrize("tag", ["edge", "preview.test", "v1.2.3", "v1.2.3-hotfix.rc.2"])
def test_accepts_single_oci_tag(tag):
    assert module().oci_tag(tag) == tag


@pytest.mark.parametrize("tag", ["latest", "stable", "rc"])
def test_manual_image_rejects_channels(tag):
    with pytest.raises(ValueError):
        module().manual_image(tag)


def test_manual_image_rejects_existing_stable_tag(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **_kw: subprocess.CompletedProcess(a, 0, ""))
    with pytest.raises(ValueError, match="existing"):
        module().manual_image("v1.2.3")


def test_manual_tag_is_exact_and_build_is_bound_to_tag_commit(tmp_path, monkeypatch):
    import urllib.error
    import urllib.request

    def absent(url, **_kwargs):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", absent)

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    git("add", "pyproject.toml")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "first",
    )
    sha = git("rev-parse", "HEAD")
    git("tag", "v1.2.3")
    git("branch", "v1.2.3")
    monkeypatch.chdir(tmp_path)
    assert module().manual_python("v1.2.3") == (sha, True)
    assert module().manual_python("HEAD") == (sha, False)
    for ref in ("v1.2.3^{commit}", "v1.2.3bad", "v1.2.3\n", "v9.9.9"):
        with pytest.raises(ValueError):
            module().manual_python(ref)


def test_image_output_uses_unique_delimiter_and_validates_channel():
    validator = module()
    first = validator.image_outputs("edge", "rc", True)
    assert first != validator.image_outputs("edge", "rc", True)
    lines = first.splitlines()
    assert lines[0] == "tags<<" + lines[-1]
    assert lines[1:-1] == [
        "ghcr.io/knaisoma/data-olympus:edge",
        "ghcr.io/knaisoma/data-olympus:latest",
        "ghcr.io/knaisoma/data-olympus:rc",
    ]
    with pytest.raises(ValueError):
        validator.image_outputs("edge", "other", False)


def test_channel_promotion_binds_digest_and_checks_result(monkeypatch):
    calls = []
    digest = "sha256:" + "a" * 64

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, digest + "\n")

    monkeypatch.setattr(subprocess, "run", run)
    module().set_channel("stable", "edge")
    create = next(command for command in calls if "create" in command)
    assert create[-1] == "ghcr.io/knaisoma/data-olympus@" + digest
    assert len([command for command in calls if "inspect" in command]) == 2


def test_channel_promotion_rejects_digest_mismatch(monkeypatch):
    results = iter(["sha256:" + "a" * 64, "", "sha256:" + "b" * 64])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kw: subprocess.CompletedProcess(command, 0, next(results)),
    )
    with pytest.raises(ValueError, match="mismatch"):
        module().set_channel("stable", "edge")


def test_publication_fallbacks_have_no_tag_push_trigger():
    import yaml

    workflows = SCRIPT.parents[1] / ".github/workflows"
    for name in ("release-image.yml", "publish-pypi.yml"):
        doc = yaml.safe_load((workflows / name).read_text())
        triggers = doc.get("on", doc.get(True))
        assert "push" not in triggers
    image = yaml.safe_load((workflows / "release-image.yml").read_text())
    assert image["jobs"]["prerelease"]["needs"] == "validate"


@pytest.mark.parametrize("value", ["", "sha256:bad", "sha256:" + "a" * 64 + "\nother"])
def test_channel_rejects_invalid_digest_before_create(monkeypatch, value):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, value)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="invalid image digest"):
        module().set_channel("stable", "edge")
    assert not any("create" in command for command in calls)


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2.3"])
def test_manual_image_checks_both_stable_aliases(monkeypatch, tag):
    def run(command, **_kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 1, "")
        if command[-1].endswith(":1.2.3"):
            return subprocess.CompletedProcess(command, 0, "")
        return subprocess.CompletedProcess(command, 1, "", f"ERROR: {command[-1]}: not found")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="existing"):
        module().manual_image(tag)


def test_manual_image_registry_token_failure_is_not_absence(monkeypatch):
    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "token service not found")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="could not check"):
        module().manual_image("v1.2.3")


@pytest.mark.parametrize(
    "tag,expected",
    [("v1.2.3", "1.2.3"), ("v1.2.3-rc.2", "1.2.3rc2"), ("v1.2.3-hotfix.rc.2", "1.2.3.dev2")],
)
def test_manual_python_rejects_existing_mapped_identity(monkeypatch, tag, expected):
    import io
    import urllib.request

    validator = module()
    urls = []
    monkeypatch.setattr(
        validator,
        "run",
        lambda *args: f'[project]\nversion = "{expected}"\n' if args[1] == "show" else "a" * 40,
    )

    def urlopen(url, **_kwargs):
        urls.append(url)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(ValueError, match="existing Python"):
        validator.manual_python(tag)
    assert urls == [f"https://pypi.org/pypi/data-olympus/{expected}/json"]


@pytest.mark.parametrize("status", [403, 500])
def test_manual_python_fails_closed_when_registry_is_unavailable(monkeypatch, status):
    import urllib.error
    import urllib.request

    validator = module()
    monkeypatch.setattr(
        validator,
        "run",
        lambda *args: '[project]\nversion = "1.2.3"\n' if args[1] == "show" else "a" * 40,
    )

    def unavailable(url, **_kwargs):
        raise urllib.error.HTTPError(url, status, "Unavailable", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    with pytest.raises(ValueError, match="could not check Python"):
        validator.manual_python("v1.2.3")


@pytest.mark.parametrize("tag", ["v1.2.3", "v1.2.3-rc.2"])
def test_manual_python_refuses_metadata_mismatch_before_registry(monkeypatch, tag):
    import urllib.request

    validator = module()
    commands = []

    def git(*args):
        commands.append(args)
        return '[project]\nversion = "9.9.9"\n' if args[1] == "show" else "a" * 40

    def unexpected_registry(*_args, **_kwargs):
        pytest.fail("metadata mismatch must fail before registry access")

    monkeypatch.setattr(validator, "run", git)
    monkeypatch.setattr(urllib.request, "urlopen", unexpected_registry)
    with pytest.raises(ValueError, match="metadata version"):
        validator.manual_python(tag)
    assert ("git", "show", "a" * 40 + ":pyproject.toml") in commands
