"""Bounded, reconnecting public futures best-bid/ask streams."""

import asyncio
from contextlib import AsyncExitStack
import gzip
import json
import logging

import aiohttp

from .futures_ws_protocols import PROTOCOLS
from .market import fresh, now_ms
from .ws_reconnect import CONNECT_TIMEOUT, ReconnectingFeed


MEXC_URL = PROTOCOLS['mexc'].url
KUCOIN_TOKEN_URL = 'https://api-futures.kucoin.com/api/v1/bullet-public'
QUOTE_AGE = 3000
LOG = logging.getLogger(__name__)

PILOT_CONTRACTS = {
    'BTC': ('BTC_USDT', 'XBTUSDTM'),
    'INIT': ('INIT_USDT', 'INITUSDTM'),
    'ARIA': ('ARIA_USDT', 'ARIAUSDTM'),
    'JCT': ('JCT_USDT', 'JCTUSDTM'),
    'AGT': ('AGT_USDT', 'AGTUSDTM'),
}
PILOT_VENUES = ('mexc', 'kucoinfutures')
FUTURES_WS_VENUES = tuple(PROTOCOLS)


class FuturesBookFeed(ReconnectingFeed):
    def __init__(self, venue, on_disconnect, *, clock=None, on_update=None):
        super().__init__()
        self.venue = venue
        self.protocol = PROTOCOLS[venue]
        self.clock = clock
        self.on_disconnect = on_disconnect
        self.on_update = on_update
        self.symbols = frozenset()
        self.quotes = {}
        self.watermarks = {}

    def start(self, session, symbols):
        if not symbols:
            self.symbols = frozenset()
            self.quotes.clear()
            if self.reader is not None:
                self.reader.cancel()
        elif self.reader is None or self.reader.done():
            self.symbols = frozenset(symbols)
            self.start_reader(session)

    def consume(self, message, received):
        if isinstance(message, dict):
            for update in self.protocol.updates(message):
                self.apply_update(update, received)

    def apply_update(self, update, received):
        symbol, stamp, sequence, bid, ask, reset = update
        if (not isinstance(symbol, str) or symbol not in self.symbols or sequence is None
                or sequence < 0 or stamp is None):
            return
        source_at = self.clock.correct(stamp) if self.clock is not None else stamp
        if not fresh(source_at, received, QUOTE_AGE):
            return
        previous = self.watermarks.get(symbol)
        if previous:
            version, timestamp = previous
            if stamp < timestamp or (version is None and stamp == timestamp):
                return
            if version is not None:
                if sequence < version and not (reset and stamp > timestamp):
                    return
                if sequence == version and (not self.protocol.repeated_snapshots or stamp == timestamp):
                    return
        self.watermarks[symbol] = sequence, stamp
        if not (bid and ask and 0 < bid <= ask):
            removed = self.quotes.pop(symbol, None)
            if removed is not None and self.on_update is not None:
                self.on_update()
            return
        quote = dict(bid=bid, ask=ask, sourceAt=source_at, receivedAt=received, quoteSource='ws')
        if source_at != stamp:
            quote['sourceAtRaw'] = stamp
        self.quotes[symbol] = quote
        if self.on_update is not None:
            self.on_update()

    def quote(self, symbol, now):
        quote = self.quotes.get(symbol)
        if quote and all(fresh(quote[field], now, QUOTE_AGE) for field in ('sourceAt', 'receivedAt')):
            return quote
        return None

    async def connection(self, session):
        if self.venue == 'mexc':
            return MEXC_URL, {}, self.protocol.ping_interval
        if self.venue != 'kucoinfutures':
            return self.protocol.url, {}, self.protocol.ping_interval
        async with session.post(KUCOIN_TOKEN_URL) as response:
            response.raise_for_status()
            body = await response.json()
        data = body['data']
        server = data['instanceServers'][0]
        interval = max(4, server['pingInterval'] / 1000 - 3)
        return server['endpoint'], {'token': data['token'], 'connectId': str(now_ms())}, interval

    async def read(self, session):
        try:
            async with AsyncExitStack() as connections:
                try:
                    async with asyncio.timeout(CONNECT_TIMEOUT):
                        url, params, interval = await self.connection(session)
                        timeout = aiohttp.ClientWSTimeout(ws_close=2)
                        headers = {'X-Gate-Size-Decimal': '1'} if self.protocol.decimal_sizes else None
                        socket = await connections.enter_async_context(
                            session.ws_connect(url, params=params, headers=headers, timeout=timeout, heartbeat=25),
                        )
                        for message in self.protocol.subscribe(sorted(self.symbols)):
                            await socket.send_json(message)
                    self.mark_connected()
                    await self.read_quotes(socket, interval)
                finally:
                    self.connected = False
                    self.quotes.clear()
                    # Exchange sequence numbers may restart after reconnection.
                    self.watermarks = {symbol: (None, stamp) for symbol, (_, stamp) in self.watermarks.items()}
                    self.on_disconnect()
        except (aiohttp.ClientError, TimeoutError, OSError, RuntimeError, KeyError, IndexError, TypeError, ValueError) as exc:
            # Avoid logging the KuCoin public connection token from a failed handshake URL.
            LOG.warning('%s Futures WebSocket: %s', self.venue, type(exc).__name__)

    async def read_quotes(self, socket, interval):
        clock = asyncio.get_running_loop().time
        next_ping = clock() + interval if self.protocol.ping else None
        pong_due = None
        while True:
            now = clock()
            if pong_due is not None and now >= pong_due:
                raise TimeoutError('missing WebSocket pong')
            if next_ping is not None and now >= next_ping:
                ping = self.protocol.ping()
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    if isinstance(ping, str):
                        await socket.send_str(ping)
                    else:
                        await socket.send_json(ping)
                next_ping = now + interval
                pong_due = now + min(interval, 10)
            try:
                async with asyncio.timeout_at(pong_due if pong_due is not None else next_ping):
                    message = await socket.receive()
            except TimeoutError:
                continue
            if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return
            if message.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                continue
            payload = message.data
            try:
                if isinstance(payload, bytes) and payload.startswith(bytes((31, 139))):
                    payload = gzip.decompress(payload)
                if payload in ('pong', b'pong'):
                    pong_due = None
                    continue
                data = json.loads(payload)
            except (ValueError, OSError, EOFError):
                continue
            if not isinstance(data, dict):
                continue
            if any(data.get(field) == 'pong' for field in ('channel', 'type', 'op', 'ret_msg')):
                pong_due = None
            elif (data.get('channel') == 'rs.error' or data.get('type') == 'error' or data.get('event') == 'error'
                  or data.get('success') is False or data.get('error') is not None or ('code' in data and 'msg' in data)):
                raise ValueError('public subscription rejected')
            else:
                self.consume(data, now_ms())
