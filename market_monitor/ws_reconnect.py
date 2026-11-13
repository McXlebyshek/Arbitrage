"""Shared lifecycle for long-running public WebSocket feeds."""

import asyncio
from contextlib import suppress
from random import uniform
from time import monotonic

MAX_BACKOFF = 30.0
HEALTHY_SESSION = 30.0
CONNECT_TIMEOUT = 15.0


def retry_delay(backoff):
    return min(MAX_BACKOFF, backoff * uniform(0.8, 1.2))


class ReconnectingFeed:
    def __init__(self):
        self.reader = None
        self.connected = False
        self.reconnects = 0
        self.connected_at = None

    def start_reader(self, *args):
        if self.reader is None or self.reader.done():
            self.reader = asyncio.create_task(self.run_reader(*args))

    def mark_connected(self):
        self.connected = True
        self.connected_at = monotonic()

    async def run_reader(self, *args):
        backoff = 1.0
        while True:
            self.connected_at = None
            await self.read(*args)
            if self.connected_at is not None and monotonic() - self.connected_at >= HEALTHY_SESSION:
                backoff = 1.0
            await asyncio.sleep(retry_delay(backoff))
            self.reconnects += 1
            backoff = min(MAX_BACKOFF, backoff * 2)

    async def close(self):
        reader = self.reader
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
            if self.reader is reader:
                self.reader = None
