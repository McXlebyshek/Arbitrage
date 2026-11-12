import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from market_monitor.futures_ws import CONNECTION_ACTIVITY_AGE, FuturesBookFeed
from market_monitor.market import now_ms
from market_monitor.sources import Source
from market_monitor.whitebit import WhitebitBookTicker
from monitor import Monitor


class FuturesPairStateTests(unittest.TestCase):
    def test_locally_confirmed_quotes_survive_exchange_time_skew(self):
        monitor = object.__new__(Monitor)
        now = 1_800_000_000_000
        monitor.instruments = {}
        monitor.futures_feeds = {}
        monitor.futures_pairs = []
        monitor.notify_quotes_changed = Mock()
        legs = []
        for venue, symbol, exchange, changed_at, bid in (
            ('binanceusdm', 'BTCUSDT', 'Binance', now - 4000, 99),
            ('gate', 'BTC_USDT', 'Gate.io', now - 20_000, 101),
        ):
            key = f'{venue}.swap.BTC.USDT'
            monitor.instruments[key] = (SimpleNamespace(kind='swap', venue=venue), {'id': symbol})
            legs.append(dict(id=key, exchange=exchange, type='swap', quote='USDT', asset='btc', volume=10))
            monitor.futures_feeds[venue] = SimpleNamespace(quote=lambda _symbol, _now, stamp=changed_at,
                price=bid: dict(bid=price, ask=price + 1, changedAt=stamp, sourceAt=stamp,
                                receivedAt=now - 100, confirmedAt=now - 100,
                                validUntil=now + 44_900, quoteSource='ws'))

        monitor.update_futures_pairs({'BTC': legs}, now)
        self.assertEqual(len(monitor.futures_pairs), 1)
        self.assertTrue(monitor.futures_pairs[0]['timestampSuspect'])
        self.assertEqual(monitor.futures_pairs[0]['expiresAt'], now + 44_900)

    def test_subscription_selection_keeps_near_cutoff_symbols_and_contracts(self):
        monitor = object.__new__(Monitor)
        monitor.instruments = {}
        monitor.futures_feeds = {venue: object() for venue in ('mexc', 'kucoinfutures')}
        monitor.futures_symbols = {venue: set() for venue in monitor.futures_feeds}
        monitor.futures_contracts = {}
        markets = {}
        for index, (coin, volume) in enumerate(
            [(f'OLD{i:02}', 31 - i) for i in range(30)] + [('NEW', 2.05)],
        ):
            legs = []
            symbols = []
            for venue, suffix in (('mexc', '_USDT'), ('kucoinfutures', 'USDTM')):
                key = f'{venue}.swap.{coin}.USDT'
                symbol = f'{coin}{suffix}'
                monitor.instruments[key] = (SimpleNamespace(kind='swap', venue=venue), {'id': symbol})
                legs.append(dict(id=key, volume=volume, asset=coin, quote='USDT'))
                symbols.append(symbol)
                if coin.startswith('OLD'):
                    monitor.futures_symbols[venue].add(symbol)
            markets[coin] = legs
            if coin.startswith('OLD'):
                monitor.futures_contracts[coin] = tuple(symbols)

        selected_symbols = monitor.select_futures_symbols(markets)
        selected_contracts = monitor.select_futures_contracts(markets)
        self.assertEqual(len(selected_symbols['mexc']), 30)
        self.assertIn('OLD29_USDT', selected_symbols['mexc'])
        self.assertNotIn('NEW_USDT', selected_symbols['mexc'])
        self.assertEqual(len(selected_contracts), 30)
        self.assertIn('OLD29', selected_contracts)
        self.assertNotIn('NEW', selected_contracts)

        monitor.futures_symbols = selected_symbols
        monitor.futures_contracts = selected_contracts
        for leg in markets['NEW']:
            leg['volume'] = 3
        selected_symbols = monitor.select_futures_symbols(markets)
        selected_contracts = monitor.select_futures_contracts(markets)
        self.assertIn('NEW_USDT', selected_symbols['mexc'])
        self.assertNotIn('OLD29_USDT', selected_symbols['mexc'])
        self.assertIn('NEW', selected_contracts)
        self.assertNotIn('OLD29', selected_contracts)

    def test_futures_pair_clearing_is_limited_to_affected_venue_and_symbols(self):
        monitor = object.__new__(Monitor)
        monitor.instruments = {
            'binanceusdm.swap.BTC.USDT': (SimpleNamespace(venue='binanceusdm'), {'id': 'BTCUSDT'}),
            'gate.swap.BTC.USDT': (SimpleNamespace(venue='gate'), {'id': 'BTC_USDT'}),
            'binanceusdm.swap.SOL.USDT': (SimpleNamespace(venue='binanceusdm'), {'id': 'SOLUSDT'}),
            'gate.swap.SOL.USDT': (SimpleNamespace(venue='gate'), {'id': 'SOL_USDT'}),
            'mexc.swap.ETH.USDT': (SimpleNamespace(venue='mexc'), {'id': 'ETH_USDT'}),
            'kucoinfutures.swap.ETH.USDT': (SimpleNamespace(venue='kucoinfutures'), {'id': 'ETHUSDTM'}),
        }
        monitor.futures_pairs = [
            dict(coin='BTC', legs=[dict(id='binanceusdm.swap.BTC.USDT'), dict(id='gate.swap.BTC.USDT')]),
            dict(coin='SOL', legs=[dict(id='binanceusdm.swap.SOL.USDT'), dict(id='gate.swap.SOL.USDT')]),
            dict(coin='ETH', legs=[dict(id='mexc.swap.ETH.USDT'), dict(id='kucoinfutures.swap.ETH.USDT')]),
        ]
        monitor.notify_quotes_changed = Mock()
        monitor.clear_futures_pairs('binanceusdm', {'BTCUSDT'})
        self.assertEqual([pair['coin'] for pair in monitor.futures_pairs], ['SOL', 'ETH'])
        monitor.clear_futures_pairs('gate')
        self.assertEqual([pair['coin'] for pair in monitor.futures_pairs], ['ETH'])

class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_whitebit_drops_quotes_on_disconnect_and_resubscribes(self):
        close_first = asyncio.Event()
        retry_started = asyncio.Event()
        release_retry = asyncio.Event()
        subscriptions = []
        connections = 0

        async def book_stream(request):
            nonlocal connections
            connections += 1
            connection = connections
            if connection == 2:
                retry_started.set()
                await release_retry.wait()
                raise web.HTTPServiceUnavailable()
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            subscriptions.append(await socket.receive_json())
            await socket.send_json({'id': 1, 'result': {'status': 'success'}})
            price = 100 + connection
            # Reconnect has a lower update ID and must still accept the first quote.
            await socket.send_json({'method': 'bookTicker_update', 'params': [
                [now_ms() / 1000, None, 'BTC_USDT', 10 if connection == 1 else 1, price, 2, price + 1, 2],
            ]})
            if connection == 1:
                await close_first.wait()
            else:
                async for _ in socket:
                    pass
            await socket.close()
            return socket

        app = web.Application()
        app.router.add_get('/ws', book_stream)
        symbol = 'BTC/USDT'
        source = Source('whitebit', 'spot', None)
        source.catalogs['whitebit'] = {symbol: dict(id='BTC_USDT', symbol=symbol, spot=True, active=True, quote='USDT')}
        source.updated = now_ms()
        source.clients['whitebit'] = SimpleNamespace(fetch_tickers=AsyncMock(side_effect=lambda **_: {
            symbol: dict(symbol=symbol, last=100, bid=98, ask=99, quoteVolume=2_000_000),
        }), close=AsyncMock())

        async def wait_for_quote():
            async with asyncio.timeout(3):
                while not source.book_ticker.quotes:
                    await asyncio.sleep(.01)

        def quote():
            return source.batches['whitebit']['tickers'][symbol]

        async with TestServer(app) as gateway, aiohttp.ClientSession() as session:
            source.book_ticker = WhitebitBookTicker(session)
            with patch('market_monitor.whitebit.BOOK_TICKER_URL', str(gateway.make_url('/ws').with_scheme('ws'))), \
                 patch('market_monitor.ws_reconnect.retry_delay', return_value=.01):
                try:
                    await source.collect()
                    reader = source.book_ticker.reader
                    await wait_for_quote()
                    await source.collect()
                    self.assertEqual((quote()['bid'], quote()['ask']), (101, 102))

                    close_first.set()
                    await asyncio.wait_for(retry_started.wait(), 3)
                    self.assertFalse(source.book_ticker.connected)
                    self.assertFalse(source.book_ticker.quotes)
                    with self.assertLogs('market_monitor.whitebit', level='WARNING'):
                        await source.collect()
                        self.assertEqual(source.ticker_status['whitebit']['state'], 'partial')
                        self.assertEqual((quote()['bid'], quote()['ask'], quote()['last']), (None, None, 100))
                        release_retry.set()
                        await wait_for_quote()
                    self.assertIs(source.book_ticker.reader, reader)
                    self.assertEqual(source.book_ticker.reconnects, 2)
                    await source.collect()
                    self.assertEqual(source.ticker_status['whitebit']['state'], 'ok')
                    self.assertEqual((quote()['bid'], quote()['ask']), (103, 104))
                    self.assertEqual(subscriptions, [{'id': 1, 'method': 'bookTicker_subscribe', 'params': []}] * 2)
                finally:
                    close_first.set()
                    release_retry.set()
                    await source.close()



    async def test_futures_selection_prioritizes_verified_pairs_and_reuses_subscriptions(self):
        monitor = Monitor(('mexc', 'kucoinfutures'), Path('unused.sqlite3'), futures_ws=True)
        markets = {}

        def add(coin, volume, assets=(None, None)):
            legs = []
            for venue, native, asset in zip(
                ('mexc', 'kucoinfutures'), (coin + '_USDT', coin + 'USDTM'), assets,
            ):
                key = f'{venue}.swap.{coin}.USDT'
                monitor.instruments[key] = (monitor.sources[venue, 'swap'], {'id': native})
                legs.append(dict(id=key, volume=volume, asset=asset))
            markets[coin] = legs

        add('INIT', .5)
        add('VERIFIED', 2, ('asset', 'asset'))
        add('LIQUID', 9)
        add('LOW', .9)
        add('COLLISION', 90, ('left', 'right'))
        for i in range(35):
            add(f'OTHER{i:02}', 5)

        selected = monitor.select_futures_contracts(markets)
        self.assertEqual(len(selected), 30)
        self.assertEqual(list(selected)[:3], ['INIT', 'VERIFIED', 'LIQUID'])
        self.assertNotIn('LOW', selected)
        self.assertNotIn('COLLISION', selected)
        self.assertIsNone(markets['LIQUID'][0]['asset'])

        for feed in monitor.futures_feeds.values():
            feed.close = AsyncMock()
            feed.start = Mock(side_effect=lambda _session, symbols, feed=feed: setattr(
                feed, '_subscriptions', {symbol: object() for symbol in symbols},
            ))
        at = now_ms()
        await monitor.refresh_futures_feeds(None, markets, at)
        for feed in monitor.futures_feeds.values():
            feed.start.assert_called_once()
            feed.close.assert_not_awaited()
        add('NEW', 12)
        await monitor.refresh_futures_feeds(None, markets, at + 15000)
        self.assertNotIn('NEW', monitor.futures_contracts)
        for feed in monitor.futures_feeds.values():
            feed.close.assert_not_awaited()
        await monitor.refresh_futures_feeds(None, {}, at + 180000, refresh=False)
        self.assertEqual(monitor.futures_contracts, selected)
        for feed in monitor.futures_feeds.values():
            feed.close.assert_not_awaited()
        await monitor.refresh_futures_feeds(None, markets, at + 180000)
        self.assertIn('NEW', monitor.futures_contracts)
        for feed in monitor.futures_feeds.values():
            feed.close.assert_not_awaited()
            self.assertGreaterEqual(feed.start.call_count, 2)
            self.assertEqual(len(feed.symbols), 30)

    async def test_futures_books_clear_and_resubscribe_after_failed_reconnect(self):
        for venue, symbol in (('mexc', 'BTC_USDT'), ('kucoinfutures', 'XBTUSDTM')):
            with self.subTest(venue=venue):
                await self.check_futures_recovery(venue, symbol)

    async def check_futures_recovery(self, venue, symbol):
        close_first = asyncio.Event()
        retry_started = asyncio.Event()
        release_retry = asyncio.Event()
        subscriptions = []
        connections = 0
        disconnects = []
        first_stamp = None

        def quote(sequence, price, stamp=None):
            at = now_ms() if stamp is None else stamp
            if venue == 'mexc':
                return dict(channel='push.depth.full', symbol=symbol, ts=at, data=dict(
                    version=sequence, bids=[[price, 1]], asks=[[price + 1, 1]],
                ))
            return dict(subject='tickerV2', data=dict(symbol=symbol, ts=at * 1_000_000, sequence=sequence,
                bestBidPrice=price, bestAskPrice=price + 1, bestBidSize=1, bestAskSize=1))

        async def public_token(request):
            return web.json_response({'data': {'token': 'test-public', 'instanceServers': [{
                'endpoint': str(request.url.with_path('/ws').with_scheme('ws')), 'pingInterval': 18000,
            }]}})

        async def stream(request):
            nonlocal connections, first_stamp
            connections += 1
            if connections == 2:
                retry_started.set()
                await release_retry.wait()
                raise web.HTTPServiceUnavailable()
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            subscriptions.append(await socket.receive_json())
            if connections == 1:
                first_stamp = now_ms()
                await socket.send_json(quote(10, 100, first_stamp))
                await socket.send_json(quote(9, 900))
                await socket.send_json(quote(11, 950, now_ms() - 5000))
                await close_first.wait()
                await socket.close()
            else:
                await socket.send_json(quote(9, 900, first_stamp))
                await socket.send_json(quote(1, 101))
                async for _ in socket:
                    pass
            return socket

        app = web.Application()
        app.router.add_get('/ws', stream)
        app.router.add_post('/token', public_token)
        feed = FuturesBookFeed(venue, lambda: disconnects.append(len(feed.quotes)))

        consumed = []
        consume = feed.consume

        def record(message, received):
            consume(message, received)
            consumed.append((feed.quote(symbol, received) or {}).get('bid'))

        feed.consume = record

        async def wait_for_bid(bid):
            async with asyncio.timeout(3):
                while (feed.quote(symbol, now_ms()) or {}).get('bid') != bid:
                    await asyncio.sleep(.01)

        async with TestServer(app) as gateway, aiohttp.ClientSession() as session:
            with patch('market_monitor.futures_ws.MEXC_URL', str(gateway.make_url('/ws').with_scheme('ws'))), \
                 patch('market_monitor.futures_ws.KUCOIN_TOKEN_URL', str(gateway.make_url('/token'))), \
                 patch('market_monitor.ws_reconnect.retry_delay', return_value=.01):
                try:
                    feed.start(session, {symbol})
                    reader = feed.reader
                    await wait_for_bid(100)
                    async with asyncio.timeout(3):
                        while len(consumed) < 3:
                            await asyncio.sleep(.01)
                    self.assertEqual(consumed, [100, 100, 100])
                    close_first.set()
                    await asyncio.wait_for(retry_started.wait(), 3)
                    self.assertFalse(feed.connected)
                    self.assertIsNone(feed.quote(symbol, now_ms()))
                    with self.assertLogs('market_monitor.futures_ws', level='WARNING'):
                        release_retry.set()
                        await wait_for_bid(101)
                    self.assertTrue(feed.connected)
                    self.assertIs(feed.reader, reader)
                    self.assertEqual(feed.reconnects, 2)
                    self.assertEqual(consumed[3:], [None, 101])
                    self.assertIsNone(feed.quote(symbol, now_ms() + CONNECTION_ACTIVITY_AGE + 1))
                    self.assertEqual(disconnects, [0, 0])
                    self.assertEqual(connections, 3)
                    self.assertEqual(len(subscriptions), 2)
                    self.assertEqual(subscriptions[0], subscriptions[1])
                finally:
                    close_first.set()
                    release_retry.set()
                    await feed.close()

    async def test_funding_timeout_and_delayed_period_recover_on_next_valid_batch(self):
        venues = {'okx': 'JCT-USDT-SWAP', 'mexc': 'JCT_USDT', 'htx': 'JCT-USDT'}
        monitor = Monitor(venues, Path('unused.sqlite3'))
        boundary, interval = 1_800_000_000_000, 14_400_000
        now, settlement, rate, unavailable = boundary - 100, boundary, '0.001', False
        symbol = 'JCT/USDT:USDT'

        def reply(venue):
            if unavailable:
                raise TimeoutError('test funding outage')
            native = venues[venue]
            if venue == 'okx':
                return {'code': '0', 'data': [dict(instId=native, instType='SWAP', ts=str(now),
                    fundingTime=str(settlement), nextFundingTime=str(settlement + interval), fundingRate=rate)]}
            if venue == 'mexc':
                return {'success': True, 'data': [dict(symbol=native, timestamp=now, collectCycle=4,
                    nextSettleTime=settlement, fundingRate=rate)]}
            return {'status': 'ok', 'data': [dict(contract_code=native, funding_time=str(settlement),
                next_funding_time=str(settlement + interval), funding_rate=rate, estimated_rate='0.005')]}

        def htx_reply(url):
            if url.endswith('/swap_contract_info'):
                return {'status': 'ok', 'data': [dict(contract_code=venues['htx'])]}
            return reply('htx')

        for venue, native in venues.items():
            source = monitor.sources[venue, 'swap']
            source.catalogs[venue] = {symbol: dict(symbol=symbol, id=native, base='JCT', quote='USDT', settle='USDT',
                                                  active=True, swap=True, linear=True, contractSize=1)}
            source.updated, source.turnover_due = now, now + interval
            source.clients[venue] = SimpleNamespace(fetch_tickers=AsyncMock(return_value={
                symbol: dict(symbol=symbol, bid=100, ask=101, fundingRate=0.123),
            }))
        monitor.sources['okx', 'swap'].client.publicGetPublicFundingRate = AsyncMock(side_effect=lambda _: reply('okx'))
        monitor.sources['mexc', 'swap'].client.fetch = AsyncMock(side_effect=lambda _: reply('mexc'))
        monitor.sources['htx', 'swap'].client.fetch = AsyncMock(side_effect=htx_reply)

        async def snapshot():
            await asyncio.gather(*(monitor.sources[venue, 'swap'].collect() for venue in venues))
            legs = monitor.snapshot_markets(now)['JCT']
            return [(leg['rate'], leg['hours'], leg['next']) for leg in legs]

        with patch('market_monitor.sources.now_ms', side_effect=lambda: now):
            self.assertEqual(await snapshot(), [(0.1, 4, boundary)] * 3)
            now, unavailable = boundary + 100, True
            with self.assertLogs('market_monitor.sources', level='WARNING') as logs:
                self.assertEqual(await snapshot(), [(None, None, None)] * 3)
            self.assertEqual(len(logs.output), 3)
            now, unavailable = boundary + 15_100, False
            self.assertEqual(await snapshot(), [(None, None, None)] * 3)
            now, settlement, rate = boundary + 30_100, boundary + interval, '0.002'
            self.assertEqual(await snapshot(), [(0.2, 4, settlement)] * 3)
