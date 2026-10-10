"""Ownership of the onboarding bootstrap in-flight marker.

``release`` must remove a marker only for the claim that wrote it. A claimer
whose TTL expired, and whose slot another claimer then reclaimed, must not
delete the new holder's live marker on its late release.
"""
from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

import data_olympus.onboarding_inflight as mod
from data_olympus.onboarding_inflight import BootstrapInFlight, _marker_filename

if TYPE_CHECKING:
    from pathlib import Path


class _FakeClock:
    """A ``time`` module stand-in whose clock only moves when told to."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


def _marker(root: Path) -> Path:
    return root / _marker_filename("p", None)


def _marker_token(root: Path) -> object:
    return json.loads(_marker(root).read_text()).get("token")


def test_claim_release_roundtrip(tmp_path) -> None:
    root = tmp_path / "inflight"
    guard = BootstrapInFlight(str(root))
    token = guard.claim("p", None)
    assert isinstance(token, str) and len(token) == 32
    assert _marker_token(root) == token
    assert guard.claim("p", None) is None  # held
    assert guard.release("p", None, token) is True
    assert not _marker(root).exists()
    assert guard.claim("p", None) is not None  # free again


def test_each_claim_gets_a_distinct_token(tmp_path) -> None:
    guard = BootstrapInFlight(str(tmp_path / "inflight"))
    first = guard.claim("p", None)
    assert first is not None
    assert guard.release("p", None, first)
    second = guard.claim("p", None)
    assert second is not None and second != first


def test_double_release_is_a_noop(tmp_path) -> None:
    root = tmp_path / "inflight"
    guard = BootstrapInFlight(str(root))
    token = guard.claim("p", None)
    assert token is not None
    assert guard.release("p", None, token) is True
    assert guard.release("p", None, token) is False  # no raise, nothing removed
    # And the second release did not disturb a later holder.
    later = guard.claim("p", None)
    assert later is not None
    assert guard.release("p", None, token) is False
    assert _marker_token(root) == later


def test_release_before_any_claim_is_a_noop_without_mkdir(tmp_path) -> None:
    root = tmp_path / "inflight"
    assert BootstrapInFlight(str(root)).release("p", None, "0" * 32) is False
    assert not root.exists()


def test_late_release_by_expired_claimer_keeps_new_holders_marker(
    tmp_path, monkeypatch,
) -> None:
    """Claim A, let its TTL lapse, claim B reclaims, A releases late: B's marker
    must survive, still lock out a third claimer, and be removed by B alone."""
    clock = _FakeClock()
    monkeypatch.setattr(mod, "time", clock)
    root = tmp_path / "inflight"
    guard = BootstrapInFlight(str(root), ttl_seconds=10.0)

    token_a = guard.claim("p", None)
    assert token_a is not None
    clock.now += 11.0  # A's claim has expired
    token_b = guard.claim("p", None)
    assert token_b is not None and token_b != token_a

    assert guard.release("p", None, token_a) is False  # late, not A's marker
    assert _marker(root).exists()
    assert _marker_token(root) == token_b
    assert guard.claim("p", None) is None  # B still holds the slot

    assert guard.release("p", None, token_b) is True
    assert not _marker(root).exists()


def test_legacy_marker_without_token_is_never_released(tmp_path, monkeypatch) -> None:
    """A marker written before tokens existed is not removed by any release; it
    holds the slot until its recorded expiry and is then reclaimed."""
    clock = _FakeClock()
    monkeypatch.setattr(mod, "time", clock)
    root = tmp_path / "inflight"
    root.mkdir()
    _marker(root).write_text(json.dumps({
        "workspace": "p", "component": None,
        "claimed_at": clock.now, "expires_at": clock.now + 10.0,
    }))
    guard = BootstrapInFlight(str(root))
    assert guard.release("p", None, "0" * 32) is False
    assert _marker(root).exists()
    assert guard.claim("p", None) is None  # legacy claim still live
    clock.now += 11.0
    token = guard.claim("p", None)
    assert token is not None
    assert _marker_token(root) == token


def test_release_reads_and_unlinks_under_the_claim_lock(tmp_path, monkeypatch) -> None:
    """Deterministic interleaving: a releaser whose own marker has expired is
    parked right after reading the marker. A reclaimer then runs. The
    reclaimer's marker must survive: either the releaser held the lock while
    parked (so it removed only its own marker before the reclaim), or it saw the
    reclaimer's token and left it. A compare outside the lock removes it."""
    root = tmp_path / "inflight"
    stale = BootstrapInFlight(str(root), ttl_seconds=0.0).claim("p", None)
    assert stale is not None  # marker already expired

    parked = threading.Event()
    resume = threading.Event()
    releaser_name = "late-releaser"
    real_read = BootstrapInFlight._read_marker

    def _parking_read(path: str) -> dict[str, object] | None:
        data = real_read(path)
        if threading.current_thread().name == releaser_name and not parked.is_set():
            parked.set()
            assert resume.wait(10), "test harness never resumed the releaser"
        return data

    monkeypatch.setattr(BootstrapInFlight, "_read_marker", staticmethod(_parking_read))
    results: dict[str, object] = {}

    def _release() -> None:
        results["released"] = BootstrapInFlight(str(root)).release("p", None, stale)

    def _reclaim() -> None:
        results["token"] = BootstrapInFlight(str(root), ttl_seconds=900.0).claim("p", None)

    releaser = threading.Thread(target=_release, name=releaser_name)
    releaser.start()
    assert parked.wait(10), "releaser never read the marker"
    reclaimer = threading.Thread(target=_reclaim)
    reclaimer.start()
    reclaimer.join(0.5)  # a correct release keeps the reclaimer blocked here
    resume.set()
    releaser.join(10)
    reclaimer.join(10)
    assert not releaser.is_alive() and not reclaimer.is_alive()

    new_token = results["token"]
    assert isinstance(new_token, str)
    assert _marker(root).exists(), "the late release removed the reclaimer's marker"
    assert _marker_token(root) == new_token


def test_concurrent_claims_and_stale_releases_keep_one_live_holder(tmp_path) -> None:
    """Stress: claimers race to reclaim an expired slot while late releasers of
    the expired claim run alongside them. Every round must end with exactly one
    winner whose marker is still in place, and only that winner can remove it."""
    contenders, stale_releasers = 8, 8
    for round_no in range(50):
        root = tmp_path / f"inflight-{round_no}"
        stale = BootstrapInFlight(str(root), ttl_seconds=0.0).claim("p", None)
        assert stale is not None
        tokens: list[str | None] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        start = threading.Barrier(contenders + stale_releasers)

        def _claim() -> None:
            guard = BootstrapInFlight(str(root), ttl_seconds=900.0)  # noqa: B023
            start.wait()  # noqa: B023
            try:
                token = guard.claim("p", None)
            except BaseException as exc:
                with lock:  # noqa: B023
                    errors.append(exc)  # noqa: B023
                return
            with lock:  # noqa: B023
                tokens.append(token)  # noqa: B023

        def _late_release() -> None:
            guard = BootstrapInFlight(str(root))  # noqa: B023
            start.wait()  # noqa: B023
            try:
                guard.release("p", None, stale)  # noqa: B023
            except BaseException as exc:
                with lock:  # noqa: B023
                    errors.append(exc)  # noqa: B023

        threads = [threading.Thread(target=_claim) for _ in range(contenders)]
        threads += [threading.Thread(target=_late_release) for _ in range(stale_releasers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"round {round_no}: {errors!r}"
        winners = [t for t in tokens if t is not None]
        assert len(winners) == 1, f"round {round_no}: {len(winners)} claimers won"
        assert _marker(root).exists(), f"round {round_no}: winner's marker removed"
        assert _marker_token(root) == winners[0]
        guard = BootstrapInFlight(str(root))
        assert guard.release("p", None, stale) is False
        assert guard.release("p", None, winners[0]) is True


# --------------------------------------------------------------------------
# Call site: kb_bootstrap_project_fn carries the token from claim to release.
# --------------------------------------------------------------------------

class _RecordingGuard(BootstrapInFlight):
    """A real guard that records the tokens it hands out and receives."""

    def __init__(self, root: str) -> None:
        super().__init__(root)
        self.claimed: list[str | None] = []
        self.released: list[str] = []

    def claim(self, workspace: str, component: str | None) -> str | None:
        token = super().claim(workspace, component)
        self.claimed.append(token)
        return token

    def release(self, workspace: str, component: str | None, token: str) -> bool:
        self.released.append(token)
        return super().release(workspace, component, token)


def _bootstrap(tmp_path, guard, **overrides):
    from data_olympus.auth import PathBlocklist
    from data_olympus.pending import PendingQueue
    from data_olympus.rate_limit import SlidingWindowLimiter
    from data_olympus.tools_onboarding import kb_bootstrap_project_fn
    idx = MagicMock()
    idx.list_by_prefix.return_value = []  # absent
    idx.list_with_remote_url.return_value = []
    kwargs = dict(
        idx=idx, workspace="p", component=None,
        workspace_remote_url=None, component_remote_url=None,
        files=[{"target_path": "projects/p/README.md", "postimage": "r\n"}],
        source_session="s", agent_identity="claude",
        confidence=0.95, confidence_threshold=0.85,
        worktrees=MagicMock(), push_queue=MagicMock(),
        pending=PendingQueue(pending_root=str(tmp_path / "p")),
        rate_limiter=SlidingWindowLimiter(max_per_hour=100),
        blocklist=PathBlocklist(tier_blocks=[], path_blocks=[]),
        in_flight=guard,
    )
    kwargs.update(overrides)
    return kb_bootstrap_project_fn(**kwargs)


def test_bootstrap_releases_with_its_own_token_on_rejection(tmp_path) -> None:
    guard = _RecordingGuard(str(tmp_path / "inflight"))
    files = [{"target_path": f"projects/p/f{i}.md", "postimage": "x"} for i in range(3)]
    resp = _bootstrap(tmp_path, guard, files=files, max_files=2)
    assert resp.status == "rejected_too_many_files"
    assert len(guard.claimed) == 1 and guard.claimed[0] is not None
    assert guard.released == guard.claimed
    assert not _marker(tmp_path / "inflight").exists()


def test_bootstrap_releases_with_its_own_token_in_finally_on_exception(tmp_path) -> None:
    guard = _RecordingGuard(str(tmp_path / "inflight"))
    boom = MagicMock()
    boom.get_or_create.side_effect = RuntimeError("git blew up")
    with pytest.raises(RuntimeError):
        _bootstrap(tmp_path, guard, worktrees=boom)
    assert len(guard.claimed) == 1 and guard.claimed[0] is not None
    assert guard.released == guard.claimed
    assert not _marker(tmp_path / "inflight").exists()


def test_bootstrap_late_release_spares_a_reclaiming_holder(tmp_path, monkeypatch) -> None:
    """End to end through the tool: a bootstrap that outlives its TTL, during
    which another caller reclaims the slot, must not free that caller's slot
    when it finally fails and releases."""
    clock = _FakeClock()
    monkeypatch.setattr(mod, "time", clock)
    root = tmp_path / "inflight"
    guard = BootstrapInFlight(str(root), ttl_seconds=10.0)
    holder: dict[str, str | None] = {}

    def _slow_failure(**_kwargs):
        clock.now += 11.0  # this bootstrap's claim expires mid-flight
        holder["token"] = guard.claim("p", None)  # another caller reclaims
        raise RuntimeError("git blew up")

    boom = MagicMock()
    boom.get_or_create.side_effect = _slow_failure
    with pytest.raises(RuntimeError):
        _bootstrap(tmp_path, guard, worktrees=boom)
    assert isinstance(holder["token"], str)
    assert _marker(root).exists()
    assert _marker_token(root) == holder["token"]
    assert guard.claim("p", None) is None
