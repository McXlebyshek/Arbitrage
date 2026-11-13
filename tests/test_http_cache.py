import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
import orjson

from market_monitor.http_server import compact_futures_quotes
from monitor import Monitor, create_app
from market_monitor.thresholds import PUBLIC_CONFIG


NOW = 1_800_000_000_000


class HttpCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.monitor = Monitor([], Path('unused.sqlite3'))
        self.started, self.release = asyncio.Event(), asyncio.Event()

        async def public_book(market):
            self.started.set()
            await self.release.wait()
            return dict(symbol=market['symbol'], timestamp=NOW,
                        bids=[[100, 2]], asks=[[101, 2]])

        self.fetch = AsyncMock(side_effect=public_book)
        source = SimpleNamespace(kind='swap', public_book=self.fetch)
        self.monitor.instruments = {
            str(i): (source, dict(symbol=str(i), contractSize=1)) for i in range(5)
        }
        self.app = create_app(self.monitor, collect=False)
        self.lifecycle = self.app.cleanup_ctx[0](self.app)
        await anext(self.lifecycle)
        self.clock = patch('monitor.now_ms', return_value=NOW)
        self.clock.start()

    async def asyncTearDown(self):
        self.release.set()
        await self.lifecycle.aclose()
        self.clock.stop()

    def handler(self, path):
        return next(route.handler for route in self.app.router.routes()
                    if route.method == 'GET' and route.resource.canonical == path)

    async def book(self, key='0'):
        return await self.handler('/api/market/{key}/book')(SimpleNamespace(match_info={'key': key}))

    async def test_concurrent_same_market_uses_one_request_and_cache(self):
        first = asyncio.create_task(self.book())
        await self.started.wait()
        second = asyncio.create_task(self.book())
        await asyncio.sleep(0)
        self.release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(results[0].body, results[1].body)
        self.assertEqual((await self.book()).body, results[0].body)
        self.assertEqual(self.fetch.await_count, 1)

    async def test_cancelled_caller_does_not_cancel_shared_fetch(self):
        first = asyncio.create_task(self.book())
        await self.started.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.release.set()
        self.assertEqual((await self.book()).status, 200)
        self.assertEqual(self.fetch.await_count, 1)

    async def test_limit_applies_to_distinct_markets(self):
        requests = [asyncio.create_task(self.book(str(i))) for i in range(4)]
        await self.started.wait()
        with self.assertRaises(web.HTTPTooManyRequests):
            await self.book('4')
        duplicate = asyncio.create_task(self.book('0'))
        self.release.set()
        await asyncio.gather(*requests, duplicate)
        self.assertEqual(self.fetch.await_count, 4)

    async def test_failure_releases_slot_and_allows_retry(self):
        self.fetch.side_effect = ValueError('invalid book')
        with self.assertLogs('market_monitor.http_server', level='WARNING'):
            with self.assertRaises(web.HTTPServiceUnavailable):
                await self.book()
        self.fetch.side_effect = None
        self.fetch.return_value = dict(timestamp=NOW, bids=[[100, 2]], asks=[[101, 2]])
        self.assertEqual((await self.book()).status, 200)
        self.assertEqual(self.fetch.await_count, 2)

    async def test_expired_source_is_not_served_from_cache(self):
        self.release.set()
        await self.book()
        with patch('monitor.now_ms', return_value=NOW + 3001), self.assertLogs('market_monitor.http_server', level='WARNING'):
            with self.assertRaises(web.HTTPServiceUnavailable):
                await self.book()
        self.assertEqual(self.fetch.await_count, 2)

    async def test_catalog_change_invalidates_cached_book(self):
        self.release.set()
        first = await self.book()
        source, market = self.monitor.instruments['0']
        self.monitor.instruments['0'] = source, dict(market, contractSize=2)
        second = await self.book()
        self.assertNotEqual(first.body, second.body)
        self.assertEqual(self.fetch.await_count, 2)

    async def test_catalog_change_rejects_an_in_flight_book(self):
        old = asyncio.create_task(self.book())
        await self.started.wait()
        source, market = self.monitor.instruments['0']
        self.monitor.instruments['0'] = source, dict(market, contractSize=2)
        self.release.set()
        with self.assertRaises(web.HTTPServiceUnavailable):
            await old
        self.assertEqual((await self.book()).status, 200)

    async def test_shutdown_cancels_pending_fetch(self):
        request = asyncio.create_task(self.book())
        await self.started.wait()
        await self.lifecycle.aclose()
        with self.assertRaises(asyncio.CancelledError):
            await request

    async def test_snapshot_serialized_once_and_invalidated_on_publish(self):
        snapshot = self.handler('/api/markets')
        with patch('market_monitor.http_server.orjson.dumps', wraps=orjson.dumps) as dumps:
            request = SimpleNamespace(headers={}, query={})
            first = await snapshot(request)
            second = await snapshot(request)
            self.assertEqual(first.body, second.body)
            self.assertEqual(dumps.call_count, 1)
            self.monitor.snapshot = dict(self.monitor.snapshot, generatedAt=NOW)
            third = await snapshot(request)
            self.assertEqual(dumps.call_count, 2)
        self.assertEqual(json.loads(third.body)['generatedAt'], NOW)
        self.assertEqual(json.loads(third.body)['config'], PUBLIC_CONFIG)
        self.assertEqual(third.headers['Cache-Control'], 'no-store')
        self.assertEqual(third.content_type, 'application/json')

    async def test_conditional_snapshot_omits_body_until_publication_changes(self):
        snapshot = self.handler('/api/markets')
        first = await snapshot(SimpleNamespace(headers={}, query={}))
        tag = first.headers['ETag']
        for value in (tag, tag[2:], '"other", ' + tag, '*'):
            response = await snapshot(SimpleNamespace(headers={'If-None-Match': value}, query={}))
            self.assertEqual(response.status, 304)
            self.assertIsNone(response.body)
            self.assertEqual(response.headers['ETag'], tag)
        self.monitor.snapshot = dict(self.monitor.snapshot, generatedAt=NOW)
        response = await snapshot(SimpleNamespace(headers={'If-None-Match': tag}, query={}))
        self.assertEqual(response.status, 200)
        self.assertNotEqual(response.headers['ETag'], tag)
        self.assertEqual(json.loads(response.body)['generatedAt'], NOW)

    async def test_compact_snapshot_and_versioned_metadata_are_consistent(self):
        leg = dict(id='fixture', symbol='BTC/USDT:USDT', exchange='Fixture', bid=100)
        snapshot = self.handler('/api/markets')
        meta = self.handler('/api/meta')
        compact_request = SimpleNamespace(headers={}, query={'compact': '1'})

        async def publish(**updates):
            await self.monitor.publish_snapshot(dict(self.monitor.snapshot, markets={'BTC': [dict(leg, **updates)]}))
            return json.loads((await snapshot(compact_request)).body)

        first = await publish()
        self.assertEqual(first['markets']['BTC'], [{'id': 'fixture', 'bid': 100}])
        version = first['metaVersion']
        fields = json.loads((await meta(SimpleNamespace(query={'version': version}))).body)
        self.assertEqual(fields['markets']['fixture'], {'symbol': leg['symbol'], 'exchange': leg['exchange']})
        self.assertEqual(first['config'], PUBLIC_CONFIG)
        self.assertEqual(fields['config'], PUBLIC_CONFIG)
        self.assertEqual((await publish(bid=101))['metaVersion'], version)
        self.assertNotEqual((await publish(exchange='Other'))['metaVersion'], version)
        self.assertEqual((await meta(SimpleNamespace(query={'version': version}))).status, 200)


class FuturesQuoteWireTests(unittest.TestCase):
    def test_futures_quote_event_contains_only_ids_and_dynamic_fields(self):
        static = dict(exchange='Fixture', type='swap', quote='USDT', symbol='BTC/USDT:USDT', asset='btc',
                      size=1, networks=[dict(network='Ethereum', deposit=True, withdraw=True)],
                      nativeBase=False, priceUnits=1, totals={'1': .01, '3': .03, '7': .07},
                      volume=1000000)
        quote = dict(bid=100, ask=101, sourceAt=NOW, receivedAt=NOW, quoteSource='ws',
                     confirmedAt=NOW, validUntil=NOW + 45000, changedAt=NOW - 5000, suspect=True)
        pair = dict(coin='BTC', expiresAt=NOW + 45000, timestampSuspect=True,
                    legs=[dict(static, id='venue-a.swap.BTC.USDT', **quote),
                          dict(static, exchange='Other', id='venue-b.swap.BTC.USDT', **quote)])

        event = compact_futures_quotes(NOW, [pair])
        compact_legs = event['pairs'][0]['legs']
        self.assertEqual([leg['id'] for leg in compact_legs], [leg['id'] for leg in pair['legs']])
        self.assertEqual(set(compact_legs[0]), {'id', 'bid', 'ask', 'sourceAt', 'receivedAt', 'quoteSource',
                                                'confirmedAt', 'validUntil', 'changedAt', 'suspect'})
        self.assertLess(len(orjson.dumps(event)), len(orjson.dumps(dict(baseAt=NOW, pairs=[pair]))))
