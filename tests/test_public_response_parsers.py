import json
from pathlib import Path
import unittest

from market_monitor.funding_history import window_stats
from market_monitor.usdc import ExtendedPublic, parse_edgex_markets
from market_monitor.asset_metrics import parse_valuations
from market_monitor.whitebit import WhitebitBookTicker


FIXTURES = json.loads((Path(__file__).parent / 'fixtures/public_response_samples.json').read_text(encoding='utf-8'))


class PublicResponseParserTests(unittest.TestCase):
    def test_edgex_catalog_parser_filters_non_crypto_and_normalizes_market(self):
        markets = parse_edgex_markets(FIXTURES['edgexMetadata'])
        self.assertEqual(list(markets), ['9001'])
        self.assertEqual(markets['9001']['symbol'], 'BTC/USDC:USDC')

    def test_extended_catalog_filters_non_crypto_and_normalizes_usdc_market(self):
        response = FIXTURES['extended']
        client = ExtendedPublic(None, budget=None)
        client.usdc_ids = {response['assets'][0]['starkexId']}
        markets = [client.market_for(item) for item in response['markets']]
        self.assertEqual(markets[0]['symbol'], 'BTC/USDC:USDC')
        self.assertIsNone(markets[1])

    def test_funding_history_marks_missing_boundary_as_partial(self):
        events = FIXTURES['fundingEvents']
        result = window_stats(events, 10_800_000, 3 / 24)
        self.assertEqual(result['events'], 3)
        self.assertEqual(result['coverage'], 'regular_observed_coverage')
        partial = window_stats(events[1:], 10_800_000, 3 / 24)
        self.assertEqual(partial['coverage'], 'partial_or_irregular')

    def test_whitebit_book_ticker_uses_versioned_public_update(self):
        item = FIXTURES['whitebitBookTicker']
        feed = WhitebitBookTicker(None)
        feed.consume([item], item[0] * 1000)
        market = {'id': item[2], 'symbol': 'BTC/USDT', 'spot': True, 'active': True, 'quote': 'USDT'}
        quote = feed.snapshot({'BTC/USDT': market}, item[0] * 1000)['BTC/USDT']
        self.assertEqual((quote['bid'], quote['ask']), (100.0, 101.0))

    def test_asset_metrics_parses_a_fresh_recorded_market_row(self):
        item = FIXTURES['assetMetrics'][0]
        values = parse_valuations([item], {'bitcoin': 'btc'}, 1_800_000)
        self.assertEqual(values['btc']['cap'], 100.0)
        self.assertEqual(values['btc']['fdv'], 120.0)


if __name__ == '__main__':
    unittest.main()
