from __future__ import annotations

from data_olympus.config import load_config


def test_ledger_path_default(monkeypatch) -> None:
    monkeypatch.delenv("KB_LEDGER_PATH", raising=False)
    assert load_config().ledger_path == "/state/ledger.json"


def test_ledger_path_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KB_LEDGER_PATH", "/tmp/x.json")
    assert load_config().ledger_path == "/tmp/x.json"
