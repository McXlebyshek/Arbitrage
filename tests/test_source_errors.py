import unittest
from types import SimpleNamespace

import aiohttp

from market_monitor.sources import Source
from monitor import Monitor


class SourceErrorMetricsTests(unittest.TestCase):
    def test_failure_count_is_by_exception_class_and_debug_log_has_traceback(self):
        source = Source('bybit', 'swap', None)
        try:
            raise RuntimeError('request failed')
        except RuntimeError as error:
            with self.assertLogs('market_monitor.sources', level='DEBUG') as captured:
                source.record_failure('catalog', error)
                source.record_failure('tickers', error)

        self.assertEqual(source.error_counts, {'RuntimeError': 2})
        self.assertEqual(len(captured.records), 2)
        self.assertTrue(all(record.exc_info is not None for record in captured.records))

    def test_health_exposes_exception_counts_per_source(self):
        source = SimpleNamespace(
            batches={}, error_counts={'RuntimeError': 2}, budget=SimpleNamespace(requests=0, rate_limits=0),
        )
        monitor = SimpleNamespace(
            sources={('bybit', 'swap'): source}, snapshot=dict(venues={}, generatedAt=None, state='starting'),
            futures_feeds={}, collection_failed=False, cycle_duration_ms=None, snapshot_build_ms=None,
            history=SimpleNamespace(last_write_ms=None), history_write_skipped=0, event_loop_lag_ms=None,
            event_loop_lag_max_ms=None, cycle_started=None, source_duration_ms={}, clocks={},
        )
        health = Monitor.health(monitor, 1000)
        self.assertEqual(health['sources']['Bybit · swap']['errorCounts'], {'RuntimeError': 2})

    def test_spot_ticker_failure_is_counted_logged_and_exposed(self):
        source = Source('mexc', 'spot', None)
        error = aiohttp.ClientError('connection reset')

        with self.assertLogs('market_monitor.sources', level='WARNING') as captured:
            source.record_spot_ticker_failure('mexc', error)

        self.assertEqual(source.error_counts, {'ClientError': 1})
        self.assertEqual(source.ticker_status['mexc']['state'], 'error')
        self.assertEqual(source.ticker_status['mexc']['error'], 'ClientError: connection reset')
        self.assertTrue(captured.records[0].exc_info)

    def test_health_exposes_latest_spot_error(self):
        source = SimpleNamespace(
            batches={}, error_counts={'ClientError': 2}, ticker_status={
                'mexc': {'state': 'error', 'error': 'ClientError: connection reset'},
            }, catalog_status={},
            budget=SimpleNamespace(requests=0, rate_limits=0),
        )
        monitor = SimpleNamespace(
            sources={('mexc', 'spot'): source}, snapshot=dict(venues={}, generatedAt=None, state='starting'),
            futures_feeds={}, collection_failed=False, cycle_duration_ms=None, snapshot_build_ms=None,
            history=SimpleNamespace(last_write_ms=None), history_write_skipped=0, event_loop_lag_ms=None,
            event_loop_lag_max_ms=None, cycle_started=None, source_duration_ms={}, clocks={},
        )
        health = Monitor.health(monitor, 1000)
        spot_health = health['sources']['MEXC · spot']
        self.assertEqual(spot_health['errorCounts'], {'ClientError': 2})
        self.assertEqual(spot_health['lastError'], 'ClientError: connection reset')
