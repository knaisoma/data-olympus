"""Tests for the 503-hardening work: health-snapshot TTL cache and thread-safety
of the audit log and consultation ledger.

Context: the single-replica server ran every REST/enforce handler's synchronous
core inline on the one asyncio event loop. Under load the loop stalled past the
1s readiness probe timeout, the only pod was ejected from the Service, and nginx
returned 503. The fixes: cache Index.health() so the readiness path is memory-only
in steady state, and add locks so the stateful writers stay correct once their
work is offloaded to a threadpool (anyio.to_thread) and can run concurrently.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from data_olympus.audit_log import AuditLog
from data_olympus.enforce_policy import ConsultationLedger
from data_olympus.index import Index

if TYPE_CHECKING:
    from pathlib import Path


# --- Index.health() TTL cache ------------------------------------------------


def test_health_cached_within_ttl_and_refreshed_after(tmp_kb: Path, tmp_path: Path) -> None:
    clock = [1000.0]
    idx = Index(tmp_path / "idx.db", clock=lambda: clock[0], health_ttl_sec=5.0)
    idx.build(tmp_kb, source_commit="abc")

    h1 = idx.health()
    assert idx._health_uncached_calls == 1  # first read hits the DB

    clock[0] += 4.0  # still inside the TTL window
    h2 = idx.health()
    assert idx._health_uncached_calls == 1  # served from cache, no DB read
    assert h2 == h1

    clock[0] += 2.0  # 6s since the cached read -> past the 5s TTL
    idx.health()
    assert idx._health_uncached_calls == 2  # re-read from DB


def test_build_invalidates_health_cache(tmp_kb: Path, tmp_path: Path) -> None:
    clock = [0.0]
    # Large TTL so only build() (not expiry) can refresh the cache.
    idx = Index(tmp_path / "idx.db", clock=lambda: clock[0], health_ttl_sec=1000.0)
    idx.build(tmp_kb, source_commit="one")
    assert idx.health()["source_commit"] == "one"

    idx.build(tmp_kb, source_commit="two")
    # Cache must reflect the rebuild immediately, despite the TTL not elapsing.
    assert idx.health()["source_commit"] == "two"


# --- AuditLog thread-safety --------------------------------------------------


def test_concurrent_appends_preserve_hash_chain(tmp_path: Path) -> None:
    log = AuditLog(log_path=str(tmp_path / "audit.log"))
    workers, per_worker = 8, 20

    def worker(n: int) -> None:
        for i in range(per_worker):
            log.append({"status": "ok", "tag": f"{n}-{i}"})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok, broken_idx = log.verify()
    assert ok, f"hash chain broken at line {broken_idx}"
    assert sum(1 for _ in log.iter_filtered()) == workers * per_worker


# --- ConsultationLedger thread-safety ----------------------------------------


def test_concurrent_ledger_records_persist_all_entries(tmp_path: Path) -> None:
    led = ConsultationLedger(path=str(tmp_path / "ledger.json"))
    workers, per_worker = 8, 20

    def worker(n: int) -> None:
        for i in range(per_worker):
            led.record(session_id=f"s{n}-{i}", workspace="w", rule_ids=[], now=float(i))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Reload from disk: a torn write or dict-mutation-during-iteration would drop
    # entries or corrupt the JSON.
    reloaded = ConsultationLedger(path=str(tmp_path / "ledger.json"))
    for n in range(workers):
        for i in range(per_worker):
            assert reloaded.get(session_id=f"s{n}-{i}", workspace="w") is not None


# --- ConsultationLedger bounded growth ---------------------------------------
# The ledger is a TTL freshness cache: an entry older than the consult TTL can
# never be "fresh" again, so keeping it is pure dead weight. Before this fix,
# _entries grew unbounded (every (session, workspace) pair ever seen) and
# record() rewrote the whole file each call -> O(n) that climbs forever.


def test_ledger_evicts_entries_past_retention() -> None:
    led = ConsultationLedger(retention_sec=100.0)
    led.record(session_id="old", workspace="w", rule_ids=[], now=0.0)
    # Recording at now=200 makes 'old' 200s stale (> 100s retention): evicted.
    led.record(session_id="new", workspace="w", rule_ids=[], now=200.0)
    assert led.get(session_id="old", workspace="w") is None
    assert led.get(session_id="new", workspace="w") is not None


def test_ledger_keeps_still_fresh_entries() -> None:
    led = ConsultationLedger(retention_sec=100.0)
    led.record(session_id="a", workspace="w", rule_ids=[], now=0.0)
    # 'a' is only 50s old at the next record (< 100s retention): kept.
    led.record(session_id="b", workspace="w", rule_ids=[], now=50.0)
    assert led.get(session_id="a", workspace="w") is not None
    assert led.get(session_id="b", workspace="w") is not None


def test_ledger_enforces_max_entries_cap() -> None:
    # Retention effectively disabled so only the hard cap can evict.
    led = ConsultationLedger(retention_sec=1e12, max_entries=3)
    for i in range(6):
        led.record(session_id=f"s{i}", workspace="w", rule_ids=[], now=float(i))
    remaining = [i for i in range(6) if led.get(session_id=f"s{i}", workspace="w") is not None]
    assert remaining == [3, 4, 5]  # only the 3 most-recent survive


def test_ledger_eviction_is_persisted(tmp_path: Path) -> None:
    path = str(tmp_path / "ledger.json")
    led = ConsultationLedger(path=path, retention_sec=100.0)
    led.record(session_id="old", workspace="w", rule_ids=[], now=0.0)
    led.record(session_id="new", workspace="w", rule_ids=[], now=200.0)
    # The on-disk file must also be pruned, not just the in-memory dict.
    reloaded = ConsultationLedger(path=path, retention_sec=100.0)
    assert reloaded.get(session_id="old", workspace="w") is None
    assert reloaded.get(session_id="new", workspace="w") is not None
