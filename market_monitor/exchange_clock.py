"""Estimate quote timestamp offset from public exchange clock requests."""

import asyncio
from statistics import median
from time import monotonic

from .market import now_ms, numeric


CLOCK_REFRESH_MS = 300_000
CLOCK_MAX_AGE_MS = 3 * CLOCK_REFRESH_MS
MAX_RTT_MS = 250


class ExchangeClock:
    def __init__(self):
        self.offset = None
        self.checked_at = None
        self.due = 0

    async def measure(self, fetch_time):
        self.due = now_ms() + CLOCK_REFRESH_MS
        offsets = []
        for _ in range(3):
            before, started = now_ms(), monotonic()
            try:
                async with asyncio.timeout(5):
                    remote = numeric(await fetch_time())
            except (TimeoutError, OSError):
                continue
            after = now_ms()
            elapsed = (monotonic() - started) * 1000
            if (remote is not None and 0 <= after - before <= MAX_RTT_MS and elapsed <= MAX_RTT_MS
                    and abs(elapsed - (after - before)) <= 25 and abs(remote - after) < 60_000):
                offsets.append(remote - (before + after) / 2)
            # Allow a throttled CCXT request to acquire its venue's next slot.
            await asyncio.sleep(.55)
        if len(offsets) >= 2 and max(offsets) - min(offsets) <= MAX_RTT_MS:
            self.offset = round(median(offsets))
            self.checked_at = now_ms()

    def estimate(self, now):
        if self.checked_at is not None and 0 <= now - self.checked_at <= CLOCK_MAX_AGE_MS:
            return self.offset
        return None

    def correct(self, stamp, now):
        if stamp is None:
            return None
        return stamp - (self.estimate(now) or 0)
