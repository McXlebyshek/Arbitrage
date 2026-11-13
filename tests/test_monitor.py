import asyncio
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from market_monitor.economics import EconomicBlock
from market_monitor.funding_history import fetch_history
from market_monitor.sources import OKX_BAR_MS, Source, okx_turnover
from market_monitor.whitebit import WhitebitBookTicker
from monitor import (
    History,
    Monitor,
    compatible_assets,
    complete_history,
    detail_refresh_after,
    estimate_pair,
    funding_fields,
    normalized_book,
    valid_market,
)

NOW = 1_800_000_000_000


def lagged_binance_market(coin):
    binance = dict(id=f'binanceusdm.swap.{coin}.USDT', type='swap', exchange='Binance', quote='USDT',
                   bid=100, ask=101, volume=2, asset=None, sourceAt=NOW - 5000, receivedAt=NOW)
    kucoin = dict(binance, id=f'kucoinfutures.swap.{coin}.USDT', exchange='KuCoin', sourceAt=NOW - 200)
    return [binance, kucoin]


class MonitorTests(unittest.TestCase):
    def test_snapshot_source_views_detach_mutable_source_mappings(self):
        ticker = {'bid': 100, 'ask': 101}
        catalog = {'BTC': {'id': 'BTC'}}
        funding = {'BTC': {'rate': 0.01}}
        volume = {'BTC': {'quoteVolume': 10}}
        currencies = {'BTC': {'networks': {'ERC20': {'deposit': True}}}}
        source = SimpleNamespace(
            catalogs={'binanceusdm': catalog}, batches={'binanceusdm': {
                'received_at_ms': NOW, 'tickers': {'BTC/USDT': ticker},
            }},
            funding=funding, volume=volume, currencies=currencies, schedule=None,
        )
        monitor = Monitor((), Path('unused.sqlite3'))
        monitor.sources = {('binanceusdm', 'swap'): source}

        views = monitor.snapshot_source_views()

        source.batches['binanceusdm']['tickers'].update({'ETH/USDT': {'bid': 10}})
        ticker['bid'] = 99
        catalog['ETH'] = {'id': 'ETH'}
        catalog['BTC']['id'] = 'changed'
        funding['ETH'] = {'rate': 0.02}
        funding['BTC']['rate'] = 0.99
        volume['ETH'] = {'quoteVolume': 20}
        volume['BTC']['quoteVolume'] = 99
        currencies['BTC']['networks']['TRC20'] = {'deposit': True}
        currencies['BTC']['networks']['ERC20']['deposit'] = False

        view = views['binanceusdm', 'swap']
        self.assertEqual(set(view.batches['binanceusdm']['tickers']), {'BTC/USDT'})
        self.assertEqual(view.batches['binanceusdm']['tickers']['BTC/USDT']['bid'], 100)
        self.assertEqual(set(view.catalogs['binanceusdm']), {'BTC'})
        self.assertEqual(view.catalogs['binanceusdm']['BTC']['id'], 'BTC')
        self.assertEqual(set(view.funding), {'BTC'})
        self.assertEqual(view.funding['BTC']['rate'], 0.01)
        self.assertEqual(set(view.volume), {'BTC'})
        self.assertEqual(view.volume['BTC']['quoteVolume'], 10)
        self.assertEqual(view.currencies['BTC']['networks']['ERC20']['deposit'], True)

    def test_bulk_receipt_confirmation_is_limited_to_empirically_confirmed_venues(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        for venue in ('binanceusdm', 'bybit'):
            market = dict(id='BTCUSDT', symbol='BTC/USDT:USDT', base='BTC', quote='USDT', settle='USDT',
                          swap=True, linear=True, active=True, contractSize=1)
            ticker = dict(symbol=market['symbol'], native_id=market['id'], bid='100', ask='101')
            monitor.sources[venue, 'swap'] = SimpleNamespace(
                catalogs={venue: {'BTCUSDT': market}}, currencies={}, funding={}, volume={}, schedule=None,
                batches={venue: dict(received_at_ms=NOW, tickers={market['symbol']: ticker})},
            )

        with patch('monitor.BULK_RECEIPT_CONFIRMED_VENUES', frozenset({'binanceusdm'})):
            legs = monitor.snapshot_markets(NOW)['BTC']

        confirmed = {leg['id'].split('.')[0]: leg.get('confirmedAt') for leg in legs}
        self.assertEqual(confirmed, {'binanceusdm': NOW, 'bybit': None})

    def test_bybit_funding_zero_is_valid(self):
        result = funding_fields({'info': {'fundingRate': '0', 'nextFundingTime': NOW + 1000}},
                                {'info': {'fundingInterval': '240'}}, 'bybit', NOW)
        self.assertEqual((result['rate'], result['hours'], result['next']), (0, 4, NOW + 1000))

    def test_binance_does_not_infer_schedule_from_failed_request(self):
        market = {'symbol': 'BTC/USDT:USDT'}
        raw = {'fundingRate': 0, 'fundingTimestamp': NOW // 28_800_000 * 28_800_000 + 28_800_000}
        self.assertIsNone(funding_fields(raw, market, 'binanceusdm', NOW)['hours'])
        self.assertEqual(funding_fields(raw, market, 'binanceusdm', NOW, {})['hours'], 8)

    def test_okx_bulk_uses_upcoming_forecast_and_actual_interval(self):
        upcoming = NOW + 1000
        raw = {'info': {'fundingRate': '-0.0002', 'settFundingRate': '0.0003',
                        'fundingTime': str(upcoming), 'nextFundingTime': str(upcoming + 6 * 3_600_000)}}
        result = funding_fields(raw, {}, 'okx', NOW)
        self.assertEqual((result['rate'], result['hours'], result['next']), (-0.02, 6, upcoming))
        self.assertIsNone(funding_fields(raw, {}, 'okx', upcoming)['rate'])

    def test_mexc_bulk_preserves_zero_and_explicit_period(self):
        upcoming = NOW + 1000
        raw = {'info': {'fundingRate': 0, 'collectCycle': 4, 'nextSettleTime': upcoming}}
        result = funding_fields(raw, {}, 'mexc', NOW)
        self.assertEqual((result['rate'], result['hours'], result['next']), (0, 4, upcoming))

    def test_detail_cache_expires_at_settlement_even_if_new_funding_arrives_mid_request(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        boundary = NOW + 2_000
        result = {'funding': {'next': NOW + 14_402_000}, 'totals': {'1': 0}}
        result['refreshAfter'] = detail_refresh_after(result, NOW, boundary, retry_incomplete=True)
        self.assertEqual(result['refreshAfter'], boundary)
        monitor.details['instrument'] = (None, result)
        self.assertIs(monitor.cached_detail('instrument', boundary - 1), result)
        self.assertIsNone(monitor.cached_detail('instrument', boundary))

    def test_detail_rechecks_delayed_history_with_a_bounded_retry(self):
        result = {'funding': {'next': NOW + 14_400_000}, 'totals': {}}
        self.assertEqual(detail_refresh_after(result, NOW, None, retry_incomplete=True), NOW + 15_000)
        self.assertEqual(detail_refresh_after(result, NOW, None, retry_incomplete=False), NOW + 300_000)

    def test_htx_only_combines_schedule_for_the_same_settlement(self):
        upcoming = NOW + 1000
        market = {'symbol': 'BTC/USDT:USDT'}
        raw = {'info': {'funding_rate': '0.0001', 'funding_time': str(upcoming)}}
        for date, hours in [(upcoming, 4), (upcoming + 3_600_000, None)]:
            schedule = {market['symbol']: {'info': {'settlement_date': str(date), 'settlement_period': '4'}}}
            result = funding_fields(raw, market, 'htx', NOW, schedule)
            self.assertEqual((result['rate'], result['hours'], result['next']), (0.01, hours, upcoming))

    def test_bulk_funding_rejects_stale_and_excludes_non_swap_okx_rows(self):
        source = Source('okx', 'swap', None)
        source.catalogs['okx'] = {name: {'id': name, 'symbol': name} for name in ['fresh', 'stale', 'future']}
        response = {'data': [
            {'instId': 'fresh', 'ts': str(NOW), 'instType': 'SWAP'},
            {'instId': 'stale', 'ts': str(NOW - 120_001), 'instType': 'SWAP'},
            {'instId': 'future', 'ts': str(NOW), 'instType': 'FUTURES'},
        ]}
        with patch('market_monitor.sources.now_ms', return_value=NOW):
            self.assertEqual(list(source.catalog_funding(response, 'instId', True, timestamp='ts')), ['fresh'])

    def test_excludes_mismatched_settlement_and_rwa(self):
        market = dict(base='BTC', quote='USDT', settle='USDT', swap=True,
                      linear=True, active=True, contractSize=0.01)
        self.assertTrue(valid_market(market, 'swap'))
        self.assertFalse(valid_market(dict(market, settle='USDC'), 'swap'))
        self.assertFalse(valid_market(dict(market, info={'symbolType': 'stock'}), 'swap'))
        for venue, info in [('okx', {'instCategory': '3'}), ('okx', {'instCategory': '4'}),
                            ('htx', {'tradfi_labels': ['Stocks']}),
                            ('mexc', {'conceptPlate': ['mc-trade-zone-tradfi']}),
                            ('binanceusdm', {'contractType': 'TRADIFI_PERPETUAL'}),
                            ('binanceusdm', {'underlyingType': 'PREMARKET'}),
                            ('kucoinfutures', {'assetClass': 'STOCK'}),
                            ('kucoinfutures', {'assetClass': 'COMMODITY'}),
                            ('kucoinfutures', {'assetClass': 'METAL'}),
                            ('bybit', {'symbolType': 'ETF'}),
                            ('bybit', {'symbolType': 'forex'}),
                            ('whitebit', {'type': 'tradfiFutures'}),
                            ('gate', {'contract_type': 'metals'})]:
            self.assertFalse(valid_market(dict(market, info=info), 'swap', venue=venue))

    def test_scaled_legs_require_the_same_quote_unit_and_verified_asset(self):
        scaled = dict(asset='simon-s-cat', priceUnits=1000)
        self.assertTrue(compatible_assets(scaled, dict(scaled)))
        self.assertFalse(compatible_assets(scaled, dict(asset='simon-s-cat')))
        self.assertFalse(compatible_assets(scaled, dict(asset=None, priceUnits=1000)))
        market = dict(id='1000CATUSDT', base='1000CAT', quote='USDT', settle='USDT',
                      swap=True, linear=True, active=True, contractSize=1)
        match = Monitor((), Path('unused.sqlite3')).registry.match('bybit', 'swap', market)
        self.assertEqual(match['price_units'], 1000)

    def test_registered_alias_preserves_native_symbol_and_requires_confirmed_peer(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        binance, bybit = 'binanceusdm', 'bybit'
        for venue, base, native in [(binance, '哈基米', '哈基米USDT'), (bybit, 'HAJIMI', 'HAJIMIUSDT')]:
            market = dict(id=native, symbol=f'{base}/USDT:USDT', base=base, quote='USDT',
                          settle='USDT', swap=True, linear=True, active=True, contractSize=1)
            ticker = dict(symbol=market['symbol'], native_id=native, bid='1', ask='1.01')
            monitor.sources[venue, 'swap'] = SimpleNamespace(
                catalogs={venue: {native: market}},
                batches={venue: dict(received_at_ms=NOW, tickers={market['symbol']: ticker})},
                funding={}, volume={}, schedule=None,
            )
        legs = monitor.snapshot_markets(NOW)['HAJIMI']
        self.assertEqual([leg['asset'] for leg in legs], ['hajimi', 'hajimi'])
        self.assertEqual(legs[0]['id'], 'binanceusdm.swap.HAJIMI.USDT')
        self.assertEqual(legs[0]['nativeBase'], '哈基米')
        self.assertEqual(legs[0]['bid'], 1)
        self.assertEqual(monitor.instruments[legs[0]['id']][1]['symbol'], '哈基米/USDT:USDT')

        peer = monitor.sources[bybit, 'swap'].catalogs[bybit]['HAJIMIUSDT']
        peer['id'] = 'UNVERIFIED'
        self.assertNotIn('HAJIMI', monitor.snapshot_markets(NOW))
        peer['id'] = 'HAJIMIUSDT'
        monitor.sources[binance, 'swap'].catalogs[binance]['哈基米USDT']['contractSize'] = 1000
        self.assertNotIn('HAJIMI', monitor.snapshot_markets(NOW))

    def test_catalog_uses_the_unified_symbol_and_checks_native_identity(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        for venue, market_id in [('edgex', '9001'), ('extended', 'BTC-USD')]:
            market = dict(id=market_id, symbol='BTC/USDC:USDC', base='BTC', quote='USDC',
                          settle='USDC', swap=True, linear=True, active=True, contractSize=1)
            ticker = dict(symbol=market['symbol'], native_id=market_id, bid='100', ask='101')
            monitor.sources[venue, 'swap'] = SimpleNamespace(
                catalogs={venue: {market_id: market}},
                batches={venue: dict(received_at_ms=NOW, tickers={market['symbol']: ticker})},
                funding={}, volume={}, schedule=None,
            )
        legs = monitor.snapshot_markets(NOW)['BTC']
        self.assertEqual([leg['bid'] for leg in legs], [100, 100])
        source = monitor.sources['edgex', 'swap']
        symbol = 'BTC/USDC:USDC'
        source.volume[symbol] = dict(quoteVolume=1_500_000, native_id='9001', expiresAt=NOW + 1000)
        self.assertEqual(monitor.snapshot_markets(NOW)['BTC'][0]['volume'], 1.5)
        self.assertIsNone(monitor.snapshot_markets(NOW + 1000)['BTC'][0]['volume'])
        source.volume[symbol]['native_id'] = '9002'
        self.assertIsNone(monitor.snapshot_markets(NOW)['BTC'][0]['volume'])
        monitor.sources['edgex', 'swap'].batches['edgex']['tickers']['BTC/USDC:USDC']['native_id'] = '9002'
        legs = monitor.snapshot_markets(NOW)['BTC']
        self.assertEqual([leg['bid'] for leg in legs], [None, 100])

    def test_okx_turnover_requires_all_confirmed_bars_and_uses_quote_volume(self):
        bars = [[str(NOW - i * OKX_BAR_MS), '1', '1', '1', '1', '77', '88', '12500', '1']
                for i in range(1, 97)]
        current = [str(NOW), '1', '1', '1', '1', '77', '88', '12500', '0']
        self.assertEqual(okx_turnover([current, *bars], NOW), 1_200_000)
        self.assertEqual(okx_turnover([*bars, bars[0]], NOW), 1_200_000)
        self.assertIsNone(okx_turnover(bars[1:], NOW))
        self.assertIsNone(okx_turnover([*bars, [*bars[0][:7], '3', '1']], NOW))
        bars[0][8] = '0'
        self.assertIsNone(okx_turnover(bars, NOW))
        bars[0][8] = '1'
        bars[0][7] = '0'
        self.assertEqual(okx_turnover(bars, NOW), 1_187_500)

    def test_whitebit_spot_uses_fresh_monotonic_book_updates(self):
        feed = WhitebitBookTicker(None)
        market = {'id': 'BTC_USDT', 'symbol': 'BTC/USDT', 'spot': True, 'active': True, 'quote': 'USDT'}
        markets = {market['symbol']: market}
        base = NOW / 1000
        feed.consume([[base, base, market['id'], 2, '100', '2', '101', '2']], NOW)
        feed.consume([[base, base, market['id'], 1, '98', '2', '99', '2']], NOW)
        self.assertEqual(feed.snapshot(markets, NOW)[market['symbol']]['bid'], 100)
        self.assertEqual(feed.snapshot(markets, NOW + 60_001), {})
        feed.consume([[base, base, market['id'], 3, '100', '2', '0', '0']], NOW)
        self.assertEqual(feed.snapshot(markets, NOW), {})

    def test_funding_interval_uses_observed_cadence_only_when_next_matches(self):
        step = 3_600_000
        events = [{'timestamp': NOW - 2 * step + index * step, 'rate': '0.0001'} for index in range(3)]
        for next_stamp, expected in [(NOW + step, 1), (NOW + 2 * step, None)]:
            result = dict(events=[], totals={}, errors={}, funding=dict(next=next_stamp, hours=None))
            complete_history(result, events, NOW, None)
            self.assertEqual(result['funding']['hours'], expected)
            self.assertEqual(result['totals'], {})

    def test_hourly_history_accepts_timestamp_drift_but_not_missing_hours(self):
        step = 3_600_000
        events = [{'timestamp': NOW - (25 - index) * step + (5000 if index == 10 else 0),
                   'rate': '0.0001'} for index in range(26)]
        result = dict(events=[], totals={}, errors={})
        complete_history(result, events, NOW, None, tolerance_ms=10_000, interval_ms=step)
        self.assertAlmostEqual(result['totals']['1'], 0.24)
        result = dict(events=[], totals={}, errors={})
        complete_history(result, events[1::2], NOW, None, tolerance_ms=10_000, interval_ms=step)
        self.assertEqual(result['totals'], {})

    def test_depth_and_four_fees(self):
        source = SimpleNamespace(venue='bybit', client=SimpleNamespace(precisionMode=4))
        market = dict(contractSize=0.1, precision={'amount': 1})
        left = normalized_book({'timestamp': NOW, 'asks': [[100, 100]], 'bids': [[99, 100]]}, 0.1, NOW)
        right = normalized_book({'timestamp': NOW, 'asks': [[102, 100]], 'bids': [[101, 100]]}, 0.1, NOW)
        result = estimate_pair([(source, market)] * 2, [left, right], Decimal(201),
                               [Decimal('.001'), Decimal('.001')], NOW)
        self.assertEqual(result['quantity'], 1)
        self.assertEqual(result['used'], 201)
        self.assertAlmostEqual(result['fees'], 0.402)
        restricted = dict(market, info={'lotSizeFilter': {'maxMktOrderQty': '9', 'maxOrderQty': '100'}})
        with self.assertRaisesRegex(EconomicBlock, 'максимум одного рыночного ордера'):
            estimate_pair([(source, restricted), (source, market)], [left, right], Decimal(201),
                          [Decimal('.001'), Decimal('.001')], NOW)
        self.assertAlmostEqual(result['net'], -2.402)
        with self.assertRaisesRegex(ValueError, 'синхронные'):
            estimate_pair([(source, market)] * 2, [left, dict(right, sourceAt=None)],
                          Decimal(201), [Decimal(0)] * 2, NOW)

    def test_binance_depth_repairs_skew_only_when_the_new_quote_matches_a_peer(self):
        star, eden, aligned = map(lagged_binance_market, ('STAR', 'EDEN', 'BTC'))
        aligned[0]['sourceAt'] = NOW - 500
        markets = dict(STAR=star, EDEN=eden, BTC=aligned)
        quote = dict(sourceAt=NOW - 100, receivedAt=NOW, bid=102, ask=103, quoteSource='depth')
        updates = {star[0]['id']: quote, eden[0]['id']: dict(quote, sourceAt=NOW - 2000), aligned[0]['id']: quote}
        self.assertEqual(Monitor.apply_binance_depth(markets, updates, NOW), 1)
        self.assertEqual((star[0]['bid'], star[0]['ask'], star[0]['sourceAt']), (102, 103, NOW - 100))
        self.assertEqual(eden[0]['bid'], 100)
        self.assertEqual(aligned[0]['bid'], 100)

    def test_history_records_one_snapshot_per_minute(self):
        with TemporaryDirectory() as directory, History(Path(directory) / 'markets.sqlite3') as history:
            leg = dict(id='x', sourceAt=NOW, receivedAt=NOW, bid=1, ask=2, last=1, index=None, oi=None)
            history.record([leg], NOW)
            history.record([dict(leg, bid=2)], NOW + 1000)
            samples = history.read('x', NOW + 2000)
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0][3:5], (1, 2))

    def test_snapshot_build_metric_keeps_slowest_build_in_the_cycle(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        monitor._build_snapshot_markets = Mock(return_value={})
        with patch('monitor.monotonic', side_effect=[1.0, 1.04, 2.0, 2.1]):
            monitor.snapshot_markets(NOW)
            monitor.snapshot_markets(NOW, source_keys=())
        self.assertEqual(monitor.snapshot_build_ms, 100)


class EventLoopMetricTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_loop_probe_records_lag_and_cancels_its_next_tick(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        loop = asyncio.get_running_loop()
        monitor.record_event_loop_lag(loop.time() - .025)
        self.assertGreaterEqual(monitor.event_loop_lag_ms, 25)
        self.assertEqual(monitor.event_loop_lag_max_ms, monitor.event_loop_lag_ms)
        self.assertIsNotNone(monitor._event_loop_lag_handle)
        monitor.closing = True
        monitor.cancel_event_loop_lag_probe()
        self.assertIsNone(monitor._event_loop_lag_handle)


class MonitorRunRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_cycle_error_is_reported_and_next_cycle_recovers(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        monitor.snapshot = dict(state='ready', generatedAt=NOW, markets={'old': []}, venues={})
        failed = asyncio.Event()
        published = asyncio.Event()
        loop = asyncio.get_running_loop()
        failures = []

        def snapshot_markets(*_args, **_kwargs):
            if not failures:
                failures.append(True)
                loop.call_soon_threadsafe(failed.set)
                raise KeyError('injected snapshot failure')
            return {}

        monitor.snapshot_markets = Mock(side_effect=snapshot_markets)
        publish = monitor.publish_snapshot

        async def publish_and_signal(snapshot):
            await publish(snapshot)
            published.set()

        monitor.publish_snapshot = publish_and_signal
        with self.assertLogs('monitor', level='ERROR') as logs:
            task = asyncio.create_task(monitor.run())
            try:
                await asyncio.wait_for(failed.wait(), 2)
                await asyncio.sleep(.05)
                health = monitor.health(NOW + 100)
                self.assertEqual(health['state'], 'error')
                self.assertEqual(monitor.snapshot['generatedAt'], NOW)
                self.assertIsNone(health['activeCycleMs'])
                self.assertIn('injected snapshot failure', '\n'.join(logs.output))

                await asyncio.wait_for(published.wait(), 4)
                self.assertEqual(monitor.health(monitor.snapshot['generatedAt'])['state'], 'ready')
                self.assertNotEqual(monitor.snapshot['generatedAt'], NOW)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    def test_health_reports_stalled_snapshot_after_three_intervals(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        monitor.snapshot = dict(state='ready', generatedAt=NOW, markets={}, venues={})
        self.assertEqual(monitor.health(NOW + 3 * 15_000)['state'], 'ready')
        self.assertEqual(monitor.health(NOW + 3 * 15_000 + 1)['state'], 'stalled')


class TurnoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_bulk_turnover_uses_matching_swap_and_preserves_last_batch_on_timeout(self):
        source = Source('kucoinfutures', 'swap', None)
        market = dict(swap=True, linear=True, active=True, quote='USDT', settle='USDT')
        btc, eth = 'BTC/USDT:USDT', 'ETH/USDT:USDT'
        source.catalogs[source.venue] = {btc: dict(market, id='XBTUSDTM'), eth: dict(market, id='ETHUSDTM')}
        fetch = AsyncMock(return_value={btc: dict(symbol=btc, quoteVolume='0', info={'symbol': 'XBTUSDTM'}),
                                        eth: dict(symbol=eth, quoteVolume='2000000', info={'symbol': 'OTHER'})})
        source.clients[source.venue] = SimpleNamespace(fetch_tickers=fetch)
        await source.refresh_bulk_turnover()
        self.assertEqual(list(source.volume), [btc])
        self.assertEqual(source.volume[btc]['quoteVolume'], 0)
        fetch.side_effect = TimeoutError
        with self.assertLogs('market_monitor.sources', level='WARNING'):
            await source.refresh_bulk_turnover()
        self.assertEqual(list(source.volume), [btc])


class KuCoinTickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_futures_snapshot_time_and_falls_back_when_stale(self):
        source = Source('kucoinfutures', 'swap', None)
        symbol = 'BTC/USDT:USDT'
        source.catalogs[source.venue] = {symbol: {'id': 'XBTUSDTM'}}
        data = dict(tradeType='FUTURES', ts=(NOW - 500) * 1_000_000, list=[
            dict(symbol='XBTUSDTM', bestBidPrice='100', bestAskPrice='101', lastPrice='100', quoteVolume='2000000'),
            dict(symbol='NOT_IN_CATALOG', bestBidPrice='1', bestAskPrice='2'),
        ])
        fallback = {symbol: dict(symbol=symbol, bid=98, ask=99, timestamp=NOW - 1000)}
        source.clients[source.venue] = SimpleNamespace(
            fetch=AsyncMock(return_value=dict(code='200000', data=data)),
            fetch_tickers=AsyncMock(return_value=fallback),
        )
        with patch('market_monitor.sources.now_ms', return_value=NOW):
            await source.tickers(source.venue)
            batch = source.batches[source.venue]['tickers']
            self.assertEqual(list(batch), [symbol])
            self.assertEqual((batch[symbol]['timestamp'], batch[symbol]['bid'], batch[symbol]['quoteVolume']),
                             (NOW - 500, '100', '2000000'))
            self.assertEqual(source.ticker_status[source.venue]['feed'], 'uta')
            source.client.fetch_tickers.assert_not_awaited()

            data['ts'] = (NOW - 10_001) * 1_000_000
            with self.assertLogs('market_monitor.sources', level='WARNING'):
                await source.tickers(source.venue)
            self.assertEqual(source.batches[source.venue]['tickers'], fallback)
            self.assertEqual(source.ticker_status[source.venue]['feed'], 'classic')


class BinanceDepthTests(unittest.IsolatedAsyncioTestCase):
    async def test_depth_refresh_is_bounded_and_rotates_through_missing_pairs(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        coins = ('AAA', 'BBB', 'CCC', 'DDD', 'EDEN', 'STAR')
        monitor.snapshot['markets'] = {coin: lagged_binance_market(coin) for coin in coins}

        def book(symbol, limit):
            self.assertEqual(limit, 5)
            stamp = NOW - (4000 if symbol.startswith('EDEN') else 50)
            return dict(symbol=symbol, timestamp=stamp, bids=[[100, 2]], asks=[[101, 2]])

        fetch = AsyncMock(side_effect=book)
        source = SimpleNamespace(client=SimpleNamespace(fetch_order_book=fetch))
        for coin, (leg, _) in monitor.snapshot['markets'].items():
            monitor.instruments[leg['id']] = (source, dict(symbol=f'{coin}/USDT:USDT', contractSize=1))
        with patch('monitor.now_ms', return_value=NOW):
            first = await monitor.binance_depth_quotes()
            second = await monitor.binance_depth_quotes()
        self.assertEqual(len(first), 4)
        self.assertEqual(fetch.await_count, 8)
        self.assertEqual(fetch.await_args_list[4].args, ('EDEN/USDT:USDT',))
        self.assertIn('binanceusdm.swap.STAR.USDT', second)
        self.assertNotIn('binanceusdm.swap.EDEN.USDT', second)


class EarlyBinanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_slot_publishes_atomically_and_preserves_rotation_budget(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        legs = lagged_binance_market('JELLYJELLY')
        for leg in legs:
            leg['asset'] = 'jelly-my-jelly'
            venue = leg['id'].split('.')[0]
            symbol = 'JELLYJELLY/USDT:USDT'
            market = {'id': leg['id'], 'symbol': symbol}
            source = SimpleNamespace(venue=venue, catalogs={venue: {symbol: market}})
            monitor.instruments[leg['id']] = source, market
        monitor.snapshot['generatedAt'] = NOW - 15_000
        monitor.snapshot_markets = lambda *_args, **_kw: {'JELLYJELLY': legs}
        key = legs[0]['id']
        monitor.binance_depth_quote = AsyncMock(return_value=dict(
            bid=102, ask=103, sourceAt=NOW, receivedAt=NOW, quoteSource='depth'))

        async def rotate(*, reserved):
            self.assertEqual(reserved, key)
            self.assertEqual(monitor.early_pair['legs'][0]['bid'], 102)
            return {}

        monitor.binance_depth_quotes = AsyncMock(side_effect=rotate)
        event = asyncio.Event()
        event.set()
        with patch('monitor.now_ms', return_value=NOW):
            quotes = await monitor.binance_cycle_depth({'test': event})
        self.assertEqual(set(quotes), {key})
        self.assertEqual(monitor.early_pair['baseAt'], NOW - 15_000)
        self.assertEqual(monitor.early_pair['expiresAt'], NOW + 2800)
        self.assertEqual(legs[0]['bid'], 100)

    async def test_reserved_slot_counts_even_if_priority_request_fails(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        monitor.snapshot['markets'] = {
            coin: lagged_binance_market(coin) for coin in ('AAA', 'BBB', 'CCC', 'DDD', 'EEE')
        }
        for legs in monitor.snapshot['markets'].values():
            monitor.instruments[legs[0]['id']] = None
        monitor.binance_depth_quote = AsyncMock(return_value=None)
        await monitor.binance_depth_quotes(reserved='binanceusdm.swap.AAA.USDT')
        self.assertEqual(monitor.binance_depth_quote.await_count, 3)
        requested = [call.args[0] for call in monitor.binance_depth_quote.await_args_list]
        self.assertNotIn('binanceusdm.swap.AAA.USDT', requested)


class PyboboDepthTests(unittest.IsolatedAsyncioTestCase):
    async def test_replaces_both_quotes_only_after_synchronized_book_confirmation(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        symbol = 'PYBOBO/USDT:USDT'
        stamps = {'kucoinfutures': NOW - 500, 'mexc': NOW - 100}
        reads = {}
        legs = []
        for venue, exchange, stamp in [('kucoinfutures', 'KuCoin', NOW - 200),
                                        ('mexc', 'MEXC', NOW - 4200)]:
            key = f'{venue}.swap.PYBOBO.USDT'
            book = dict(symbol=symbol, bids=[[0.51, 2]], asks=[[0.52, 2]])
            fetch = AsyncMock(side_effect=lambda market, v=venue, value=book: dict(value, timestamp=stamps[v]))
            reads[venue] = fetch
            monitor.instruments[key] = (SimpleNamespace(public_book=fetch), dict(symbol=symbol, contractSize=1))
            legs.append(dict(id=key, exchange=exchange, asset='capybobo', bid=0.50, ask=0.51,
                             sourceAt=stamp, receivedAt=NOW))
        original = [dict(leg) for leg in legs]

        with patch('monitor.now_ms', return_value=NOW):
            stamps['mexc'] = NOW - 3001
            self.assertEqual((await monitor.recheck_pybobo({'PYBOBO': legs}))['state'], 'unavailable')
            self.assertEqual(legs, original)
            stamps['mexc'] = NOW - 2100
            self.assertEqual((await monitor.recheck_pybobo({'PYBOBO': legs}))['state'], 'depth_skew')
            self.assertEqual(legs, original)
            stamps['mexc'] = NOW - 100
            self.assertEqual(await monitor.recheck_pybobo({'PYBOBO': legs}),
                             dict(state='applied', bulkSkewMs=4000, depthSkewMs=400))
            self.assertEqual([(leg['bid'], leg['ask'], leg['quoteSource']) for leg in legs],
                             [(0.51, 0.52, 'depth')] * 2)
            self.assertEqual((await monitor.recheck_pybobo({'PYBOBO': legs}))['state'], 'bulk_aligned')
        self.assertEqual([fetch.await_count for fetch in reads.values()], [3, 3])


class HtxHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_htx_hourly_history_moves_the_inclusive_upper_bound(self):
        symbol = 'EURUSD/USDT:USDT'
        hour = 3_600_000
        events = [dict(symbol=symbol, timestamp=NOW - i * hour, fundingRate=0.001) for i in range(102)]
        history = AsyncMock(side_effect=[events[:100], events[100:]])
        client = SimpleNamespace(fetch_funding_rate_history=history)
        result = await fetch_history(client, 'htx', symbol, NOW - 101 * hour, NOW)
        self.assertIsNone(result['error'])
        self.assertEqual(len(result['events']), 102)
        self.assertEqual([call.kwargs['params']['end_time'] for call in history.await_args_list],
                         [NOW, events[99]['timestamp'] - 1])


if __name__ == '__main__':
    unittest.main()
