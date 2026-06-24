"""In-memory sliding-window rate limiter keyed by (remote_addr, agent_identity).

Per spec §2.7 + §5.2 honest framing: this is fair-use accounting for cooperative
agents. A misbehaving agent CAN vary agent_identity to multiply quota; that is
the accepted residual risk under the operator-trusted laptop posture.
"""
from __future__ import annotations

import time
from collections import defaultdict


class SlidingWindowLimiter:
    def __init__(self, max_per_hour: int = 100) -> None:
        self._max = max_per_hour
        self._buckets: dict[tuple[str, str], list[float]] = defaultdict(list)

    def allow(self, *, remote_addr: str, agent_identity: str) -> bool:
        now = time.time()
        cutoff = now - 3600
        key = (remote_addr, agent_identity)
        self._buckets[key] = [t for t in self._buckets[key] if t > cutoff]
        if len(self._buckets[key]) >= self._max:
            return False
        self._buckets[key].append(now)
        return True
