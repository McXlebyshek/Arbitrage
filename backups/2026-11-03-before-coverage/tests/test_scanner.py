from contextlib import closing, redirect_stdout
from dataclasses import asdict
from io import StringIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from arbitrage.__main__ import publish
from arbitrage.models import normalize
from arbitrage.scanner import Registry, scan


def binding(exchange, size="1", base="BTC", units="1", asset="bitcoin"):
    return dict(exchange=exchange, id=f"{base}_USDT", base=base, asset=asset,
                price_units=units, contract_size=size)


def snapshot(exchange, bid=99, ask=100, size=1, base="BTC", stamp=10000, source="ws_bbo"):
    market = dict(id=f"{base}_USDT", symbol=f"{base}/USDT:USDT", base=base,
                  quote="USDT", settle="USDT", contractSize=size,
                  active=True, linear=True, swap=True)
    quote = normalize(exchange, market, dict(bid=bid, ask=ask, timestamp=stamp), 10100, source)
    return dict(status=dict(exchange=exchange, state="streaming", checked_at_ms=10100,
                            quotes=1, usable_quotes=1),
                instruments=[market], quotes=[dict(asdict(quote), quality="ok")])


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry(dict(version="test", bindings=[binding("alpha"), binding("beta", "0.01")]))
        self.snapshots = [snapshot("alpha"), snapshot("beta", bid=102, ask=103, size=0.01)]

    def scan(self, **options):
        return scan(self.snapshots, self.registry, 10100, **options)

    def test_directed_pairs_use_ask_for_long_bid_for_short(self):
        result = self.scan()
        self.assertEqual(result["evaluated_pairs"], 2)
        self.assertEqual(result["displayed_pairs"], 2)
        forward, reverse = result["rows"]
        self.assertEqual((forward["long_exchange"], forward["short_exchange"]), ("alpha", "beta"))
        self.assertEqual(forward["open_spread_pct"], 2)
        self.assertAlmostEqual(reverse["open_spread_pct"], 100 * (99 - 103) / 103)
        self.assertIsNone(forward["net_pnl_usdt"])
        self.assertFalse(forward["execution_verified"])

    def test_equal_underlying_and_combined_budget(self):
        row = self.scan()["rows"][0]
        quantity = row["reference_base_quantity"]
        self.assertAlmostEqual(row["reference_long_notional_usdt"] + row["reference_short_notional_usdt"], 1000)
        self.assertAlmostEqual(row["reference_long_contracts_unrounded"] * 1, quantity)
        self.assertAlmostEqual(row["reference_short_contracts_unrounded"] * 0.01, quantity)
        self.assertNotEqual(row["reference_long_notional_usdt"], row["reference_short_notional_usdt"])

    def test_explicit_package_multiplier_normalizes_prices_and_sizes(self):
        registry = Registry(dict(version="package-test", bindings=[binding("alpha", base="FOO", asset="foo"),
                        binding("beta", base="1000FOO", units="1000", asset="foo")]))
        result = scan([snapshot("alpha", 0.099, 0.1, base="FOO"),
                       snapshot("beta", 102, 103, base="1000FOO")], registry, 10100)
        row = result["rows"][0]
        self.assertAlmostEqual(row["open_spread_pct"], 2)
        self.assertEqual(row["short_base_units_per_contract"], 1000)
        self.assertAlmostEqual(row["reference_short_contracts_unrounded"] * 1000, row["reference_base_quantity"])

    def test_unknown_instrument_is_not_matched_by_ticker(self):
        self.snapshots.append(snapshot("unregistered", bid=200, ask=201))
        result = self.scan()
        self.assertEqual(result["displayed_pairs"], 2)
        self.assertEqual(result["exclusions"][0]["reason"], "unregistered_instrument")

    def test_changed_contract_size_is_excluded(self):
        self.snapshots[1] = snapshot("beta", size=0.02)
        result = self.scan()
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["exclusions"][0]["reason"], "contract_size_changed_or_inconsistent")

    def test_metadata_must_confirm_contract_type_and_identity(self):
        for field, value in [("active", False), ("linear", False), ("swap", False), ("settle", "BTC")]:
            with self.subTest(field=field):
                snapshots = [snapshot("alpha"), snapshot("beta", size=0.01)]
                snapshots[1]["instruments"][0][field] = value
                self.assertEqual(scan(snapshots, self.registry, 10100)["rows"], [])
        self.snapshots[1]["quotes"][0]["base"] = "OTHER"
        self.assertEqual(self.scan()["exclusions"][0]["reason"], "instrument_identity_mismatch")

    def test_stale_data_is_rechecked_despite_cached_ok(self):
        self.snapshots[1] = snapshot("beta", size=0.01, stamp=6000)
        result = self.scan()
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["exclusions"][0]["reason"], "stale")

    def test_source_time_skew_excludes_fresh_but_unsynchronized_pairs(self):
        self.snapshots[1] = snapshot("beta", size=0.01, stamp=8999)
        result = self.scan()
        self.assertEqual(result["eligible_quotes"], 2)
        self.assertEqual(result["rows"], [])
        self.assertEqual(len(result["skipped_pairs"]), 2)
        self.assertEqual(result["skipped_pairs"][0]["reason"], "source_time_skew")

    def test_disconnected_and_unverified_sources_are_excluded(self):
        for source, reason in [("ws_ticker_unverified_book_time", "unverified_book_time"),
                               ("ticker", "unverified_book_source")]:
            self.snapshots[1] = snapshot("beta", size=0.01, source=source)
            result = self.scan()
            self.assertEqual(result["rows"], [])
            self.assertEqual(result["exclusions"][0]["reason"], reason)
        self.snapshots[1]["status"]["state"] = "reconnecting"
        self.assertEqual(self.scan()["exclusions"][0]["reason"], "exchange_not_streaming")

    def test_threshold_and_custom_budget(self):
        result = self.scan(min_spread_pct=0, capital_usdt=600)
        self.assertEqual(result["evaluated_pairs"], 2)
        self.assertEqual(result["displayed_pairs"], 1)
        self.assertEqual(result["capital_usdt"], 600)
        self.assertEqual(self.scan(min_spread_pct=3)["rows"], [])
        with self.assertRaises(ValueError):
            self.scan(capital_usdt=1001)

    def test_invalid_prices_or_future_receipt_never_form_a_pair(self):
        for bid, ask in [(104, 103), (float("inf"), 103), (99, 0), (None, 103)]:
            self.snapshots[1] = snapshot("beta", size=0.01, bid=bid, ask=ask)
            self.assertEqual(self.scan()["rows"], [])
        self.snapshots[1] = snapshot("beta", size=0.01)
        self.snapshots[1]["quotes"][0]["received_at_ms"] = 20000
        self.assertEqual(self.scan()["exclusions"][0]["reason"], "future_receipt_time")

    def test_same_exchange_is_never_paired_with_itself(self):
        self.snapshots = [snapshot("alpha"), snapshot("alpha")]
        self.assertEqual(self.scan()["rows"], [])

    def test_registry_rejects_ambiguous_and_invalid_bindings(self):
        for bindings in [[binding("alpha"), binding("alpha")], [binding("alpha", units="0")],
                         [binding("alpha", size="nan")]]:
            with self.assertRaises(ValueError):
                Registry(dict(version="bad", bindings=bindings))
        for document in [None, [], {"version": "bad", "bindings": [None]}]:
            with self.assertRaises(ValueError):
                Registry(document)

    def test_live_publication_persists_scan_and_clears_pairs_on_stop(self):
        import time
        timestamp = int(time.time()*1000)
        for item in self.snapshots:
            item["quotes"][0].update(source_timestamp_ms=timestamp, received_at_ms=timestamp)
        with TemporaryDirectory() as directory:
            args = SimpleNamespace(database=Path(directory)/"market.sqlite3", output=Path(directory)/"latest.json",
                                   registry=self.registry, capital_usdt=1000, max_skew_ms=1000, min_spread_pct=None, top=2)
            console = StringIO()
            with redirect_stdout(console):
                publish(args, self.snapshots, "WebSocket")
            document = json.loads(args.output.read_text(encoding="utf-8"))
            self.assertEqual(document["scanner"]["displayed_pairs"], 2)
            self.assertIn("OPEN %", console.getvalue())
            for item in self.snapshots:
                item["status"]["state"] = "stopped"
            with redirect_stdout(StringIO()):
                publish(args, self.snapshots, "WebSocket")
            self.assertEqual(json.loads(args.output.read_text(encoding="utf-8"))["scanner"]["rows"], [])
            with closing(sqlite3.connect(args.database)) as database:
                self.assertEqual(database.execute("SELECT COUNT(*) FROM scans").fetchone()[0], 2)
                first = json.loads(database.execute("SELECT payload FROM scans ORDER BY id LIMIT 1").fetchone()[0])
                self.assertEqual(first["registry_version"], "test")
                self.assertEqual(len(first["rows"]), 2)


if __name__ == "__main__":
    unittest.main()
