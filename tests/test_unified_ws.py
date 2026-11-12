from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from monitor import Monitor


NOW = 1_800_000_000_000


class UnifiedBookTests(unittest.TestCase):
    def test_normalized_updates_require_exchange_time_and_clear_invalid_quote(self):
        monitor = Monitor(('okx',), Path('unused.sqlite3'), futures_ws=True)
        feed = monitor.wide_futures_feeds['okx']
        symbol = 'BTC/USDT:USDT'
        feed.symbols = {symbol}
        feed.consume({symbol: dict(symbol=symbol, bid=99, ask=100)}, NOW)
        self.assertIsNone(feed.quote(symbol, NOW))
        row = dict(symbol=symbol, timestamp=NOW, bid=99, ask=100)
        feed.consume({symbol: row}, NOW)
        self.assertEqual(feed.quote(symbol, NOW)['bid'], 99)
        feed.consume(dict(row, timestamp=NOW - 1, bid=90), NOW)
        self.assertEqual(feed.quote(symbol, NOW)['bid'], 99)
        self.assertIsNone(feed.quote(symbol, NOW + 3001))
        feed.consume(dict(row, bid=101), NOW)
        self.assertIsNone(feed.quote(symbol, NOW))

    def test_wide_pairs_use_synced_cross_venue_books_and_the_same_currency(self):
        monitor = Monitor(('okx', 'bybit'), Path('unused.sqlite3'), futures_ws=True)
        legs = []
        for venue, exchange in [('okx', 'OKX'), ('bybit', 'Bybit')]:
            source = monitor.sources[venue, 'swap']
            feed = monitor.wide_futures_feeds[venue]
            key, symbol = f'{venue}.swap.BTC.USDT', 'BTC/USDT:USDT'
            legs.append(dict(id=key, symbol=symbol, exchange=exchange, type='swap', quote='USDT', asset='btc', volume=10))
            monitor.instruments[key] = source, dict(id=key, symbol=symbol)
            feed.symbols = {symbol}
            feed.consume(dict(symbol=symbol, timestamp=NOW, bid=100, ask=101), NOW)

        markets = {'BTC': legs}
        self.assertEqual(monitor.wide_futures_symbols('okx', markets), {'BTC/USDT:USDT'})
        pairs = monitor.wide_futures_pairs(markets, NOW)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['expiresAt'], NOW + 3000)
        legs[1]['quote'] = 'USDC'
        self.assertEqual(monitor.wide_futures_pairs(markets, NOW), [])

class SnapshotWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_keeps_input_versions_and_returns_original_instruments(self):
        monitor = Monitor(('okx', 'bybit'), Path('unused.sqlite3'))
        for venue in ('okx', 'bybit'):
            source = monitor.sources[venue, 'swap']
            market = dict(id=venue + '-TEST', symbol='TEST/USDT:USDT', base='TEST', quote='USDT',
                          settle='USDT', active=True, swap=True, linear=True, contractSize=1)
            symbol = market['symbol']
            source.catalogs[venue] = {symbol: market}
            source.batches[venue] = dict(received_at_ms=NOW, tickers={
                symbol: dict(symbol=symbol, bid=100, ask=101),
            })
        worker = monitor.snapshot_builder.capture({})
        source = monitor.sources['okx', 'swap']
        source.catalogs.clear()
        captured, routes = worker.build(NOW)
        self.assertEqual(len(captured['TEST']), 2)
        self.assertIs(routes['okx.swap.TEST.USDT'][0], source)
        self.assertEqual(await monitor.prepare_markets(NOW), {})

    async def test_configured_pair_prefers_fresh_websocket_before_direct_rest(self):
        monitor = Monitor(('mexc', 'kucoinfutures'), Path('unused.sqlite3'), futures_ws=True)
        rule = monitor.registry.depth_recheck
        legs = []
        for i, (venue, exchange) in enumerate([('kucoinfutures', 'KuCoin'), ('mexc', 'MEXC')]):
            key = f'{venue}.swap.{rule["group"]}.USDT'
            native = f'native-{i}'
            fetch = AsyncMock()
            monitor.instruments[key] = SimpleNamespace(venue=venue, public_book=fetch), {'id': native}
            legs.append(dict(id=key, exchange=exchange, asset=rule['asset'], bid=100, ask=101,
                             volume=0, sourceAt=NOW - 4000*i))
            monitor.futures_feeds[venue].quotes[native] = dict(sourceAt=NOW, receivedAt=NOW, bid=100, ask=101)

        markets = {rule['group']: legs}
        self.assertIn(rule['group'], monitor.select_futures_contracts(markets))
        with patch('monitor.now_ms', return_value=NOW):
            status = await monitor.early_quotes.recheck_configured_pair(markets)
        self.assertEqual(status['state'], 'ws_aligned')
        for source, _ in monitor.instruments.values():
            source.public_book.assert_not_awaited()
