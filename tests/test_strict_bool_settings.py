"""Boolean settings refuse an unrecognised value (#314, #315).

KB_READ_ONLY, KB_DISABLE_VERSION_CHECK and KB_STATUS_AUTOFILL accept the
documented true spellings (1, true, yes, on) and false spellings (0, false, no,
off), case-insensitive with surrounding whitespace ignored; an empty value
means unset and keeps the setting's default. Anything else fails load_config
naming the setting, instead of silently reading as false.
"""
from __future__ import annotations

import pytest

from data_olympus.config import load_config

# (environment variable, Config attribute, default when unset or blank)
SETTINGS = (
    ("KB_READ_ONLY", "read_only", False),
    ("KB_DISABLE_VERSION_CHECK", "disable_version_check", False),
    ("KB_STATUS_AUTOFILL", "status_autofill", True),
)
TRUE_VALUES = ("1", "true", "yes", "on", "TRUE", " On ", "Yes\n")
FALSE_VALUES = ("0", "false", "no", "off", "OFF", " False ")
UNSET_VALUES = ("", "   ")
INVALID_VALUES = ("ture", "enabled", "2", "y", "n", "disable", "true,")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for env, _, _ in SETTINGS:
        monkeypatch.delenv(env, raising=False)


@pytest.mark.parametrize(("env", "attr"), [(e, a) for e, a, _ in SETTINGS])
@pytest.mark.parametrize("value", TRUE_VALUES)
def test_true_spellings_enable(
    monkeypatch: pytest.MonkeyPatch, env: str, attr: str, value: str,
) -> None:
    monkeypatch.setenv(env, value)
    assert getattr(load_config(), attr) is True


@pytest.mark.parametrize(("env", "attr"), [(e, a) for e, a, _ in SETTINGS])
@pytest.mark.parametrize("value", FALSE_VALUES)
def test_false_spellings_disable(
    monkeypatch: pytest.MonkeyPatch, env: str, attr: str, value: str,
) -> None:
    monkeypatch.setenv(env, value)
    assert getattr(load_config(), attr) is False


@pytest.mark.parametrize(("env", "attr", "default"), SETTINGS)
@pytest.mark.parametrize("value", UNSET_VALUES)
def test_empty_means_unset(
    monkeypatch: pytest.MonkeyPatch, env: str, attr: str, default: bool, value: str,
) -> None:
    monkeypatch.setenv(env, value)
    assert getattr(load_config(), attr) is default


@pytest.mark.parametrize(("attr", "default"), [(a, d) for _, a, d in SETTINGS])
def test_unset_keeps_the_default(attr: str, default: bool) -> None:
    assert getattr(load_config(), attr) is default


@pytest.mark.parametrize("env", [e for e, _, _ in SETTINGS])
@pytest.mark.parametrize("value", INVALID_VALUES)
def test_unrecognised_value_fails_startup_naming_the_setting(
    monkeypatch: pytest.MonkeyPatch, env: str, value: str,
) -> None:
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError, match=env) as exc:
        load_config()
    assert repr(value) in str(exc.value)
