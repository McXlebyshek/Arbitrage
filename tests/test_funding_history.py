from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arbitrage.funding_history import DAY, window_stats, fetch_history, persist, read_reports

END = 1793750400000


class FundingHistoryTests(unittest.IsolatedAsyncioTestCase):
    def test_window_boundaries_cadence_and_missing_rates_are_explicit(self):
        rows = [dict(timestamp=END-hours*3600000, rate="0.0001") for hours in range(0, 32*24, 8)]
        self.assertEqual(window_stats(rows, END, 30)["coverage"], "regular_observed_coverage")
        self.assertEqual(window_stats(rows, END, 7)["events"], 21)
        self.assertEqual(window_stats(rows[:4], END, 30)["coverage"], "partial_or_irregular")
        self.assertEqual(window_stats(rows[:5]+rows[6:], END, 30)["coverage"], "partial_or_irregular")
        self.assertIsNone(window_stats([], END, 7)["mean_rate"])
        jittered = [dict(row, timestamp=row["timestamp"]+(i%2)*1000) for i, row in enumerate(rows)]
        self.assertEqual(window_stats(jittered, END, 30)["coverage"], "regular_observed_coverage")

    async def test_forward_pagination_is_bounded_and_checks_overlap(self):
        class Client:
            async def fetch_funding_rate_history(self, symbol, since, limit, params):
                return [dict(symbol=symbol, timestamp=stamp, fundingRate=0.0001)
                        for stamp in range(since, min(since+100, 205))]
        result = await fetch_history(Client(), "binanceusdm", "BTC", 0, 205)
        self.assertEqual(result["requests"], 3)
        self.assertEqual(len(result["events"]), 205)
        self.assertIsNone(result["error"])

    async def test_numbered_pages_and_stalled_cursor_are_detected(self):
        calls = []
        class Client:
            async def fetch_funding_rate_history(self, symbol, since, limit, params):
                calls.append(params)
                return [dict(symbol=symbol, timestamp=stamp, fundingRate=0.001) for stamp in range(100, 200)]
        result = await fetch_history(Client(), "mexc", "BTC", 0, 300)
        self.assertEqual(calls, [{"page_num": 1}, {"page_num": 2}])
        self.assertIn("did not advance", result["error"])
        self.assertEqual(len(result["events"]), 100)

    async def test_backward_cursor_and_page_budget(self):
        calls = []
        class Client:
            async def fetch_funding_rate_history(self, symbol, since, limit, params):
                end = params["until"]
                calls.append(end)
                return [dict(symbol=symbol, timestamp=stamp, fundingRate=0.001) for stamp in range(end-99, end+1)]
        result = await fetch_history(Client(), "bybit", "BTC", 0, 500, max_pages=2)
        self.assertEqual(calls, [500, 400])
        self.assertEqual(result["error"], "History page budget exhausted")

    async def test_invalid_symbol_or_rate_never_becomes_zero(self):
        class Client:
            async def fetch_funding_rate_history(self, *args, **kwargs):
                return [dict(symbol="WRONG", timestamp=100, fundingRate=None)]
        result = await fetch_history(Client(), "okx", "BTC", 0, 200)
        self.assertEqual(result["events"], [])
        self.assertIsNotNone(result["error"])

    def test_persistence_is_idempotent_and_error_prevents_coverage_claim(self):
        events = [dict(timestamp=END-hours*3600000, rate="0.0001") for hours in range(0, 32*24, 8)]
        binding = dict(id="BTC_USDT", asset="bitcoin")
        result = dict(events=events, requests=1, error=None)
        with TemporaryDirectory() as directory:
            database = Path(directory)/"history.sqlite3"
            persist(database, "mexc", binding, result, END)
            persist(database, "mexc", binding, result, END)
            self.assertEqual(len(read_reports(database)), 1)
            result["error"] = "truncated"
            report = persist(database, "mexc", binding, result, END)
            self.assertTrue(all(window["coverage"] == "partial_or_irregular" for window in report["windows"]))
