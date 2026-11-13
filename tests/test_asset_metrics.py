import unittest
from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from market_monitor.asset_metrics import MAX_AGE_MS, AssetMetrics, parse_valuations
from market_monitor.market import Registry

NOW = 1_800_000_000_000
IDS = {'bitcoin': 'btc'}


def row(stamp=NOW, **fields):
    updated = datetime.fromtimestamp(stamp / 1000, UTC).isoformat()
    return {'id': 'bitcoin', 'last_updated': updated, 'market_cap': 12_000_000, **fields}


class AssetMetricsTests(unittest.IsolatedAsyncioTestCase):
    def test_registry_resolves_provider_aliases(self):
        ids = Registry.load().coingecko_ids
        self.assertEqual(ids['avalanche'], 'avalanche-2')
        self.assertEqual(ids['bitway-btw'], 'bitway')
        self.assertEqual(ids['xrp'], 'ripple')

    def test_spot_binding_is_exact_and_separate_from_swap(self):
        registry = Registry.load()
        spot = dict(id='BTCUSDT', base='BTC', quote='USDT', active=True, spot=True)
        swap = dict(spot, spot=False, swap=True, linear=True, settle='USDT', contractSize=1)
        self.assertEqual(registry.match('binanceusdm', 'spot', spot)['asset'], 'bitcoin')
        self.assertEqual(registry.match('binanceusdm', 'swap', swap)['asset'], 'bitcoin')
        for unknown in (dict(spot, id='BTC-OTHER'), dict(spot, quote='USDC'),
                        dict(spot, base='WBTC'), dict(spot, active=False), swap):
            self.assertIsNone(registry.match('binanceusdm', 'spot', unknown))
        self.assertIsNone(registry.match('gate', 'spot', spot))

    def test_new_asset_requires_verified_okx_underlying_and_size(self):
        registry = Registry.load()
        binding = registry.bindings['okx', 'swap', 'HYPE-USDT-SWAP']
        swap = dict(id='HYPE-USDT-SWAP', base='HYPE', quote='USDT', settle='USDT',
                    active=True, swap=True, linear=True, contractSize=binding['contract_size'],
                    info={'uly': 'HYPE-USDT', 'ctValCcy': 'HYPE'})
        spot = dict(id='HYPEUSDT', base='HYPE', quote='USDT', active=True, spot=True)
        self.assertEqual(registry.match('bybit', 'spot', spot)['asset'], 'hyperliquid')
        self.assertEqual(registry.match('okx', 'swap', swap)['asset'], 'hyperliquid')
        self.assertIsNone(registry.match('okx', 'swap', dict(swap, info={'uly': 'OTHER-USDT'})))
        self.assertIsNone(registry.match('okx', 'swap', dict(swap, contractSize=999)))

    def test_matches_provider_id_and_validates_fields_and_timestamp(self):
        self.assertEqual(parse_valuations([row(fully_diluted_valuation='NaN')], IDS, NOW), {
            'btc': dict(cap=12, fdv=None, updatedAt=NOW, expiresAt=NOW + MAX_AGE_MS),
        })
        self.assertEqual(parse_valuations([row(id='other', symbol='btc')], IDS, NOW), {})
        self.assertEqual(parse_valuations([row(NOW - MAX_AGE_MS - 1)], IDS, NOW), {})
        self.assertEqual(parse_valuations([row(NOW + 1001)], IDS, NOW), {})
        self.assertEqual(parse_valuations([row(last_updated=None)], IDS, NOW), {})
        self.assertEqual(parse_valuations([row(market_cap=0, fully_diluted_valuation=-1)], IDS, NOW), {})

    async def test_keeps_fresh_values_on_error_and_clears_missing_data_on_success(self):
        metrics = AssetMetrics({'btc': 'bitcoin'})
        session = MagicMock()
        response = session.get.return_value.__aenter__.return_value
        response.raise_for_status = MagicMock()
        response.json = AsyncMock(side_effect=[
            [row(fully_diluted_valuation=15_000_000)], TimeoutError(), [],
        ])
        with patch('market_monitor.asset_metrics.now_ms', return_value=NOW):
            await metrics.refresh(session)
            self.assertEqual(metrics.snapshot(NOW)['btc']['fdv'], 15)
            with self.assertLogs('market_monitor.asset_metrics', level='WARNING'):
                await metrics.refresh(session)
            self.assertIn('btc', metrics.snapshot(NOW + MAX_AGE_MS - 1))
            self.assertEqual(metrics.snapshot(NOW + MAX_AGE_MS), {})
            await metrics.refresh(session)
            self.assertEqual(metrics.snapshot(NOW), {})
