import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import ccxt.async_support as ccxt

from market_monitor.asset_metrics import AssetMetrics, REFRESH_SECONDS, RETRY_SECONDS
from market_monitor.thresholds import QUOTE_MAX_AGE_MS
from market_monitor.sources import CATALOG_MAX_AGE_MS, CATALOG_REFRESH_MS, Source, public_client_config


NOW = 1_800_000_000_000
SYMBOL = 'BTC/USDT:USDT'
MARKET = dict(id='BTCUSDT', symbol=SYMBOL, base='BTC', quote='USDT', settle='USDT',
              spot=False, swap=True, linear=True, active=True, contractSize=1)


class CatalogLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make_source(self):
        source = Source('bybit', 'swap', None)
        source.clients['bybit'] = SimpleNamespace(
            fetch_tickers=AsyncMock(return_value={SYMBOL: dict(symbol=SYMBOL, bid=100, ask=101)}),
            set_markets_from_exchange=Mock(), close=AsyncMock(),
        )
        source.catalog_client = SimpleNamespace(load_markets=AsyncMock(return_value={SYMBOL: MARKET}), close=AsyncMock())
        self.addAsyncCleanup(source.close)
        return source

    async def test_bootstrap_releases_early_waiters_and_publishes_at_collect_boundary(self):
        source = self.make_source()
        requested, release = asyncio.Event(), asyncio.Event()

        async def load(*, reload):
            requested.set()
            await release.wait()
            return {SYMBOL: MARKET}

        source.catalog_client.load_markets.side_effect = load
        early_ready = asyncio.Event()
        async with asyncio.timeout(2):
            await source.collect(early_ready)
            await requested.wait()
            self.assertTrue(early_ready.is_set())
            self.assertEqual(source.status()['state'], 'loading')
            await source.collect()
            source.client.fetch_tickers.assert_not_awaited()
            self.assertEqual(source.catalog_client.load_markets.await_count, 1)
            release.set()
            await source.catalog_task
            self.assertFalse(source.has_market(MARKET))
            await source.collect()
        self.assertTrue(source.has_market(MARKET))
        self.assertEqual(source.status()['state'], 'ok')
        self.assertEqual(source.batches['bybit']['tickers'][SYMBOL]['bid'], 100)

    async def test_slow_reload_keeps_collecting_and_switches_market_atomically(self):
        source = self.make_source()
        source.catalogs['bybit'] = {SYMBOL: MARKET}
        source.updated = NOW
        source.catalog_status['bybit'] = dict(state='ok', lastSuccessAt=NOW)
        requested, release = asyncio.Event(), asyncio.Event()
        replacement = dict(MARKET, id='BTCUSDT-2', contractSize=2)

        async def load(*, reload):
            requested.set()
            await release.wait()
            return {SYMBOL: replacement}

        source.catalog_client.load_markets.side_effect = load
        with patch('market_monitor.sources.now_ms', return_value=NOW + CATALOG_REFRESH_MS):
            async with asyncio.timeout(2):
                await source.collect()
                await requested.wait()
                await source.collect()
                self.assertTrue(source.has_market(MARKET))
                self.assertEqual(source.client.fetch_tickers.await_count, 2)
                release.set()
                await source.catalog_task
                self.assertTrue(source.has_market(MARKET))
                self.assertFalse(source.has_market(replacement))
                await source.collect()
        self.assertTrue(source.has_market(replacement))
        self.assertFalse(source.has_market(MARKET))
        self.assertEqual(source.status()['catalog']['lastSuccessAt'], NOW + CATALOG_REFRESH_MS)

    async def test_failed_reload_keeps_old_catalog_until_ttl_and_recovers(self):
        source = self.make_source()
        source.catalogs['bybit'] = {SYMBOL: MARKET}
        source.updated = NOW
        source.catalog_status['bybit'] = dict(state='ok', lastSuccessAt=NOW, contracts=1)
        source.catalog_client.load_markets.side_effect = ccxt.RateLimitExceeded('429')
        at = NOW + CATALOG_REFRESH_MS
        with patch('market_monitor.sources.now_ms', side_effect=lambda: at):
            await source.collect()
            await source.catalog_task
            await source.collect()
            status = source.status()
            self.assertEqual(status['catalog']['state'], 'stale')
            self.assertEqual(status['catalog']['lastSuccessAt'], NOW)
            self.assertEqual(status['ticker']['state'], 'ok')
            self.assertEqual(status['state'], 'partial')
            self.assertTrue(source.has_market(MARKET))

            at = NOW + CATALOG_MAX_AGE_MS
            await source.collect()
            self.assertFalse(source.has_market(MARKET))
            self.assertEqual(source.status()['state'], 'error')
            self.assertNotIn('bybit', source.batches)
            await source.catalog_task
            await source.collect()

            source.catalog_client.load_markets.side_effect = None
            at += QUOTE_MAX_AGE_MS
            await source.collect()
            await source.catalog_task
            await source.collect()
        self.assertTrue(source.has_market(MARKET))
        self.assertEqual(source.status()['catalog']['lastSuccessAt'], at)

    async def test_ccxt_collection_client_uses_latest_market_map(self):
        source = self.make_source()
        source.clients['bybit'] = ccxt.bybit(public_client_config('bybit'))
        source.catalog_client = ccxt.bybit(public_client_config('bybit'))
        markets = source.catalog_client.set_markets([MARKET])
        source.publish_catalog(markets)
        self.assertEqual((await source.client.load_markets())[SYMBOL]['id'], MARKET['id'])
        replacement = dict(MARKET, id='BTCUSDT-2')
        markets = source.catalog_client.set_markets([replacement])
        source.publish_catalog(markets)
        self.assertEqual((await source.client.load_markets())[SYMBOL]['id'], replacement['id'])

    async def test_close_cancels_pending_catalog_before_closing_clients(self):
        source = self.make_source()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def load(*, reload):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def close():
            self.assertTrue(cancelled.is_set())

        source.catalog_client.load_markets.side_effect = load
        source.catalog_client.close.side_effect = close
        await source.collect()
        await started.wait()
        await source.close()

    async def test_bitget_tickers_funding_and_usdc_can_overlap(self):
        source = Source('bitget', 'swap', None)
        source.catalogs['bitget'] = {SYMBOL: MARKET}
        calls, all_started = [], asyncio.Event()

        async def wait_on_peers(name):
            calls.append(name)
            if len(calls) == 4:
                all_started.set()
            await all_started.wait()

        async def ticker(params=None):
            if params:
                await wait_on_peers('USDC tickers')
                return {'BTC/USDC:USDC': dict(symbol='BTC/USDC:USDC', bid=102, ask=103)}
            await wait_on_peers('primary tickers')
            return {SYMBOL: dict(symbol=SYMBOL, bid=100, ask=101)}

        async def funding(*, params):
            await wait_on_peers(params['productType'])
            return {}

        source.clients['bitget'] = SimpleNamespace(fetch_tickers=ticker, fetch_funding_intervals=funding)
        async with asyncio.timeout(2):
            await source.collect()
        self.assertEqual(source.status()['returned'], 2)
        usdc = source.batches['bitget']['tickers']['BTC/USDC:USDC']
        self.assertIsInstance(usdc['quote_received_at_ms'], int)

    async def test_turnover_failure_does_not_mark_quotes_failed(self):
        source = Source('aster', 'swap', None)
        source.catalogs['aster'] = {SYMBOL: MARKET}
        source.clients['aster'] = SimpleNamespace(has={}, close=AsyncMock(),
            fetch_bids_asks=AsyncMock(return_value={SYMBOL: dict(symbol=SYMBOL, bid=100, ask=101)}),
            fetch_funding_rates=AsyncMock(return_value={}),
        )
        self.addAsyncCleanup(source.close)
        source.refresh_bulk_turnover = AsyncMock(side_effect=KeyError('broken turnover'))
        with self.assertLogs('market_monitor.sources', level='ERROR') as logs:
            await source.collect()
            await source.turnover_task
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertEqual(source.status()['state'], 'ok')
        self.assertIn('KeyError', source.status()['turnover_error'])
        source.refresh_bulk_turnover.side_effect = None
        source.refresh_bulk_turnover.return_value = None
        with patch('market_monitor.sources.now_ms', return_value=source.turnover_due):
            await source.collect()
            await source.turnover_task
        self.assertIsNone(source.status()['turnover_error'])

    async def test_asset_metrics_supervises_unexpected_failure_but_propagates_cancellation(self):
        metrics = AssetMetrics({'bitcoin': 'bitcoin'})
        metrics.refresh = AsyncMock(side_effect=[TypeError('malformed valuation'), None])
        pause = AsyncMock(side_effect=[None, asyncio.CancelledError])
        with patch('market_monitor.asset_metrics.asyncio.sleep', pause), self.assertLogs(
                'market_monitor.asset_metrics', level='ERROR') as logs:
            with self.assertRaises(asyncio.CancelledError):
                await metrics.run(None)
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertEqual(metrics.refresh.await_count, 2)
        self.assertEqual([call.args[0] for call in pause.await_args_list], [RETRY_SECONDS, REFRESH_SECONDS])
