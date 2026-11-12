from pathlib import Path
import unittest

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
