"""Public futures top-of-book subscriptions backed by the installed CCXT adapters."""

import asyncio
from collections import defaultdict

import aiohttp
import ccxt.async_support as ccxt

from .futures_ws import FuturesBookFeed, LOG, QUOTE_AGE
from .market import fresh, now_ms, numeric
from .sources import public_client_config


WIDE_VENUES = ('binanceusdm', 'bybit', 'okx', 'gate', 'bitget')


def public_ws_client(source):
    import ccxt.pro as pro

    config = public_client_config(source.venue)
    config['newUpdates'] = True
    client = getattr(pro, source.venue)(config)
    client.set_markets_from_exchange(source.client)
    return client


class UnifiedBookFeed(FuturesBookFeed):
    """The same start/quote/close interface as the native futures feeds."""

    def __init__(self, source, on_disconnect, client_factory=public_ws_client):
        super().__init__(source.venue, on_disconnect, source.clock)
        self.source = source
        self.client_factory = client_factory
        self.catalog = None

    def start(self, session, symbols):
        if self.reader is None or self.reader.done():
            self.catalog = self.source.catalogs.get(self.venue)
        super().start(session, symbols)

    def quote(self, symbol, now):
        if self.catalog is not self.source.catalogs.get(self.venue):
            return None
        return super().quote(symbol, now)

    def consume(self, updates, received):
        if not isinstance(updates, dict):
            return
        rows = [updates] if 'symbol' in updates else updates.values()
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get('symbol')
            stamp = numeric(row.get('timestamp'))
            if not isinstance(symbol, str) or symbol not in self.symbols or stamp is None:
                continue
            corrected = self.clock.correct(stamp, received)
            if not fresh(corrected, received, QUOTE_AGE) or stamp < self.watermarks.get(symbol, 0):
                continue
            self.watermarks[symbol] = stamp
            bid, ask = numeric(row.get('bid')), numeric(row.get('ask'))
            if not bid or not ask or not 0 < bid <= ask:
                self.quotes.pop(symbol, None)
                continue
            self.quotes[symbol] = dict(bid=bid, ask=ask, sourceAt=corrected, receivedAt=received, quoteSource='ws')

    async def read_group(self, client, symbols):
        while True:
            async with asyncio.timeout(20):
                result = await client.watch_bids_asks(symbols)
            self.consume(result, now_ms())

    async def read(self, _session):
        client = None
        readers = []
        try:
            client = self.client_factory(self.source)
            by_settlement = defaultdict(list)
            for symbol in sorted(self.symbols):
                by_settlement[client.markets[symbol]['settle']].append(symbol)
            readers = [asyncio.create_task(self.read_group(client, symbols))
                       for symbols in by_settlement.values()]
            await asyncio.gather(*readers)
        except (aiohttp.ClientError, ccxt.BaseError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            LOG.warning('%s Futures WebSocket: %s', self.venue, type(exc).__name__)
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            self.quotes.clear()
            self.watermarks.clear()
            self.on_disconnect()
            if client is not None:
                await client.close()
