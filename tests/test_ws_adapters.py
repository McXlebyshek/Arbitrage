import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from market_monitor.futures_ws import CONNECTION_ACTIVITY_AGE, QUIET_SYMBOL_AGE, QUOTE_CONFIRMATION_AGE, FuturesBookFeed
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
                feed.mark_connected()
                feed.consume(frame(venue, STAMP * 1_000), STAMP + 5)
                self.assertIsNone(feed.quote(symbol, STAMP + 5))
                feed.consume(frame(venue, STAMP - 3070), STAMP)
                self.assertEqual(feed.quote(symbol, STAMP), dict(
                    bid=100, ask=101, changedAt=STAMP - 3070, sourceAt=STAMP - 3070,
                    receivedAt=STAMP, confirmedAt=STAMP, quoteSource='ws',
                    validUntil=STAMP + CONNECTION_ACTIVITY_AGE,
                ))
                feed.consume(frame(venue, STAMP - 3071, size='0', sequence=9007199254740998), STAMP + 5)
                self.assertIsNotNone(feed.quote(symbol, STAMP + 5))
                self.assertIsNotNone(feed.quote(symbol, STAMP + 3001))
                self.assertIsNone(feed.quote(symbol, STAMP + 6 + CONNECTION_ACTIVITY_AGE))
                feed.consume(frame(venue, STAMP - 3069, size='0', sequence=9007199254740998), STAMP + 5)
                self.assertIsNone(feed.quote(symbol, STAMP + 5))

    def test_snapshot_can_renew_and_bybit_engine_restart_can_reset_sequence(self):
        feed = FuturesBookFeed('bybit', lambda: None)
        feed.symbols = frozenset({'BTCUSDT'})
        feed.mark_connected()
        feed.consume(frame('bybit', STAMP), STAMP)
        feed.consume(frame('bybit', STAMP), STAMP + 3000)
        quote = feed.quote('BTCUSDT', STAMP + 3000)
        self.assertEqual((quote['changedAt'], quote['confirmedAt']), (STAMP, STAMP + 3000))
        reset = frame('bybit', STAMP + 2, sequence=1)
        reset['data']['a'][0][0] = '102'
        feed.consume(reset, STAMP + 3001)
        quote = feed.quote('BTCUSDT', STAMP + 3001)
        self.assertEqual((quote['ask'], quote['changedAt']), (102, STAMP + 2))

    def test_binance_excludes_coin_m_and_bitget_excludes_other_products(self):
        for venue in ('binanceusdm', 'bitget'):
            feed = FuturesBookFeed(venue, lambda: None)
            feed.symbols = frozenset({'BTCUSDT'})
            feed.mark_connected()
            message = frame(venue, STAMP)
            if venue == 'binanceusdm':
                message['st'] = 2
            else:
                message['arg']['instType'] = 'usdc-futures'
            feed.consume(message, STAMP)
            self.assertIsNone(feed.quote('BTCUSDT', STAMP))

    def test_calibration_changes_diagnostic_timestamp_without_changing_exchange_order(self):
        clock = Mock(correct=lambda stamp: stamp + 5000)
        feed = FuturesBookFeed('binanceusdm', lambda: None, clock=clock)
        feed.symbols = frozenset({'BTCUSDT'})
        feed.mark_connected()
        feed.consume(frame('binanceusdm', STAMP), STAMP + 5000)
        quote = feed.quote('BTCUSDT', STAMP + 5000)
        self.assertEqual(quote['sourceAt'], STAMP + 5000)
        self.assertEqual(quote['sourceAtRaw'], STAMP)
        self.assertEqual(quote['changedAt'], STAMP + 5000)
        self.assertEqual(quote['changedAtRaw'], STAMP)
        clock.correct = lambda stamp: stamp + 5500
        feed.consume(frame('binanceusdm', STAMP - 1, sequence=9007199254740998), STAMP + 5500)
        self.assertEqual(feed.quote('BTCUSDT', STAMP + 5500)['confirmedAt'], quote['confirmedAt'])

    def test_only_accepted_update_and_removal_notify(self):
        callback = Mock()
        feed = FuturesBookFeed('binanceusdm', lambda: None, on_update=callback)
        feed.symbols = frozenset({'BTCUSDT'})
        feed.mark_connected()
        feed.consume(frame('binanceusdm', STAMP, sequence=10), STAMP)
        callback.assert_called_once()
        feed.consume(frame('binanceusdm', STAMP, sequence=10), STAMP)
        feed.consume(frame('binanceusdm', STAMP - 1, sequence=11), STAMP)
        self.assertEqual(callback.call_count, 1)
        feed.consume(frame('binanceusdm', STAMP + 1, size='0', sequence=11), STAMP + 1)
        feed.consume(frame('binanceusdm', STAMP + 2, size='0', sequence=12), STAMP + 2)
        self.assertEqual(callback.call_count, 2)

    def test_transport_heartbeat_does_not_renew_confirmation_for_symbol(self):
        symbol = SYMBOLS['binanceusdm']
        feed = FuturesBookFeed('binanceusdm', lambda: None)
        feed.symbols = frozenset({symbol})
        feed.mark_connected()
        feed.consume(frame('binanceusdm', STAMP), STAMP)
        after_transport_expiry = STAMP + CONNECTION_ACTIVITY_AGE + 1
        self.assertIsNone(feed.quote(symbol, after_transport_expiry))
        feed.heartbeat_received(after_transport_expiry)
        self.assertEqual(feed.quote(symbol, after_transport_expiry)['confirmedAt'], STAMP)
        after_confirmation_expiry = STAMP + QUOTE_CONFIRMATION_AGE + 1
        feed.heartbeat_received(after_confirmation_expiry)
        self.assertIsNone(feed.quote(symbol, after_confirmation_expiry))

    def test_rest_initial_and_fallback_require_current_subscription_and_observation(self):
        for venue, ack in {
            'binanceusdm': dict(id=1, result=None),
            'gate': dict(channel='futures.book_ticker', event='subscribe', result=dict(status='success')),
        }.items():
            with self.subTest(venue=venue):
                symbol = SYMBOLS[venue]
                feed = FuturesBookFeed(venue, lambda: None)
                feed.symbols = frozenset({symbol})
                feed.mark_connected()
                due = STAMP + QUIET_SYMBOL_AGE
                feed.heartbeat_received(STAMP)
                self.assertEqual(feed.quiet_symbols(due), [])
                self.assertIsNone(feed.confirm_rest(symbol, feed.rest_token(symbol), 100, 101, due))
                feed.consume(ack, STAMP)
                self.assertEqual(feed.quiet_symbols(due), [symbol])
                initial = feed.rest_token(symbol)
                self.assertFalse(feed.confirm_rest(symbol, initial, 100, 101, due, changed_at=STAMP))
                self.assertEqual(feed.quote(symbol, due)['quoteSource'], 'rest')
                self.assertNotIn('suspect', feed.quote(symbol, due))

                feed.consume(frame(venue, STAMP, sequence=1), due + 1)
                self.assertIsNone(feed.confirm_rest(symbol, initial, 100, 101, due + 2))
                self.assertEqual(feed.quote(symbol, due + 1)['quoteSource'], 'ws')
                token = feed.rest_token(symbol)
                self.assertTrue(feed.confirm_rest(symbol, token, 100, 101, due + 2))
                self.assertEqual(feed.quote(symbol, due + 2)['confirmedAt'], due + 2)
                self.assertEqual(feed.quote(symbol, due + 2)['receivedAt'], due + 1)
                self.assertIsNone(feed.confirm_rest(symbol, token, 99, 100, due + 3))

                token = feed.rest_token(symbol)
                self.assertFalse(feed.confirm_rest(symbol, token, 99, 100, due + 3))
                quote = feed.quote(symbol, due + 3)
                self.assertEqual((quote['bid'], quote['quoteSource'], quote['suspect']), (99, 'rest', True))
                old_session = feed.rest_token(symbol)
                feed.mark_connected()
                feed.consume(ack, due + 4)
                self.assertIsNone(feed.confirm_rest(symbol, old_session, 99, 100, due + 4))

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
        transport_pong = asyncio.Event()

        async def stream(request):
            socket = web.WebSocketResponse(autoping=False)
            await socket.prepare(request)
            subscription = await socket.receive_json()
            captured.append((subscription, request.headers.get('X-Gate-Size-Decimal')))
            await socket.send_json(frame(venue, now_ms()))
            await socket.ping(b'probe')
            async for message in socket:
                if message.type == aiohttp.WSMsgType.PONG:
                    if message.data == b'probe':
                        transport_pong.set()
                elif message.data == 'ping':
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
                        while (feed.quote(symbol, now_ms()) is None or not transport_pong.is_set()
                               or len(captured) < (3 if protocol.ping else 1)):
                            await asyncio.sleep(.01)
                    self.assertTrue(feed.connected)
                    self.assertEqual(captured[0][1], '1' if venue == 'gate' else None)
                    if protocol.ping:
                        self.assertEqual(captured[1:], [protocol.ping(), protocol.ping()])
                finally:
                    await feed.close()
