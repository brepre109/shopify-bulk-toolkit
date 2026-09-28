"""Client-side model of Shopify's GraphQL cost bucket (a leaky bucket).

Shopify returns ``extensions.cost.throttleStatus`` on every Admin GraphQL
response::

    {"maximumAvailable": 2000.0, "currentlyAvailable": 1650, "restoreRate": 100.0}

The throttle keeps a local estimate of the bucket between responses so the
client can wait *before* sending a request that would be throttled, instead of
sending it, getting a ``THROTTLED`` error and retrying.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class CostThrottle:
    maximum: float = 1000.0  # conservative until the first response tells us the real plan limit
    available: float = 1000.0
    restore_rate: float = 50.0  # points per second
    reserve: float = 0.0  # keep this many points free for other clients sharing the app's bucket
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _updated_at: float | None = field(default=None, repr=False)
    total_waited: float = 0.0

    def _refill(self) -> float:
        now = self.clock()
        if self._updated_at is not None:
            self.available = min(self.maximum, self.available + (now - self._updated_at) * self.restore_rate)
        self._updated_at = now
        return self.available

    def wait_time(self, cost: float) -> float:
        """Seconds to wait until ``cost`` points (plus the reserve) are available."""
        needed = min(cost + self.reserve, self.maximum)
        available = self._refill()
        if available >= needed:
            return 0.0
        return (needed - available) / self.restore_rate

    def acquire(self, cost: float) -> float:
        """Block until ``cost`` points are available, then spend them locally."""
        waited = self.wait_time(cost)
        if waited > 0:
            self.sleep(waited)
            self.total_waited += waited
            self._refill()
        self.available -= cost
        return waited

    def update(self, throttle_status: dict | None) -> None:
        """Sync with the server's view after a response."""
        if not throttle_status:
            return
        self.maximum = float(throttle_status.get("maximumAvailable", self.maximum))
        self.available = float(throttle_status.get("currentlyAvailable", self.available))
        self.restore_rate = float(throttle_status.get("restoreRate", self.restore_rate)) or self.restore_rate
        self._updated_at = self.clock()
