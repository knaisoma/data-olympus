from __future__ import annotations

import pytest

import scripts.check_version_free as version_guard
from scripts.check_version_free import (
    RegistryError,
    VersionTaken,
    evaluate,
    idempotent_rerun,
    main,
)


def test_evaluate_free_everywhere() -> None:
    code, report = evaluate(
        version="0.5.0",
        on_pypi=False,
        on_ghcr=False,
        on_github=False,
    )
    assert code == 0
    assert "free" in report.lower()


@pytest.mark.parametrize(
    ("pypi", "ghcr", "github", "needle"),
    [
        (True, False, False, "PyPI"),
        (False, True, False, "ghcr"),
        (False, False, True, "GitHub"),
        (True, True, True, "PyPI"),
    ],
)
def test_evaluate_taken_anywhere_nonzero(
    pypi: bool, ghcr: bool, github: bool, needle: str
) -> None:
    code, report = evaluate(
        version="0.5.0",
        on_pypi=pypi,
        on_ghcr=ghcr,
        on_github=github,
    )
    assert code == VersionTaken.exit_code
    assert "0.5.0" in report
    assert needle in report


def test_idempotent_rerun_true_when_tag_at_head() -> None:
    # A prior run created + pushed vX.Y.Z at the current commit; this is a
    # legitimate reconcile, so the version check is skipped (allowed).
    assert idempotent_rerun("0.5.0", tag_commit="abc123", head_commit="abc123") is True


def test_idempotent_rerun_false_when_tag_elsewhere() -> None:
    assert idempotent_rerun("0.5.0", tag_commit="old789", head_commit="new456") is False


def test_idempotent_rerun_false_when_tag_absent() -> None:
    assert idempotent_rerun("0.5.0", tag_commit=None, head_commit="new456") is False


def test_ghcr_checks_the_exact_public_registry_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> object:
        calls.append((command, kwargs))
        return version_guard.subprocess.CompletedProcess(
            command,
            0,
            stdout="Name: ghcr.io/knaisoma/data-olympus:v0.7.0",
            stderr="",
        )

    monkeypatch.setattr(version_guard.subprocess, "run", run)

    assert version_guard._on_ghcr("0.7.0", "knaisoma/data-olympus") is True
    assert calls == [
        (
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                "ghcr.io/knaisoma/data-olympus:v0.7.0",
            ],
            {
                "capture_output": True,
                "stdin": version_guard.subprocess.DEVNULL,
                "text": True,
                "timeout": 30,
            },
        )
    ]


@pytest.mark.parametrize(
    "diagnostic",
    [
        "manifest unknown",
        "no such manifest",
        "ERROR: ghcr.io/knaisoma/data-olympus:v0.7.0: not found",
    ],
)
def test_ghcr_treats_only_an_explicit_missing_manifest_as_free(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic: str,
) -> None:
    monkeypatch.setattr(
        version_guard.subprocess,
        "run",
        lambda command, **_kwargs: version_guard.subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=diagnostic,
        ),
    )

    assert version_guard._on_ghcr("0.7.0", "knaisoma/data-olympus") is False


@pytest.mark.parametrize(
    "diagnostic",
    [
        'error getting credentials: executable file not found in $PATH',
        "",
    ],
)
def test_ghcr_does_not_misclassify_client_or_unknown_errors_as_free(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic: str,
) -> None:
    monkeypatch.setattr(
        version_guard.subprocess,
        "run",
        lambda command, **_kwargs: version_guard.subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=diagnostic,
        ),
    )

    with pytest.raises(RegistryError):
        version_guard._on_ghcr("0.7.0", "knaisoma/data-olympus")


def test_ghcr_timeout_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(command: list[str], **_kwargs: object) -> object:
        raise version_guard.subprocess.TimeoutExpired(command, timeout=30)

    monkeypatch.setattr(version_guard.subprocess, "run", timeout)

    with pytest.raises(RegistryError, match="timed out"):
        version_guard._on_ghcr("0.7.0", "knaisoma/data-olympus")


def test_ghcr_registry_failure_does_not_leak_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_diagnostic = "denied with credential marker do-not-expose"
    monkeypatch.setattr(
        version_guard.subprocess,
        "run",
        lambda command, **_kwargs: version_guard.subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=sensitive_diagnostic,
        ),
    )

    with pytest.raises(RegistryError) as captured:
        version_guard._on_ghcr("0.7.0", "knaisoma/data-olympus")
    assert sensitive_diagnostic not in str(captured.value)


def test_ghcr_fails_closed_when_registry_client_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_executable(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(version_guard.subprocess, "run", missing_executable)
    with pytest.raises(RegistryError, match="registry client unavailable"):
        version_guard._on_ghcr("0.7.0", "knaisoma/data-olympus")


def test_ghcr_fails_closed_on_registry_authorization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        version_guard.subprocess,
        "run",
        lambda command, **_kwargs: version_guard.subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="denied: requested access to the resource is denied",
        ),
    )
    with pytest.raises(RegistryError, match="ghcr.io/knaisoma/data-olympus:v0.7.0"):
        version_guard._on_ghcr("0.7.0", "knaisoma/data-olympus")


def _patch_registry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pypi: object,
    ghcr: object,
    github: object,
    tag_commit: str | None = None,
    head_commit: str = "HEADSHA",
) -> None:
    import scripts.check_version_free as mod

    def _raise_or_return(value: object) -> object:
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(mod, "_on_pypi", lambda _version: _raise_or_return(pypi))
    monkeypatch.setattr(mod, "_on_ghcr", lambda _version, _repo: _raise_or_return(ghcr))
    monkeypatch.setattr(mod, "_on_github", lambda _version, _repo: _raise_or_return(github))
    monkeypatch.setattr(mod, "_tag_commit", lambda _tag: tag_commit)
    monkeypatch.setattr(mod, "_head_commit", lambda: head_commit)


def test_main_free_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_registry(monkeypatch, pypi=False, ghcr=False, github=False)
    assert main(["--version", "0.5.0"]) == 0


def test_main_taken_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_registry(monkeypatch, pypi=True, ghcr=False, github=False)
    assert main(["--version", "0.5.0"]) == VersionTaken.exit_code


def test_main_idempotent_rerun_skips_registry_and_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tag at HEAD -> allowed AND registries must not be consulted at all.
    sentinel = RuntimeError("registry must not be queried on an idempotent re-run")
    _patch_registry(
        monkeypatch,
        pypi=sentinel,
        ghcr=sentinel,
        github=sentinel,
        tag_commit="HEADSHA",
        head_commit="HEADSHA",
    )
    assert main(["--version", "0.5.0"]) == 0


def test_main_taken_but_tag_not_at_head_hard_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exists on PyPI but no local tag matches HEAD -> hard fail (not idempotent).
    _patch_registry(
        monkeypatch,
        pypi=True,
        ghcr=False,
        github=False,
        tag_commit="old789",
        head_commit="HEADSHA",
    )
    assert main(["--version", "0.5.0"]) == VersionTaken.exit_code


def test_main_registry_outage_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_registry(
        monkeypatch,
        pypi=RegistryError("pypi unreachable"),
        ghcr=False,
        github=False,
    )
    assert main(["--version", "0.5.0"]) == RegistryError.exit_code


def test_main_bypass_override_allows_despite_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_BYPASS_VERSION_CHECK", "1")
    # Even if everything is taken, the operator override allows proceeding, and
    # the registries need not be consulted.
    sentinel = RuntimeError("registry must not be queried when bypassed")
    _patch_registry(monkeypatch, pypi=sentinel, ghcr=sentinel, github=sentinel)
    assert main(["--version", "0.5.0"]) == 0


def test_main_bypass_override_allows_despite_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_BYPASS_VERSION_CHECK", "1")
    sentinel = RuntimeError("registry must not be queried when bypassed")
    _patch_registry(monkeypatch, pypi=sentinel, ghcr=sentinel, github=sentinel)
    assert main(["--version", "0.5.0"]) == 0
