import unittest
from dataclasses import replace
from arbitrage.models import is_target, normalize, number
from arbitrage.__main__ import save
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
from contextlib import closing


class MarketDataTests(unittest.TestCase):
    def setUp(self):
        self.market = dict(symbol="BTC/USDT:USDT", id="BTCUSDT", base="BTC", quote="USDT",
                           settle="USDT", swap=True, linear=True, active=True, contractSize=0.001)
        self.quote = normalize("test", self.market,
                               dict(bid=100, ask=101, timestamp=10000, quoteVolume=2000000), 10100)

    def test_only_active_linear_usdt_swaps(self):
        self.assertTrue(is_target(self.market))
        for change in [dict(swap=False), dict(linear=False), dict(settle="BTC"),
                       dict(quote="USDC"), dict(active=None)]:
            self.assertFalse(is_target(dict(self.market, **change)))

    def test_missing_time_is_not_receipt_time(self):
        quote = normalize("test", self.market, dict(bid=1, ask=2), 10100)
        self.assertIsNone(quote.source_timestamp_ms)
        self.assertEqual(quote.quality(10100), "unknown_source_time")

    def test_quality_checks(self):
        self.assertEqual(self.quote.quality(10100), "ok")
        self.assertEqual(self.quote.quality(14000), "stale")
        self.assertEqual(self.quote.quality(8000), "future_source_time")
        self.assertEqual(replace(self.quote, bid=102).quality(10100), "crossed_book")
        self.assertEqual(replace(self.quote, ask=None).quality(10100), "missing_bid_ask")
        self.assertEqual(replace(self.quote, bid=0).quality(10100), "missing_bid_ask")

    def test_nonfinite_values_are_missing(self):
        for value in ["nan", "inf", None, "bad"]:
            self.assertIsNone(number(value))

    def test_volume_does_not_use_base_units(self):
        quote = normalize("test", self.market, dict(baseVolume=1000, bid=1, ask=2), 10100)
        self.assertIsNone(quote.volume_24h_usdt)
        self.assertEqual(self.quote.contract_size, 0.001)

    def test_history_appends(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "market.sqlite3"
            sample = {"status": {"exchange": "test", "checked_at_ms": 10000}, "quotes": []}
            save(path, [sample])
            save(path, [sample])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
