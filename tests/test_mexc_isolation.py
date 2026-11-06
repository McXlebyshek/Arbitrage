import asyncio
from unittest.mock import patch
import unittest

from arbitrage.live import LiveExchange, now_ms
from arbitrage.mexc_book import MexcBook
from validate_mexc_isolation import check


def frame(version, stamp):
    return dict(channel="push.depth", symbol="ATOM_USDT",
                data=dict(begin=version, end=version, version=version, cts=stamp, bids=[], asks=[]))


def book(version):
    result = MexcBook()
    result.initialize(dict(version=version, bids=[[100, 10]], asks=[[101, 10]]))
    return result


class RecoveryIsolationTests(unittest.IsolatedAsyncioTestCase):
    def worker(self):
        result = LiveExchange("mexc", {"ATOM"})
        result.markets = {"ATOM_USDT":dict(id="ATOM_USDT", symbol="ATOM/USDT:USDT",
                                         base="ATOM", quote="USDT", contractSize=1)}
        return result

    async def test_slow_failed_rest_does_not_interrupt_four_peers(self):
        result = await check()
        self.assertEqual(result["minimum_eligible_peers"], 4)
        self.assertEqual(result["reconnects"], 0)
        self.assertEqual(result["websocket_connections"], 1)
        self.assertEqual(result["final_eligible"], 5)
        self.assertIsNotNone(result["recovery_seconds"])

    async def test_cancel_recovery_before_socket_reuse(self):
        worker = self.worker()
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def fetch(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(worker, "fetch_mexc_book", fetch):
            async with worker.recovery_scope():
                await worker.parse_quotes(None, frame(101, now_ms()), now_ms())
                await entered.wait()
                self.assertEqual(len(worker.mexc_tasks), 1)
            self.assertTrue(cancelled.is_set())
            self.assertFalse(worker.mexc_tasks or worker.mexc_pending or worker.books or worker.quotes)
            with patch.object(worker, "fetch_mexc_book", return_value=book(200)):
                async with worker.recovery_scope():
                    await worker.parse_quotes(None, frame(201, now_ms()), now_ms())
                    await next(iter(worker.mexc_tasks.values()))
                    self.assertEqual(worker.books["ATOM_USDT"].version, 201)

    async def test_buffer_overflow_retries_without_publishing_gap(self):
        worker = self.worker()
        release = asyncio.Event()
        calls = 0
        async def fetch(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                await release.wait()
                return book(100)
            return book(250)

        with patch.object(worker, "fetch_mexc_book", fetch):
            async with worker.recovery_scope():
                for version in range(101, 251):
                    await worker.parse_quotes(None, frame(version, now_ms()), now_ms())
                self.assertEqual(len(worker.mexc_pending["ATOM_USDT"]), 128)
                task = next(iter(worker.mexc_tasks.values()))
                release.set()
                await asyncio.wait_for(task, 3)
                self.assertEqual(worker.mexc_snapshot_failures, 1)
                self.assertIn("Depth gap", worker.last_mexc_snapshot_failure)
                self.assertFalse(worker.quotes)
                fresh = now_ms()
                quotes = await worker.parse_quotes(None, frame(251, fresh), fresh)
                self.assertEqual(len(quotes), 1)

    async def test_replay_keeps_receipt_and_source_ages(self):
        worker = self.worker()
        old = now_ms() - 10000
        with patch.object(worker, "fetch_mexc_book", return_value=book(100)):
            async with worker.recovery_scope():
                await worker.parse_quotes(None, frame(101, old), old + 10)
                await next(iter(worker.mexc_tasks.values()))
                quote = worker.quotes["ATOM/USDT:USDT"]
                self.assertEqual(quote.received_at_ms, old + 10)
                self.assertEqual(quote.source_timestamp_ms, old)
                self.assertEqual(quote.quality(now_ms()), "stale")
                self.assertFalse(hasattr(worker, "last_fresh"))
