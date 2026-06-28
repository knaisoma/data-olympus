"""In-memory sliding-window rate limiter keyed by (remote_addr, agent_identity).

Honest framing: this is fair-use accounting for cooperative agents. A misbehaving
agent CAN vary agent_identity to multiply quota; that is the accepted residual
risk under a trusted single-user deployment posture. To bound that abuse a
deployment can also set an optional per-IP cap (``max_per_ip_per_hour``) which
ignores agent_identity, so varying it no longer multiplies quota from one source.
"""
from __future__ import annotations

import time
from collections import defaultdict


class SlidingWindowLimiter:
    def __init__(
        self, max_per_hour: int = 100, max_per_ip_per_hour: int = 0
    ) -> None:
        self._max = max_per_hour
        self._max_ip = max_per_ip_per_hour
        self._buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._ip_buckets: dict[str, list[float]] = defaultdict(list)

    def allow(self, *, remote_addr: str, agent_identity: str) -> bool:
        now = time.time()
        cutoff = now - 3600
        key = (remote_addr, agent_identity)
        self._buckets[key] = [t for t in self._buckets[key] if t > cutoff]
        # Per-IP cap (0 = disabled): bounds total quota from one source address
        # regardless of how many agent_identity values it presents.
        if self._max_ip > 0:
            self._ip_buckets[remote_addr] = [
                t for t in self._ip_buckets[remote_addr] if t > cutoff
            ]
            if len(self._ip_buckets[remote_addr]) >= self._max_ip:
                return False
        if len(self._buckets[key]) >= self._max:
            return False
        self._buckets[key].append(now)
        if self._max_ip > 0:
            self._ip_buckets[remote_addr].append(now)
        return True
