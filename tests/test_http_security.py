import asyncio
from pathlib import Path
import unittest

from aiohttp.test_utils import TestClient, TestServer

from monitor import Monitor, create_app


class LocalHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_publication_notifies_stream_without_an_early_quote(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        async with TestClient(TestServer(create_app(monitor, collect=False))) as client:
            async with client.get('/api/markets/early') as stream:
                async with asyncio.timeout(2):
                    self.assertIn(b'"baseAt": null', await stream.content.readline())
                    self.assertEqual(await stream.content.readline(), b'\n')
                    await monitor.publish_snapshot(dict(monitor.snapshot, generatedAt=1234))
                    self.assertEqual(await stream.content.readline(), b'event: snapshot-ready\n')
                    self.assertIn(b'"generatedAt": 1234', await stream.content.readline())

    async def test_host_validation_and_headers_on_http_and_sse(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        async with TestClient(TestServer(create_app(monitor, collect=False))) as client:
            port = client.port
            for host in (f'attacker.example:{port}', f'127.0.0.1:{port + 1}', 'localhost'):
                async with client.get('/api/markets', headers={'Host': host}) as response:
                    self.assertEqual(response.status, 400)
            for path in ('/', '/api/markets', '/api/health', '/api/markets/early'):
                async with client.get(path, headers={'Host': f'localhost:{port}'}) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
                    self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
            async with client.get('/api/health') as response:
                self.assertEqual((await response.json())['state'], 'starting')

    def test_health_reports_shared_budget_and_age_per_source(self):
        monitor = Monitor(('bybit',), Path('unused.sqlite3'))
        source = monitor.sources['bybit', 'swap']
        source.budget.requests = 12
        source.budget.rate_limits = 2
        source.batches['bybit'] = {'received_at_ms': 1000}
        monitor.snapshot['generatedAt'] = 1050
        monitor.cycle_duration_ms = 200
        monitor.source_duration_ms['bybit', 'swap'] = 150
        health = monitor.health(1100)
        self.assertEqual(health['snapshotAgeMs'], 50)
        self.assertEqual(health['cycleDurationMs'], 200)
        self.assertEqual(health['sources']['Bybit · swap']['batchAgeMs'], 100)
        self.assertEqual(health['sources']['Bybit · swap']['durationMs'], 150)
        self.assertIsNone(health['sources']['Bybit · spot']['batchAgeMs'])
        self.assertEqual(health['rest'], {'bybit': {'requests': 12, 'rateLimits': 2}})
