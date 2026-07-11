"""Tests for update-available detection (issue #146, first slice)."""
from __future__ import annotations

from data_olympus.update_check import update_available, update_notice


def test_update_available_true_when_latest_is_newer() -> None:
    assert update_available("0.4.2", "0.5.0") is True
    assert update_available("0.4.2", "0.4.3") is True
    assert update_available("0.4.2", "1.0.0") is True


def test_update_available_false_when_equal_or_older() -> None:
    assert update_available("0.5.0", "0.5.0") is False
    assert update_available("0.5.0", "0.4.9") is False


def test_update_available_tolerates_v_prefix() -> None:
    assert update_available("0.4.2", "v0.5.0") is True
    assert update_available("v0.4.2", "0.4.2") is False


def test_update_available_fail_safe_on_unknown_or_bad_input() -> None:
    # Offline lookup / unparseable -> never a bogus "update available".
    assert update_available("0.4.2", None) is False
    assert update_available("0.4.2", "not-a-version") is False
    assert update_available("garbage", "0.5.0") is False


def test_update_notice_only_when_newer() -> None:
    assert update_notice("0.5.0", "0.5.0") is None
    assert update_notice("0.4.2", None) is None
    notice = update_notice("0.4.2", "0.5.0")
    assert notice is not None
    assert "0.5.0" in notice and "0.4.2" in notice
