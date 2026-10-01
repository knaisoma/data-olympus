"""KB_AUTH_PRINCIPALS must describe usable principals, or startup fails."""
from __future__ import annotations

import pytest

from data_olympus.config import load_config

_TOKEN = "t0k3n-value-for-tests"


@pytest.mark.parametrize("raw", [
    # Trailing comma: not valid JSON.
    f'[{{"name":"ci","token":"{_TOKEN}","capabilities":["read","propose"]}},]',
    # A single object rather than a list.
    f'{{"name":"ci","token":"{_TOKEN}","capabilities":["read","propose"]}}',
    # An entry whose token key is misspelled.
    f'[{{"name":"ci","tokens":"{_TOKEN}","capabilities":["read","propose"]}}]',
    # Entries without any token.
    '[{"name":"ci","capabilities":["read"]}]',
    # A blank or non-string token.
    '[{"name":"ci","token":"   "}]',
    '[{"name":"ci","token":null}]',
    # One usable entry next to an entry without a token.
    f'[{{"name":"a","token":"{_TOKEN}"}},{{"name":"b","capabilities":["read"]}}]',
    # A non-object entry next to a usable one.
    f'[{{"name":"a","token":"{_TOKEN}"}},"b"]',
    # An empty list.
    "[]",
])
def test_unusable_auth_principals_fail_startup(
    monkeypatch: pytest.MonkeyPatch, raw: str,
) -> None:
    monkeypatch.delenv("KB_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("KB_AUTH_PRINCIPALS", raw)
    with pytest.raises(ValueError, match="KB_AUTH_PRINCIPALS") as info:
        load_config()
    # The message never repeats the configured value, which carries tokens.
    assert _TOKEN not in str(info.value)
    assert raw not in str(info.value)


def test_unusable_auth_principals_fail_even_with_an_operator_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_AUTH_TOKEN", "operator-token")
    monkeypatch.setenv("KB_AUTH_PRINCIPALS", '[{"name":"ci","tokens":"x"}]')
    with pytest.raises(ValueError, match="KB_AUTH_PRINCIPALS"):
        load_config()


def test_valid_auth_principals_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_AUTH_TOKEN", raising=False)
    monkeypatch.setenv(
        "KB_AUTH_PRINCIPALS",
        f'[{{"name":"ci","token":"{_TOKEN}","capabilities":["read","propose"]}}]',
    )
    cfg = load_config()
    assert cfg.auth_principals == [
        {"name": "ci", "token": _TOKEN, "capabilities": ["read", "propose"]},
    ]


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_or_blank_auth_principals_load(
    monkeypatch: pytest.MonkeyPatch, raw: str | None,
) -> None:
    if raw is None:
        monkeypatch.delenv("KB_AUTH_PRINCIPALS", raising=False)
    else:
        monkeypatch.setenv("KB_AUTH_PRINCIPALS", raw)
    assert load_config().auth_principals == []
