import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from market_monitor.futures_ws import FuturesBookFeed
from market_monitor.futures_ws_protocols import PROTOCOLS
from market_monitor.market import now_ms


STAMP = 1_796_370_000_000
SYMBOLS = {'binanceusdm': 'BTCUSDT', 'bybit': 'BTCUSDT', 'okx': 'BTC-USDT-SWAP',
           'gate': 'BTC_USDT', 'bitget': 'BTCUSDT'}


def frame(venue, stamp, *, size='0.25', sequence=9007199254740997):
    symbol = SYMBOLS[venue]
    bids, asks = [['100', size]], [['101', '3']]
    if venue == 'binanceusdm':
        return dict(e='bookTicker', T=stamp, E=stamp + 2, st=1, u=sequence,
                    s=symbol, b='100', B=size, a='101', A='3')
    if venue == 'bybit':
        return dict(type='snapshot', topic=f'orderbook.1.{symbol}', ts=str(stamp + 2), cts=stamp,
                    data=dict(s=symbol, b=bids, a=asks, u=sequence))
    if venue == 'okx':
        return dict(arg=dict(channel='bbo-tbt', instId=symbol), data=[dict(
            bids=bids, asks=asks, ts=str(stamp), seqId=sequence,
        )])
    if venue == 'gate':
        return dict(channel='futures.book_ticker', event='update', time_ms=stamp + 2,
                    result=dict(s=symbol, t=stamp, u=str(sequence), b='100', B=size, a='101', A='3'))
    return dict(action='snapshot', ts=stamp + 2, arg=dict(instType='usdt-futures', topic='books1', symbol=symbol),
                data=[dict(b=bids, a=asks, ts=str(stamp), seq=str(sequence))])


class PublicBookAdapterTests(unittest.TestCase):
    def test_only_valid_native_instrument_quotes_and_millisecond_engine_stamps(self):
        for venue, symbol in SYMBOLS.items():
            with self.subTest(venue=venue):
                feed = FuturesBookFeed(venue, lambda: None)
                feed.consume(frame(venue, STAMP), STAMP + 5)
                self.assertIsNone(feed.quote(symbol, STAMP + 5))
                feed.symbols = frozenset({symbol})
                feed.consume(frame(venue, STAMP * 1_000), STAMP + 5)
                self.assertIsNone(feed.quote(symbol, STAMP + 5))
                feed.consume(frame(venue, STAMP), STAMP + 5)
                self.assertEqual(feed.quote(symbol, STAMP + 5), dict(
                    bid=100, ask=101, sourceAt=STAMP, receivedAt=STAMP + 5, quoteSource='ws',
                ))
                feed.consume(frame(venue, STAMP - 1, size='0', sequence=9007199254740998), STAMP + 5)
                self.assertIsNotNone(feed.quote(symbol, STAMP + 5))
                self.assertIsNone(feed.quote(symbol, STAMP + 3001))
                feed.consume(frame(venue, STAMP + 1, size='0', sequence=9007199254740998), STAMP + 5)
                self.assertIsNone(feed.quote(symbol, STAMP + 5))

    def test_snapshot_can_renew_and_bybit_engine_restart_can_reset_sequence(self):
        feed = FuturesBookFeed('bybit', lambda: None)
        feed.symbols = frozenset({'BTCUSDT'})
        feed.consume(frame('bybit', STAMP), STAMP)
        feed.consume(frame('bybit', STAMP + 1), STAMP + 1)
        self.assertEqual(feed.quote('BTCUSDT', STAMP + 1)['sourceAt'], STAMP + 1)
        feed.consume(frame('bybit', STAMP + 2, sequence=1), STAMP + 2)
        self.assertEqual(feed.quote('BTCUSDT', STAMP + 2)['sourceAt'], STAMP + 2)

    def test_binance_excludes_coin_m_and_bitget_excludes_other_products(self):
        for venue in ('binanceusdm', 'bitget'):
            feed = FuturesBookFeed(venue, lambda: None)
            feed.symbols = frozenset({'BTCUSDT'})
            message = frame(venue, STAMP)
            if venue == 'binanceusdm':
                message['st'] = 2
            else:
                message['arg']['instType'] = 'usdc-futures'
            feed.consume(message, STAMP)
            self.assertIsNone(feed.quote('BTCUSDT', STAMP))

    def test_calibration_changes_freshness_without_changing_exchange_order(self):
        clock = Mock(correct=lambda stamp: stamp + 5000)
        feed = FuturesBookFeed('binanceusdm', lambda: None, clock=clock)
        feed.symbols = frozenset({'BTCUSDT'})
        feed.consume(frame('binanceusdm', STAMP), STAMP + 5000)
        quote = feed.quote('BTCUSDT', STAMP + 5000)
        self.assertEqual(quote['sourceAt'], STAMP + 5000)
        self.assertEqual(quote['sourceAtRaw'], STAMP)
        clock.correct = lambda stamp: stamp + 5500
        feed.consume(frame('binanceusdm', STAMP - 1, sequence=9007199254740998), STAMP + 5500)
        self.assertEqual(feed.quote('BTCUSDT', STAMP + 5500), quote)

    def test_only_accepted_update_and_removal_notify(self):
        callback = Mock()
        feed = FuturesBookFeed('binanceusdm', lambda: None, on_update=callback)
        feed.symbols = frozenset({'BTCUSDT'})
        feed.consume(frame('binanceusdm', STAMP, sequence=10), STAMP)
        callback.assert_called_once()
        feed.consume(frame('binanceusdm', STAMP, sequence=10), STAMP)
        feed.consume(frame('binanceusdm', STAMP - 1, sequence=11), STAMP)
        self.assertEqual(callback.call_count, 1)
        feed.consume(frame('binanceusdm', STAMP + 1, size='0', sequence=11), STAMP + 1)
        feed.consume(frame('binanceusdm', STAMP + 2, size='0', sequence=12), STAMP + 2)
        self.assertEqual(callback.call_count, 2)

    def test_subscription_shapes_and_urls(self):
        for venue, expected in {
            'binanceusdm': ('wss://fstream.binance.com/public/ws', dict(method='SUBSCRIBE',
                            params=['btcusdt@bookTicker'], id=1)),
            'bybit': ('wss://stream.bybit.com/v5/public/linear', dict(op='subscribe', args=['orderbook.1.BTCUSDT'])),
            'okx': ('wss://ws.okx.com/ws/v5/public', dict(op='subscribe', args=[dict(channel='bbo-tbt',
                     instId='BTC-USDT-SWAP')])),
            'bitget': ('wss://ws.bitget.com/v3/ws/public', dict(op='subscribe', args=[dict(instType='usdt-futures',
                        topic='books1', symbol='BTCUSDT')])),
        }.items():
            with self.subTest(venue=venue):
                protocol = PROTOCOLS[venue]
                self.assertEqual((protocol.url, *protocol.subscribe([SYMBOLS[venue]])), expected)
        gate = PROTOCOLS['gate']
        self.assertEqual(gate.url, 'wss://fx-ws.gateio.ws/v4/ws/usdt')
        subscription, = gate.subscribe(['BTC_USDT'])
        self.assertIsInstance(subscription.pop('time'), int)
        self.assertEqual(subscription, dict(channel='futures.book_ticker', event='subscribe', payload=['BTC_USDT']))


class PublicBookWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_websocket_public_subscriptions_quotes_and_heartbeats(self):
        for venue, symbol in SYMBOLS.items():
            with self.subTest(venue=venue):
                await self.check_wire(venue, symbol)

    async def check_wire(self, venue, symbol):
        protocol = PROTOCOLS[venue]
        captured = []

        async def stream(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            subscription = await socket.receive_json()
            captured.append((subscription, request.headers.get('X-Gate-Size-Decimal')))
            await socket.send_json(frame(venue, now_ms()))
            async for message in socket:
                if message.data == 'ping':
                    captured.append(message.data)
                    await socket.send_str('pong')
                else:
                    captured.append(message.json())
                    await socket.send_json(dict(op='ping', ret_msg='pong', success=True))
            return socket

        app = web.Application()
        app.router.add_get('/ws', stream)
        feed = FuturesBookFeed(venue, lambda: None)
        async with TestServer(app) as gateway, aiohttp.ClientSession() as session:
            connection = (str(gateway.make_url('/ws').with_scheme('ws')), {}, .03)
            with patch.object(feed, 'connection', AsyncMock(return_value=connection)):
                try:
                    feed.start(session, {symbol})
                    async with asyncio.timeout(2):
                        while feed.quote(symbol, now_ms()) is None or len(captured) < (3 if protocol.ping else 1):
                            await asyncio.sleep(.01)
                    self.assertTrue(feed.connected)
                    self.assertEqual(captured[0][1], '1' if venue == 'gate' else None)
                    if protocol.ping:
                        self.assertEqual(captured[1:], [protocol.ping(), protocol.ping()])
                finally:
                    await feed.close()
