from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from market_monitor.exchange_clock import ExchangeClock
from market_monitor.sources import okx_open_interest
from monitor import Monitor


NOW = 1_800_000_000_000


def row(symbol, *, value='12500', timestamp=NOW, kind='SWAP'):
    return dict(instId=symbol, instType=kind, oiUsd=value, ts=str(timestamp))


class OpenInterestTests(unittest.IsolatedAsyncioTestCase):
    async def test_okx_batch_is_used_only_for_matching_fresh_market(self):
        monitor = Monitor(('okx', 'bybit'), Path('unused.sqlite3'))
        symbol = 'TEST/USDT:USDT'
        for venue in ('okx', 'bybit'):
            source = monitor.sources[venue, 'swap']
            market = dict(id=venue + '-TEST', symbol=symbol, base='TEST', quote='USDT', settle='USDT',
                          active=True, swap=True, linear=True, contractSize=1)
            source.catalogs[venue] = {symbol: market}
            source.batches[venue] = dict(received_at_ms=NOW, tickers={
                symbol: dict(symbol=symbol, bid=100, ask=101, timestamp=NOW),
            })
        okx = monitor.sources['okx', 'swap']
        self.assertIsNone(monitor.snapshot_markets(NOW)['TEST'][0]['oi'])
        okx.turnover_client = SimpleNamespace(publicGetPublicOpenInterest=AsyncMock(return_value={
            'code': '0', 'data': [row('okx-TEST', value='0'), row('unknown')],
        }))
        with patch('market_monitor.sources.now_ms', return_value=NOW):
            await okx.refresh_open_interest()
        okx.turnover_client.publicGetPublicOpenInterest.assert_awaited_once_with({'instType': 'SWAP'})
        self.assertEqual(monitor.snapshot_markets(NOW)['TEST'][0]['oi'], 0)
        okx.open_interest['okx-TEST'] = (NOW - 120_001, 5)
        self.assertIsNone(monitor.snapshot_markets(NOW)['TEST'][0]['oi'])

    async def test_parser_excludes_stale_non_swap_and_invalid_values(self):
        self.assertEqual(okx_open_interest({'code': '0', 'data': [
            row('BTC', value='12'), row('BTC', value='11', timestamp=NOW - 1),
            row('STALE', timestamp=NOW - 120_001), row('SPOT', kind='SPOT'),
            row('INVALID', value='NaN'),
        ]}, NOW, ExchangeClock()), {'BTC': (NOW, 12)})
