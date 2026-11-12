"""Shared public REST pacing inside one process; bounded foreground priority."""
import asyncio
from collections import deque
from time import monotonic

from ccxt.base.errors import RateLimitExceeded


class RestBudget:
    def __init__(self, interval=.5, cooldown=30, clock=monotonic, sleep=asyncio.sleep):
        self.interval, self.cooldown = interval, cooldown
        self.clock, self.sleep = clock, sleep
        self.waiters = {False: deque(), True: deque()}
        self.changed = asyncio.Event()
        self.foreground_streak = 0
        self.foreground_burst = 4
        self.sent = {False: 0, True: 0}
        self.next_start = self.blocked_until = 0
        self.requests = self.rate_limits = 0

    def notify(self):
        self.changed.set()
        self.changed = asyncio.Event()

    def head(self):
        foreground, background = self.waiters[False], self.waiters[True]
        if background and (not foreground or self.foreground_streak >= self.foreground_burst):
            return background[0]
        return foreground[0] if foreground else None

    async def wait(self, background=False):
        ticket = object()
        queue = self.waiters[background]
        queue.append(ticket)
        self.notify()
        granted = False
        try:
            while True:
                event = self.changed
                if self.head() is not ticket:
                    await event.wait()
                    continue
                delay = max(self.next_start, self.blocked_until)-self.clock()
                if delay <= 0:
                    queue.popleft()
                    granted = True
                    self.next_start = self.clock()+self.interval
                    self.requests += 1
                    self.sent[background] += 1
                    self.foreground_streak = 0 if background else min(self.foreground_burst, self.foreground_streak+1)
                    self.notify()
                    return
                # A foreground arrival or cooldown can change the selected head
                # while it sleeps. Never reserve a future slot for background.
                timer = asyncio.create_task(self.sleep(delay))
                changed = asyncio.create_task(event.wait())
                try:
                    await asyncio.wait((timer, changed), return_when=asyncio.FIRST_COMPLETED)
                    if timer.done(): timer.result()
                finally:
                    for task in (timer, changed):
                        if not task.done(): task.cancel()
                    await asyncio.gather(timer, changed, return_exceptions=True)
        finally:
            if not granted:
                queue.remove(ticket)
                self.notify()

    def limited(self):
        self.rate_limits += 1
        self.blocked_until = max(self.blocked_until, self.clock()+self.cooldown)
        self.notify()


def guard(client, budget, background=False):
    """Apply shared per-venue pacing alongside CCXT's built-in throttler."""
    if getattr(client, '_arbitrage_rest_guarded', False):
        return client
    original = client.fetch

    async def fetch(*args, **kwargs):
        await budget.wait(background=background)
        try:
            return await original(*args, **kwargs)
        except RateLimitExceeded:
            budget.limited()
            raise

    client.fetch = fetch
    client._arbitrage_rest_guarded = True
    return client
