"""Config fields for the maintenance ledger (issue #113)."""
from __future__ import annotations

from data_olympus.config import load_config


def test_maintenance_defaults(monkeypatch) -> None:
    monkeypatch.delenv("KB_MAINTENANCE_LEDGER_PATH", raising=False)
    monkeypatch.delenv("KB_MAINTENANCE_RECENTLY_EXPIRED_DAYS", raising=False)
    monkeypatch.delenv("KB_MAINTENANCE_EXPIRING_SOON_DAYS", raising=False)
    cfg = load_config()
    assert cfg.maintenance_ledger_path == "tooling/maintenance-ledger.md"
    assert cfg.maintenance_recently_expired_days == 30
    assert cfg.maintenance_expiring_soon_days == 30


def test_maintenance_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KB_MAINTENANCE_LEDGER_PATH", "ops/kb-ledger.md")
    monkeypatch.setenv("KB_MAINTENANCE_RECENTLY_EXPIRED_DAYS", "14")
    monkeypatch.setenv("KB_MAINTENANCE_EXPIRING_SOON_DAYS", "45")
    cfg = load_config()
    assert cfg.maintenance_ledger_path == "ops/kb-ledger.md"
    assert cfg.maintenance_recently_expired_days == 14
    assert cfg.maintenance_expiring_soon_days == 45
