import unittest

from arbitrage.live import LiveExchange
from arbitrage.streams import subscriptions


class GateMonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.worker = LiveExchange("gate", {"TRX", "XRP"})
        self.worker.gate_stream = "obu"
        self.worker.markets = {
            f"{base}_USDT": dict(id=f"{base}_USDT", symbol=f"{base}/USDT:USDT",
                                base=base, quote="USDT", contractSize=1)
            for base in ("TRX", "XRP")
        }

    async def publish(self, base, **changes):
        row = dict(s=f"ob.{base}_USDT.50", full=True, t=10000, u=10,
                   b=[["10", "0.5"]], a=[["11", "2"]])
        row.update(changes)
        quotes = await self.worker.parse_quotes(None, dict(channel="futures.obu", result=row), 10020)
        for quote in quotes:
            self.worker.quotes[quote.symbol] = quote
        return quotes

    async def test_group_subscribes_once_and_routes_by_topic(self):
        message, = subscriptions("gate", self.worker.markets, gate_stream="obu")
        self.assertEqual((message["channel"], message["payload"]),
                         ("futures.obu", ["ob.TRX_USDT.50", "ob.XRP_USDT.50"]))
        trx, = await self.publish("TRX")
        xrp, = await self.publish("XRP", u=99)
        self.assertEqual((trx.source, xrp.source_timestamp_ms), ("ws_depth_verified", 10000))
        self.assertEqual(trx.quality(13001), "stale")
        self.assertEqual(await self.publish("UNKNOWN"), [])
        self.assertEqual(await self.publish("TRX", s="ob.TRX_USDT.400"), [])
        for channel, event in (("futures.obu", "subscribe"), ("futures.book_ticker", "update")):
            self.assertEqual(await self.worker.parse_quotes(None, dict(channel=channel, event=event), 10020), [])
        self.assertEqual(self.worker.book_resyncs, 2)
        self.assertEqual(self.worker.fractional_size_updates, 2)

    async def test_gap_invalidates_both_books_until_each_receives_snapshot(self):
        await self.publish("TRX")
        await self.publish("XRP")
        self.worker.state = "streaming"
        with self.assertRaisesRegex(ValueError, "sequence gap"):
            await self.publish("TRX", full=False, U=12, u=14)
        self.assertEqual(self.worker.snapshot(10020)["status"]["usable_quotes"], 0)
        self.assertFalse(self.worker.quotes or self.worker.books)
        self.assertIn("TRX_USDT", self.worker.last_book_resync_reason)
        await self.publish("TRX", u=30)
        self.assertNotIn("XRP/USDT:USDT", self.worker.quotes)
        with self.assertRaisesRegex(ValueError, "sequence gap"):
            await self.publish("XRP", full=False, U=11, u=11)

    async def test_reversed_source_time_invalidates_contiguous_update(self):
        await self.publish("TRX")
        with self.assertRaisesRegex(ValueError, "source time regressed"):
            await self.publish("TRX", full=False, U=11, u=11, t=9999)
        self.assertEqual(self.worker.out_of_order_updates, 1)
        self.assertFalse(self.worker.quotes)

    async def test_older_replacement_snapshot_cannot_roll_back_a_book(self):
        await self.publish("TRX")
        with self.assertRaisesRegex(ValueError, "snapshot ID regressed"):
            await self.publish("TRX", u=9)
        self.assertFalse(self.worker.books)
