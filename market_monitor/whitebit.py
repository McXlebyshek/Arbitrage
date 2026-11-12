"""WhiteBIT's public best-bid/ask snapshots for spot markets."""

import asyncio
from contextlib import suppress
import logging
from time import monotonic

import aiohttp

from .market import fresh, now_ms, numeric


BOOK_TICKER_URL = 'wss://api.whitebit.com/ws'
LOG = logging.getLogger(__name__)


class WhitebitBookTicker:
    def __init__(self, session):
        self.session = session
        self.reader = None
        self.quotes = {}

    def start(self):
        if self.reader is None or self.reader.done():
            self.reader = asyncio.create_task(self.read())

    def consume(self, updates, received):
        for update in updates:
            if not isinstance(update, list) or len(update) < 8:
                continue
            stamp, _, native, version, bid, bid_size, ask, ask_size = update[:8]
            stamp, bid, bid_size, ask, ask_size = map(numeric, (stamp, bid, bid_size, ask, ask_size))
            if not isinstance(native, str) or type(version) is not int or stamp is None:
                continue
            source_at = int(stamp * 1000)
            previous = self.quotes.get(native)
            if previous and (version <= previous[0] or source_at < previous[1]['timestamp']):
                continue
            if not (bid and ask and 0 < bid <= ask and bid_size and bid_size > 0 and ask_size and ask_size > 0):
                bid = ask = None
            self.quotes[native] = (version, dict(
                native_id=native, bid=bid, ask=ask, timestamp=source_at, quote_received_at_ms=received,
            ))

    def snapshot(self, markets, now):
        result = {}
        for market in markets.values():
            if not (market.get('spot') and market.get('active') and market.get('quote') in ('USDT', 'USDC')):
                continue
            item = self.quotes.get(market['id'])
            if item:
                quote = item[1]
                if quote['bid'] is not None and fresh(quote['timestamp'], now) and fresh(quote['quote_received_at_ms'], now):
                    result[market['symbol']] = dict(quote, symbol=market['symbol'])
        return result

    async def read(self):
        try:
            timeout = aiohttp.ClientWSTimeout(ws_receive=15, ws_close=2)
            async with self.session.ws_connect(BOOK_TICKER_URL, timeout=timeout) as socket:
                # The server accepts an empty list for all markets or one native ID.
                await socket.send_json(dict(id=1, method='bookTicker_subscribe', params=[]))
                accepted, next_ping = False, monotonic() + 45
                async for message in socket:
                    if monotonic() >= next_ping:
                        await socket.send_json(dict(id=2, method='ping', params=[]))
                        next_ping = monotonic() + 45
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = message.json()
                    if data.get('id') == 1:
                        if (data.get('result') or {}).get('status') != 'success':
                            raise ValueError('WhiteBIT book ticker subscription rejected')
                        accepted = True
                    elif accepted and data.get('method') == 'bookTicker_update':
                        self.consume(data.get('params', []), now_ms())
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError, TypeError, AttributeError) as exc:
            LOG.warning('WhiteBIT Spot book ticker: %s %s', type(exc).__name__, str(exc)[:180])
        finally:
            self.quotes.clear()

    async def close(self):
        if self.reader is not None:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
