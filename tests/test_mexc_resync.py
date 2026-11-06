import asyncio
import unittest

from arbitrage.live import LiveExchange
from arbitrage.mexc_book import MexcBook
from arbitrage.models import normalize


class ResyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_gap_removes_quote_before_rest_and_requires_new_delta(self):
        market = dict(id="BTC_USDT", symbol="BTC/USDT:USDT", base="BTC", quote="USDT", contractSize=0.0001)
        snapshot = dict(version=100, bids=[[100, 1]], asks=[[101, 1]])
        worker = LiveExchange("mexc", {"BTC"})
        worker.markets = {"BTC_USDT": market}
        book = MexcBook()
        book.initialize(snapshot)
        worker.books["BTC_USDT"] = book
        worker.quotes[market["symbol"]] = normalize("mexc", market, dict(bid=100, ask=101, timestamp=10000),
                                                    10100, "ws_depth_verified")
        entered, allow_response = asyncio.Event(), asyncio.Event()

        class Response:
            async def __aenter__(self):
                entered.set()
                await allow_response.wait()
                return self

            async def __aexit__(self, *args):
                pass

            def raise_for_status(self):
                pass

            async def json(self):
                return dict(success=True, data=dict(snapshot, version=105))

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        message = dict(channel="push.depth", symbol="BTC_USDT", ts=10100,
                       data=dict(begin=103, end=105, version=105, bids=[], asks=[], cts=10000))
        task = asyncio.create_task(worker.parse_quotes(Session(), message, 10100))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(worker.quotes, {})
            recovery = worker.mexc_tasks["BTC_USDT"]
            allow_response.set()
            self.assertEqual(await task, [])
            await recovery
            self.assertEqual(worker.quotes, {})
            self.assertEqual(worker.books["BTC_USDT"].version, 105)
            self.assertIn("Depth gap", worker.last_book_resync_reason)
            message["data"].update(begin=106, end=106, version=106, bids=[[100.5, 1]])
            quotes = await worker.parse_quotes(Session(), message, 10100)
            self.assertEqual(quotes[0].bid, 100.5)
            self.assertEqual(quotes[0].source, "ws_depth_verified")
        finally:
            allow_response.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
