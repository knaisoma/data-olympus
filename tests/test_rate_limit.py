"""Tests for the sliding-window rate limiter."""
from __future__ import annotations

from data_olympus.rate_limit import SlidingWindowLimiter


def test_allow_under_limit() -> None:
    rl = SlidingWindowLimiter(max_per_hour=5)
    for _ in range(5):
        assert rl.allow(remote_addr="10.0.0.1", agent_identity="claude") is True


def test_block_at_limit() -> None:
    rl = SlidingWindowLimiter(max_per_hour=3)
    for _ in range(3):
        assert rl.allow(remote_addr="10.0.0.1", agent_identity="claude") is True
    assert rl.allow(remote_addr="10.0.0.1", agent_identity="claude") is False


def test_separate_buckets_per_remote_addr() -> None:
    rl = SlidingWindowLimiter(max_per_hour=2)
    assert rl.allow(remote_addr="10.0.0.1", agent_identity="claude") is True
    assert rl.allow(remote_addr="10.0.0.1", agent_identity="claude") is True
    assert rl.allow(remote_addr="10.0.0.1", agent_identity="claude") is False
    # Different remote_addr -> own bucket.
    assert rl.allow(remote_addr="10.0.0.2", agent_identity="claude") is True


def test_separate_buckets_per_agent_identity() -> None:
    """This is fair-use accounting per the honest framing in spec §5.2.
    Distinct cooperative agents on the same IP each get their own bucket.
    A misbehaving agent CAN multiply quota by varying agent_identity; that
    is the accepted residual risk."""
    rl = SlidingWindowLimiter(max_per_hour=1)
    assert rl.allow(remote_addr="10.0.0.1", agent_identity="claude") is True
    assert rl.allow(remote_addr="10.0.0.1", agent_identity="claude") is False
    assert rl.allow(remote_addr="10.0.0.1", agent_identity="codex") is True


def test_window_slides_after_hour(monkeypatch) -> None:
    """After 3600s, the old timestamps fall out of the window."""
    import data_olympus.rate_limit as rl_mod
    fake_time = [1000.0]
    monkeypatch.setattr(rl_mod.time, "time", lambda: fake_time[0])
    rl = SlidingWindowLimiter(max_per_hour=2)
    assert rl.allow(remote_addr="10.0.0.1", agent_identity="claude") is True
    assert rl.allow(remote_addr="10.0.0.1", agent_identity="claude") is True
    assert rl.allow(remote_addr="10.0.0.1", agent_identity="claude") is False
    # Advance 1 hour + 1s.
    fake_time[0] += 3601
    assert rl.allow(remote_addr="10.0.0.1", agent_identity="claude") is True
