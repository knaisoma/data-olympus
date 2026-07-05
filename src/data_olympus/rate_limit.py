"""In-memory sliding-window rate limiter keyed by (remote_addr, agent_identity).

Honest framing: this is fair-use accounting for cooperative agents. A misbehaving
agent CAN vary agent_identity to multiply quota; that is the accepted residual
risk under a trusted single-user deployment posture. To bound that abuse a
deployment can also set an optional per-IP cap (``max_per_ip_per_hour``) which
ignores agent_identity, so varying it no longer multiplies quota from one source.
"""
from __future__ import annotations

import threading
import time
from typing import Any


class SlidingWindowLimiter:
    def __init__(
        self, max_per_hour: int = 100, max_per_ip_per_hour: int = 0
    ) -> None:
        self._max = max_per_hour
        self._max_ip = max_per_ip_per_hour
        self._buckets: dict[tuple[str, str], list[float]] = {}
        self._ip_buckets: dict[str, list[float]] = {}
        # REST handlers run on the anyio worker-thread pool (see rest_api._offload),
        # so allow() is called concurrently from multiple threads. The read-modify-
        # write on the bucket lists is not atomic; without this lock two threads can
        # interleave and both admit a request past the cap, or corrupt a list mid-
        # rebuild. The critical section is a few list ops on already-pruned data, so
        # contention is negligible (item 10).
        self._lock = threading.Lock()

    def allow(self, *, remote_addr: str, agent_identity: str) -> bool:
        now = time.time()
        cutoff = now - 3600
        key = (remote_addr, agent_identity)
        with self._lock:
            pruned = [t for t in self._buckets.get(key, ()) if t > cutoff]
            # Per-IP cap (0 = disabled): bounds total quota from one source address
            # regardless of how many agent_identity values it presents.
            if self._max_ip > 0:
                ip_pruned = [
                    t for t in self._ip_buckets.get(remote_addr, ()) if t > cutoff
                ]
                if len(ip_pruned) >= self._max_ip:
                    # Store back the pruned lists (or evict when empty) before
                    # returning, so a denied caller still drops expired timestamps
                    # and empty keys never accumulate under varying identities.
                    self._store(self._buckets, key, pruned)
                    self._store(self._ip_buckets, remote_addr, ip_pruned)
                    return False
            else:
                ip_pruned = None
            if len(pruned) >= self._max:
                self._store(self._buckets, key, pruned)
                if ip_pruned is not None:
                    self._store(self._ip_buckets, remote_addr, ip_pruned)
                return False
            pruned.append(now)
            self._store(self._buckets, key, pruned)
            if self._max_ip > 0:
                assert ip_pruned is not None
                ip_pruned.append(now)
                self._store(self._ip_buckets, remote_addr, ip_pruned)
            return True

    @staticmethod
    def _store(buckets: dict[Any, list[float]], key: Any, values: list[float]) -> None:
        """Write ``values`` back under ``key``, deleting the key entirely when the
        list is empty. Evicting empty keys bounds memory: without it, every unique
        (remote_addr, agent_identity) pair — which a caller can vary freely — left a
        permanent empty-list entry, an unbounded leak (item 10)."""
        if values:
            buckets[key] = values
        else:
            buckets.pop(key, None)

    def bucket_count(self) -> int:
        """Number of live (non-evicted) bucket keys. Exposed for tests asserting
        that empty keys are evicted rather than accumulating."""
        with self._lock:
            return len(self._buckets) + len(self._ip_buckets)
