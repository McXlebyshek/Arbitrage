"""Exercise public HTTP routes against the same deterministic order books."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from monitor import Monitor, create_app


NOW = 1_800_000_000_000


class HttpContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_details_depth_and_execution_routes(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        monitor.registry.match = lambda *args: {'asset': 'btc', 'price_units': 1}
        monitor.detail = AsyncMock(return_value={'errors': {}, 'totals': {}, 'events': []})
        monitor.history.read = lambda *_args: []
        for key, venue, bid, ask in [('a', 'bybit', 99, 100), ('b', 'binanceusdm', 101, 102)]:
            source = SimpleNamespace(venue=venue, kind='swap', client=SimpleNamespace(precisionMode=4),
                                     public_book=AsyncMock(return_value={
                                         'symbol': key, 'timestamp': NOW,
                                         'bids': [[bid, 10]], 'asks': [[ask, 10]],
                                     }))
            market = dict(symbol=key, base='BTC', quote='USDT', contractSize=1, precision={'amount': 1})
            monitor.instruments[key] = source, market
        await monitor.publish_snapshot(dict(monitor.snapshot, generatedAt=NOW, state='ready'))
        with patch('monitor.now_ms', return_value=NOW):
            async with TestClient(TestServer(create_app(monitor, collect=False))) as client:
                response = await client.get('/api/markets')
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())['generatedAt'], NOW)
                etag = response.headers['ETag']
                self.assertEqual((await client.get('/api/markets', headers={'If-None-Match': etag})).status, 304)
                self.assertEqual((await client.get('/api/health?coverage=1')).status, 200)
                self.assertEqual((await client.get('/api/market/absent/book')).status, 404)

                detail = await client.get('/api/market/a/detail')
                self.assertEqual(detail.status, 200)
                self.assertEqual((await detail.json())['samples'], [])
                book = await client.get('/api/market/a/book')
                self.assertEqual((await book.json())['asks'], [[100, 10]])

                spread = await client.post('/api/executable-spread', json={'nominal': 100, 'pairs': [['a', 'b']]})
                self.assertEqual(spread.status, 200)
                self.assertAlmostEqual((await spread.json())['results'][0]['executableSpread'], 200 / 201)

                estimate = await client.get('/api/estimate', params={
                    'long': 'a', 'short': 'b', 'budget': '201', 'longFee': '.1', 'shortFee': '.1',
                })
                self.assertEqual(estimate.status, 200)
                cost = await estimate.json()
                self.assertEqual(cost['quantity'], 1)
                self.assertAlmostEqual(cost['net'], -2.402)
                for source, _ in monitor.instruments.values():
                    source.public_book.assert_awaited_once()
