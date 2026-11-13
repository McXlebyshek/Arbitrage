"""Bounded, reconnecting public futures best-bid/ask streams."""

import asyncio
import gzip
import json
import logging
from contextlib import AsyncExitStack
from itertools import count

import aiohttp

from .futures_ws_protocols import PROTOCOLS
from .market import fresh, now_ms
from .thresholds import FUTURES_QUOTE_CONFIRMATION_AGE_MS
from .ws_reconnect import CONNECT_TIMEOUT, ReconnectingFeed

MEXC_URL = PROTOCOLS['mexc'].url
KUCOIN_TOKEN_URL = 'https://api-futures.kucoin.com/api/v1/bullet-public'
CONNECTION_ACTIVITY_AGE = 45_000
QUIET_SYMBOL_AGE = 30_000
QUIET_REST_VENUES = frozenset({'binanceusdm', 'gate'})
QUIET_BULK_VENUES = frozenset({'binanceusdm', 'gate', 'kucoinfutures'})
LOG = logging.getLogger(__name__)

FUTURES_WS_VENUES = tuple(PROTOCOLS)


class FuturesBookFeed(ReconnectingFeed):
    def __init__(self, venue, on_disconnect, *, clock=None, on_update=None):
        super().__init__()
        self.venue = venue
        self.protocol = PROTOCOLS[venue]
        self.clock = clock
        self.on_disconnect = on_disconnect
        self.on_update = on_update
        self._subscriptions: dict[str, object] = {}
        self._wire_subscriptions: dict[str, object] = {}
        self._subscriptions_changed = asyncio.Event()
        self._pending_ack: tuple[int | str, str, asyncio.Future[int]] | None = None
        self.quotes = {}
        self.watermarks = {}
        self.last_message_at = None
        self.subscription_ack_at: dict[str, int] = {}
        self.session_number = 0

    @property
    def symbols(self):
        return frozenset(self._subscriptions)

    def mark_connected(self):
        super().mark_connected()
        self.session_number += 1
        self.last_message_at = None
        self.subscription_ack_at.clear()

    def start(self, session, symbols):
        symbols = frozenset(symbols)
        if self._subscriptions.keys() != symbols:
            removed = self._subscriptions.keys() - symbols
            for symbol in removed:
                del self._subscriptions[symbol]
                self.quotes.pop(symbol, None)
                self.watermarks.pop(symbol, None)
                self.subscription_ack_at.pop(symbol, None)
            for symbol in symbols - self._subscriptions.keys():
                self._subscriptions[symbol] = object()
            self._subscriptions_changed.set()
            if removed and self.on_update is not None:
                self.on_update()
        if symbols:
            if self.reader is not None and not self.reader.done() and self.reader.cancelling():
                self.reader.add_done_callback(lambda finished: self._resume(finished, session))
            else:
                self.start_reader(session)
        elif not self.connected and self.reader is not None:
            self.reader.cancel()

    def _resume(self, finished, session):
        if self.reader is finished and self._subscriptions:
            self.start_reader(session)

    async def close(self):
        self._subscriptions.clear()
        await super().close()

    async def sync_subscriptions(self, socket):
        request_ids = count(1)
        while True:
            self._subscriptions_changed.clear()
            removed = sorted(symbol for symbol, generation in self._wire_subscriptions.items()
                             if self._subscriptions.get(symbol) is not generation)
            for symbol in removed:
                del self._wire_subscriptions[symbol]
            if removed:
                await self.send_subscription_commands(socket, removed, 'unsubscribe', request_ids)

            added = {symbol: generation for symbol, generation in self._subscriptions.items()
                     if symbol not in self._wire_subscriptions}
            if added:
                self._wire_subscriptions.update(added)
                acknowledged = await self.send_subscription_commands(socket, sorted(added), 'subscribe', request_ids)
                if acknowledged is not None:
                    for symbol, generation in added.items():
                        if self._subscriptions.get(symbol) is generation:
                            self.subscription_ack_at[symbol] = acknowledged
            await self._subscriptions_changed.wait()

    async def send_subscription_commands(self, socket, symbols, operation, request_ids):
        acknowledged = None
        commands = self.protocol.subscribe if operation == 'subscribe' else self.protocol.unsubscribe
        async with asyncio.timeout(CONNECT_TIMEOUT):
            for message in commands(symbols):
                if self.protocol.request_id is not None:
                    message['id'] = self.protocol.request_id(next(request_ids))
                if self.protocol.subscription_ack is None:
                    await socket.send_json(message)
                    continue
                confirmation = asyncio.get_running_loop().create_future()
                self._pending_ack = message['id'], operation, confirmation
                try:
                    await socket.send_json(message)
                    acknowledged = await confirmation
                finally:
                    self._pending_ack = None
        return acknowledged

    async def run_socket(self, socket, interval):
        tasks = (
            asyncio.create_task(self.read_quotes(socket, interval)),
            asyncio.create_task(self.sync_subscriptions(socket)),
        )
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def consume(self, message, received):
        if self.connected and isinstance(message, dict):
            self.last_message_at = received
            check_ack = self.protocol.subscription_ack
            if check_ack is not None and self._pending_ack is not None:
                request_id, operation, confirmation = self._pending_ack
                if message.get('id') == request_id and check_ack(message, operation):
                    if not confirmation.done():
                        confirmation.set_result(received)
                    return
            for update in self.protocol.updates(message):
                self.apply_update(update, received)

    def apply_update(self, update, received):
        symbol, stamp, sequence, bid, ask, reset = update
        # Check timestamp units and stream ordering without aging the book by exchange time.
        if (not isinstance(symbol, str) or symbol not in self._subscriptions
                or self._wire_subscriptions.get(symbol) is not self._subscriptions[symbol] or sequence is None
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
                if sequence == version and not (repeated or (self.protocol.repeated_snapshots and stamp > timestamp)):
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
        if (quote is not None and symbol in self._subscriptions and self.active(now)
                and fresh(quote['confirmedAt'], now, FUTURES_QUOTE_CONFIRMATION_AGE_MS)):
            return dict(quote, validUntil=min(
                quote['confirmedAt'] + FUTURES_QUOTE_CONFIRMATION_AGE_MS,
                self.last_message_at + CONNECTION_ACTIVITY_AGE,
            ))
        return None

    def quiet_symbols(self, now):
        if self.venue not in QUIET_BULK_VENUES or not self.active(now):
            return []
        due = []
        for symbol in self._subscriptions:
            quote = self.quotes.get(symbol)
            if quote is not None:
                confirmed_at = quote['confirmedAt']
            elif symbol in self.subscription_ack_at:
                watermark = self.watermarks.get(symbol)
                confirmed_at = self.subscription_ack_at[symbol]
                if watermark is not None and watermark[0] is not None:
                    confirmed_at = watermark[4]
            else:
                continue
            if now - confirmed_at >= QUIET_SYMBOL_AGE:
                due.append((confirmed_at, symbol))
        return [symbol for _, symbol in sorted(due)]

    def rest_token(self, symbol):
        return (self.session_number, symbol, self._subscriptions.get(symbol), self.quotes.get(symbol),
                self.watermarks.get(symbol))

    def confirm_rest(self, symbol, token, bid, ask, received, *, changed_at=None):
        return self._confirm_external(symbol, token, bid, ask, received, changed_at=changed_at,
                                      quote_source='rest')

    def confirm_bulk(self, symbol, token, bid, ask, received, *, changed_at=None):
        return self._confirm_external(symbol, token, bid, ask, received, changed_at=changed_at,
                                      quote_source='bulk')

    def _confirm_external(self, symbol, token, bid, ask, received, *, changed_at, quote_source):
        session_number, requested_symbol, generation, expected, watermark = token
        if (self.venue not in QUIET_BULK_VENUES or generation is None or not self.active(received)
                or session_number != self.session_number or requested_symbol != symbol
                or self._subscriptions.get(symbol) is not generation or self.quotes.get(symbol) is not expected
                or self.watermarks.get(symbol) is not watermark or not (bid and ask and 0 < bid <= ask)):
            return None
        if expected is None:
            acknowledged = self.subscription_ack_at.get(symbol)
            if acknowledged is None or received < acknowledged:
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
                         receivedAt=received, confirmedAt=received, quoteSource=quote_source)
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
        if not self._subscriptions:
            raise asyncio.CancelledError
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
                    self.mark_connected()
                    await self.run_socket(socket, interval)
                finally:
                    self.connected = False
                    self.last_message_at = None
                    self._wire_subscriptions.clear()
                    self.subscription_ack_at.clear()
                    self.quotes.clear()
                    # Exchange sequence numbers may restart after reconnection.
                    self.watermarks = {symbol: (None, *previous[1:]) for symbol, previous in self.watermarks.items()}
                    self.on_disconnect()
        except (aiohttp.ClientError, TimeoutError, OSError, RuntimeError,
                KeyError, IndexError, TypeError, ValueError) as exc:
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
            elif (data.get('channel') == 'rs.error' or data.get('type') == 'error'
                  or data.get('event') == 'error' or data.get('success') is False
                  or data.get('error') is not None or ('code' in data and 'msg' in data)):
                raise ValueError('public subscription rejected')
            else:
                self.consume(data, received)
