"""In-memory per-session token-bucket rate limiter. Cheap insurance against runaway LLM
spend (each query can trigger multiple LLM calls via the self-correction retry loop) -
not a substitute for real API-gateway rate limiting in an actual production deployment,
but real protection for a self-hosted demo.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    def __init__(self, capacity: int, refill_per_minute: int):
        self._capacity = float(capacity)
        self._refill_per_second = refill_per_minute / 60.0
        self._buckets: dict[str, _Bucket] = {}

    _MAX_BUCKETS = 50_000

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if len(self._buckets) > self._MAX_BUCKETS:
            # Drop buckets that have been idle long enough to be full again anyway.
            full_after = self._capacity / self._refill_per_second if self._refill_per_second else 3600
            self._buckets = {k: b for k, b in self._buckets.items() if now - b.last_refill < full_after}
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = _Bucket(tokens=self._capacity - 1, last_refill=now)
            return True

        elapsed = now - bucket.last_refill
        bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_per_second)
        bucket.last_refill = now

        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return True
        return False
