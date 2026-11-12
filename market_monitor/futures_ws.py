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
QUOTE_CONFIRMATION_AGE = 90_000
CONNECTION_ACTIVITY_AGE = 45_000
QUIET_SYMBOL_AGE = 30_000
QUIET_REST_VENUES = frozenset({'binanceusdm', 'gate'})
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
        self.last_message_at = None
        self.subscription_ack_at = None
        self.session_number = 0

    def mark_connected(self):
        super().mark_connected()
        self.session_number += 1
        self.last_message_at = None
        self.subscription_ack_at = None

    def start(self, session, symbols):
        if not symbols:
            self.symbols = frozenset()
            self.quotes.clear()
            self.last_message_at = None
            self.subscription_ack_at = None
            if self.reader is not None:
                self.reader.cancel()
        elif self.reader is None or self.reader.done():
            self.symbols = frozenset(symbols)
            self.start_reader(session)

    def consume(self, message, received):
        if self.connected and isinstance(message, dict):
            self.last_message_at = received
            if self.protocol.subscription_ack is not None and self.protocol.subscription_ack(message):
                self.subscription_ack_at = received
            for update in self.protocol.updates(message):
                self.apply_update(update, received)

    def apply_update(self, update, received):
        symbol, stamp, sequence, bid, ask, reset = update
        # Check timestamp units and stream ordering without aging the book by exchange time.
        if (not isinstance(symbol, str) or symbol not in self.symbols or sequence is None
                or sequence < 0 or stamp is None or not 10**12 <= stamp < 10**13):
            return
        previous = self.watermarks.get(symbol)
        if previous is not None:
            version, timestamp, previous_bid, previous_ask, _ = previous
            repeated = self.protocol.repeated_snapshots and (bid, ask) == (previous_bid, previous_ask)
            if stamp < timestamp or (version is None and stamp == timestamp and not repeated):
                return
            if version is not None:
                if sequence < version and not (reset and stamp > timestamp):
                    return
                if sequence == version and not (repeated or self.protocol.repeated_snapshots and stamp > timestamp):
                    return
        self.watermarks[symbol] = sequence, stamp, bid, ask, received
        if not (bid and ask and 0 < bid <= ask):
            removed = self.quotes.pop(symbol, None)
            if removed is not None and self.on_update is not None:
                self.on_update()
            return
        current = self.quotes.get(symbol)
        if current is not None and current['quoteSource'] == 'ws' and (bid, ask) == (current['bid'], current['ask']):
            quote = dict(current, receivedAt=received, confirmedAt=received)
        else:
            changed_at = self.clock.correct(stamp) if self.clock is not None else stamp
            quote = dict(bid=bid, ask=ask, changedAt=changed_at, sourceAt=changed_at,
                         receivedAt=received, confirmedAt=received, quoteSource='ws')
            if changed_at != stamp:
                quote['changedAtRaw'] = quote['sourceAtRaw'] = stamp
        self.quotes[symbol] = quote
        if self.on_update is not None:
            self.on_update()

    def active(self, now):
        return self.connected and fresh(self.last_message_at, now, CONNECTION_ACTIVITY_AGE)

    def quote(self, symbol, now):
        quote = self.quotes.get(symbol)
        if (quote is not None and symbol in self.symbols and self.active(now)
                and fresh(quote['confirmedAt'], now, QUOTE_CONFIRMATION_AGE)):
            return dict(quote, validUntil=min(
                quote['confirmedAt'] + QUOTE_CONFIRMATION_AGE,
                self.last_message_at + CONNECTION_ACTIVITY_AGE,
            ))
        return None

    def quiet_symbols(self, now):
        if self.venue not in QUIET_REST_VENUES or not self.active(now):
            return []
        due = []
        for symbol in self.symbols:
            quote = self.quotes.get(symbol)
            if quote is not None:
                confirmed_at = quote['confirmedAt']
            elif self.subscription_ack_at is not None:
                watermark = self.watermarks.get(symbol)
                confirmed_at = self.subscription_ack_at
                if watermark is not None and watermark[0] is not None:
                    confirmed_at = watermark[4]
            else:
                continue
            if now - confirmed_at >= QUIET_SYMBOL_AGE:
                due.append((confirmed_at, symbol))
        return [symbol for _, symbol in sorted(due)]

    def rest_token(self, symbol):
        return self.session_number, symbol, self.quotes.get(symbol), self.watermarks.get(symbol)

    def confirm_rest(self, symbol, token, bid, ask, received, *, changed_at=None):
        session_number, requested_symbol, expected, watermark = token
        if (self.venue not in QUIET_REST_VENUES or symbol not in self.symbols or not self.active(received)
                or session_number != self.session_number or requested_symbol != symbol or self.quotes.get(symbol) is not expected
                or self.watermarks.get(symbol) is not watermark or not (bid and ask and 0 < bid <= ask)):
            return None
        if expected is None:
            if self.subscription_ack_at is None or received < self.subscription_ack_at:
                return None
        elif received < expected['confirmedAt']:
            return None
        matches = expected is not None and (bid, ask) == (expected['bid'], expected['ask'])
        if matches:
            quote = dict(expected, confirmedAt=received)
            if expected['quoteSource'] == 'rest':
                quote['receivedAt'] = received
        else:
            quote = dict(bid=bid, ask=ask, changedAt=changed_at, sourceAt=changed_at,
                         receivedAt=received, confirmedAt=received, quoteSource='rest')
            if expected is not None:
                quote['suspect'] = True
        self.quotes[symbol] = quote
        if self.on_update is not None:
            self.on_update()
        return matches

    def heartbeat_received(self, received):
        self.last_message_at = received
        if self.quotes and self.on_update is not None:
            self.on_update()

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
                            session.ws_connect(url, params=params, headers=headers, timeout=timeout, heartbeat=25,
                                               autoping=False),
                        )
                        for message in self.protocol.subscribe(sorted(self.symbols)):
                            await socket.send_json(message)
                    self.mark_connected()
                    await self.read_quotes(socket, interval)
                finally:
                    self.connected = False
                    self.last_message_at = None
                    self.subscription_ack_at = None
                    self.quotes.clear()
                    # Exchange sequence numbers may restart after reconnection.
                    self.watermarks = {symbol: (None, *previous[1:]) for symbol, previous in self.watermarks.items()}
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
            if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                return
            received = now_ms()
            self.last_message_at = received
            if message.type == aiohttp.WSMsgType.PING:
                self.heartbeat_received(received)
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    await socket.pong(message.data)
                continue
            if message.type == aiohttp.WSMsgType.PONG:
                self.heartbeat_received(received)
                continue
            if message.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                continue
            payload = message.data
            try:
                if isinstance(payload, bytes) and payload.startswith(bytes((31, 139))):
                    payload = gzip.decompress(payload)
                if payload in ('pong', b'pong'):
                    pong_due = None
                    self.heartbeat_received(received)
                    continue
                data = json.loads(payload)
            except (ValueError, OSError, EOFError):
                continue
            if not isinstance(data, dict):
                continue
            if any(data.get(field) == 'pong' for field in ('channel', 'type', 'op', 'ret_msg')):
                pong_due = None
                self.heartbeat_received(received)
            elif (data.get('channel') == 'rs.error' or data.get('type') == 'error' or data.get('event') == 'error'
                  or data.get('success') is False or data.get('error') is not None or ('code' in data and 'msg' in data)):
                raise ValueError('public subscription rejected')
            else:
                self.consume(data, received)
