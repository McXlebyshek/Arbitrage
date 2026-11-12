"""Bounded public order-book subscriptions for the MEXC and KuCoin pilot."""

import asyncio
from contextlib import suppress
import gzip
import json
import logging
from random import uniform
from time import monotonic

import aiohttp

from .market import fresh, now_ms, numeric


MEXC_URL = 'wss://contract.mexc.com/edge'
KUCOIN_TOKEN_URL = 'https://api-futures.kucoin.com/api/v1/bullet-public'
QUOTE_AGE = 3000
LOG = logging.getLogger(__name__)

PILOT_VENUES = ('mexc', 'kucoinfutures')


class FuturesBookFeed:
    def __init__(self, venue, on_disconnect, clock=None):
        self.venue = venue
        self.on_disconnect = on_disconnect
        self.clock = clock
        self.reader = None
        self.symbols = frozenset()
        self.quotes = {}
        self.watermarks = {}
        self.reconnects = 0

    def start(self, session, symbols):
        if symbols and (self.reader is None or self.reader.done()):
            self.symbols = frozenset(symbols)
            self.reader = asyncio.create_task(self.run(session))

    def consume(self, message, received):
        channel = message.get('channel') or message.get('subject')
        data = message.get('data')
        if not isinstance(data, dict):
            return
        if self.venue == 'mexc' and channel == 'push.depth.full':
            symbol, stamp = message.get('symbol'), message.get('ts')
            sequence = data.get('version')
            bid, ask = self.best(data.get('bids'), max), self.best(data.get('asks'), min)
        elif self.venue == 'kucoinfutures' and channel == 'tickerV2':
            symbol, ns, sequence = data.get('symbol'), data.get('ts'), data.get('sequence')
            stamp = ns // 1_000_000 if type(ns) is int else None
            bid = numeric(data.get('bestBidPrice')) if (numeric(data.get('bestBidSize')) or 0) > 0 else None
            ask = numeric(data.get('bestAskPrice')) if (numeric(data.get('bestAskSize')) or 0) > 0 else None
        else:
            return
        if (not isinstance(symbol, str) or symbol not in self.symbols or type(sequence) is not int
                or sequence < 0 or type(stamp) is not int):
            return
        corrected = self.clock.correct(stamp, received) if self.clock else stamp
        if not fresh(corrected, received, QUOTE_AGE):
            return
        previous = self.watermarks.get(symbol)
        if previous:
            version, timestamp = previous
            if (sequence < version or stamp < timestamp
                    or sequence == version and (self.venue != 'mexc' or stamp == timestamp)):
                return
        self.watermarks[symbol] = sequence, stamp
        if not (bid and ask and 0 < bid <= ask):
            self.quotes.pop(symbol, None)
            return
        self.quotes[symbol] = dict(bid=bid, ask=ask, sourceAt=corrected, receivedAt=received, quoteSource='ws')

    @staticmethod
    def best(levels, choose):
        prices = []
        for level in levels if isinstance(levels, list) else ():
            if isinstance(level, list) and len(level) >= 2:
                price, size = map(numeric, level[:2])
                if price and size and price > 0 and size > 0:
                    prices.append(price)
        return choose(prices, default=None)

    def quote(self, symbol, now):
        quote = self.quotes.get(symbol)
        if quote and all(fresh(quote[field], now, QUOTE_AGE) for field in ('sourceAt', 'receivedAt')):
            return quote
        return None

    async def connection(self, session):
        if self.venue == 'mexc':
            return MEXC_URL, {}, 14
        async with session.post(KUCOIN_TOKEN_URL) as response:
            response.raise_for_status()
            body = await response.json()
        data = body['data']
        server = data['instanceServers'][0]
        interval = max(4, server['pingInterval'] / 1000 - 3)
        return server['endpoint'], {'token': data['token'], 'connectId': str(now_ms())}, interval

    async def run(self, session):
        delay = 1
        while True:
            started = monotonic()
            await self.read(session)
            if monotonic() - started >= 30:
                delay = 1
            await asyncio.sleep(uniform(delay * .8, min(30, delay * 1.2)))
            self.reconnects += 1
            delay = min(delay * 2, 30)

    async def read(self, session):
        try:
            url, params, interval = await self.connection(session)
            timeout = aiohttp.ClientWSTimeout(ws_receive=25, ws_close=2)
            async with session.ws_connect(url, params=params, timeout=timeout, heartbeat=25) as socket:
                for symbol in sorted(self.symbols):
                    message = (dict(method='sub.depth.full', param=dict(symbol=symbol, limit=5))
                               if self.venue == 'mexc' else
                               dict(id=symbol, type='subscribe', topic='/contractMarket/tickerV2:' + symbol,
                                    privateChannel=False, response=True))
                    await socket.send_json(message)
                next_ping = monotonic() + interval
                last_pong = monotonic()
                while True:
                    now = monotonic()
                    if now >= next_ping:
                        if now - last_pong > 2 * interval:
                            raise TimeoutError('missing WebSocket pong')
                        await socket.send_json(dict(method='ping') if self.venue == 'mexc'
                                               else dict(id=str(now_ms()), type='ping'))
                        next_ping = now + interval
                    try:
                        async with asyncio.timeout(max(0.01, next_ping - monotonic())):
                            message = await socket.receive()
                    except TimeoutError:
                        continue
                    if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    if message.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                        continue
                    payload = message.data
                    try:
                        if isinstance(payload, bytes) and payload.startswith(bytes((31, 139))):
                            payload = gzip.decompress(payload)
                        data = json.loads(payload)
                    except (ValueError, OSError, EOFError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    if data.get('channel') == 'pong' or data.get('type') == 'pong':
                        last_pong = monotonic()
                    elif data.get('channel') == 'rs.error' or data.get('type') == 'error':
                        raise ValueError('public subscription rejected')
                    else:
                        self.consume(data, now_ms())
        except (aiohttp.ClientError, TimeoutError, OSError, KeyError, IndexError, TypeError, ValueError) as exc:
            # Avoid logging the KuCoin public connection token from a failed handshake URL.
            LOG.warning('%s Futures WebSocket: %s', self.venue, type(exc).__name__)
        finally:
            self.quotes.clear()
            self.on_disconnect()

    async def close(self):
        if self.reader is not None:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
