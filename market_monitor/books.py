"""Shared short-lived cache of public order books."""

import asyncio
import logging

import aiohttp
import ccxt.async_support as ccxt

from .market import fresh, numeric


LOG = logging.getLogger(__name__)
BOOK_MAX_AGE_MS = 3000
BOOK_CONCURRENCY = 4


class BookUnavailable(Exception):
    pass


class BookBusy(Exception):
    pass


def normalized_book(book, size, received, clock=None):
    stamp = numeric(book.get('timestamp'))
    if clock is not None:
        stamp = clock.correct(stamp, received)
    if stamp is not None and not fresh(stamp, received, 3000):
        raise ValueError('стакан просрочен')
    sides = {}
    for side in ('bids', 'asks'):
        levels = []
        for row in book.get(side, [])[:50]:
            price, amount = map(numeric, row[:2])
            if price and amount and price > 0 and amount > 0:
                levels.append([price, amount * size])
        sides[side] = sorted(levels, reverse=side == 'bids')
    if not sides['bids'] or not sides['asks'] or sides['bids'][0][0] >= sides['asks'][0][0]:
        raise ValueError('пустой или пересечённый стакан')
    return dict(**sides, sourceAt=stamp, receivedAt=received)


class BookService:
    def __init__(self, resolve, clock_for, clock):
        self.resolve = resolve
        self.clock_for = clock_for
        self.clock = clock
        self.cached = {}
        self.pending = {}

    async def get(self, key):
        cached = self.cached.get(key)
        now = self.clock()
        if (cached and fresh(cached['receivedAt'], now, BOOK_MAX_AGE_MS)
                and (cached['sourceAt'] is None or fresh(cached['sourceAt'], now, BOOK_MAX_AGE_MS))):
            return cached
        task = self.pending.get(key)
        if task is None:
            if len(self.pending) >= BOOK_CONCURRENCY:
                raise BookBusy('Лимит одновременных запросов стакана')
            task = self.pending[key] = asyncio.create_task(self._load(key))
            # Retrieve failures even if all clients disconnect before completion.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(task)

    async def _load(self, key):
        try:
            source, market = self.resolve(key)
            async with asyncio.timeout(8):
                raw = await source.public_book(market)
                if raw.get('symbol') not in (None, market['symbol']):
                    raise ValueError('стакан другого инструмента')
                size = market['contractSize'] if source.kind == 'swap' else 1
                result = normalized_book(raw, size, self.clock(), self.clock_for(getattr(source, 'venue', None)))
            now = self.clock()
            self.cached = {item: value for item, value in self.cached.items()
                           if fresh(value['receivedAt'], now, BOOK_MAX_AGE_MS)}
            self.cached[key] = result
            return result
        except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            LOG.warning('Depth %s: %s', key, str(exc)[:250])
            raise BookUnavailable('Публичный стакан временно недоступен') from exc
        finally:
            self.pending.pop(key, None)

    async def close(self):
        pending = tuple(self.pending.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
