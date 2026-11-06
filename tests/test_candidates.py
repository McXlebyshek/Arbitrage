from copy import deepcopy
from decimal import Decimal
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from arbitrage.candidates import rank_candidates
from arbitrage.economic_data import EconomicData
from test_economics import NOW, fixture, funding
from test_economic_data import arguments
from arbitrage.__main__ import publish
from arbitrage.scanner import Registry
from test_scanner import snapshot, binding


def ranking_fixture():
    pair, legs, registry = fixture()
    pair.update(reference_base_quantity=5, long_ask_usdt_per_unit=100, short_bid_usdt_per_unit=99.9, open_spread_pct=-0.1)
    pair["id"] = "negative-price-positive-funding"
    prices = dict(pair, id="positive-price-negative-funding", long_exchange="beta", short_exchange="alpha",
                  long_ask_usdt_per_unit=99.9, short_bid_usdt_per_unit=100, open_spread_pct=0.1001)
    market_map = {key: leg["market"] for key, leg in legs.items()}
    fund = {("alpha", "BTC_USDT"): funding(8, -0.01), ("beta", "BTC_USDT"): funding(4, 0.01)}
    return [prices, pair], market_map, fund, registry


class CandidateTests(unittest.TestCase):
    def test_raw_table_filter_does_not_hide_negative_spreads_from_economic_universe(self):
        now = int(time.time()*1000)
        snapshots = [snapshot("alpha", stamp=now), snapshot("beta", bid=102, ask=103, stamp=now)]
        for item in snapshots:
            item["quotes"][0]["received_at_ms"] = now
        class Engine:
            received = None
            def submit(self, scanner):
                self.received = scanner
            def snapshot(self, scanner, timestamp):
                return {"state": "ready", "rows": [], "generated_at_ms": timestamp}
        with TemporaryDirectory() as directory:
            engine = Engine()
            args = SimpleNamespace(database=Path(directory)/"test.sqlite3", output=Path(directory)/"latest.json",
                registry=Registry(dict(version="filter-test", bindings=[binding("alpha"), binding("beta")])),
                capital_usdt=1000, max_skew_ms=1000, min_spread_pct=0, economic_engine=engine, top=0)
            with redirect_stdout(StringIO()):
                visible = publish(args, snapshots, "WebSocket")
            self.assertEqual(visible["displayed_pairs"], 1)
            self.assertEqual(len(engine.received["rows"]), 2)
            self.assertTrue(any(row["open_spread_pct"] < 0 for row in engine.received["rows"]))

    def test_negative_price_spread_can_outrank_positive_spread_due_to_funding(self):
        rows, markets, fund, registry = ranking_fixture()
        ranked = rank_candidates(rows, fund, markets, registry, NOW)
        self.assertEqual(ranked[0]["id"], "negative-price-positive-funding")
        self.assertEqual(ranked[0]["status"], "price_funding_fee_estimate")
        self.assertLess(ranked[0]["open_spread_pct"], 0)
        self.assertGreater(ranked[0]["funding_scenario_usdt"], 0)
        self.assertGreater(ranked[0]["score_usdt"], ranked[1]["score_usdt"])

    def test_unknown_or_stale_funding_is_labelled_bootstrap_not_zero(self):
        rows, markets, fund, registry = ranking_fixture()
        ranked = rank_candidates(rows, {}, markets, registry, NOW)
        self.assertIsNone(ranked[0]["score_usdt"])
        self.assertEqual(ranked[0]["status"], "price_bootstrap")
        self.assertIn("funding_missing", ranked[0]["reasons"])
        ranked = rank_candidates(rows, fund, markets, registry, NOW+180000)
        self.assertIn("funding_stale", ranked[0]["reasons"])

    def test_four_fees_and_costs_reduce_coarse_score(self):
        rows, markets, fund, registry = ranking_fixture()
        first = rank_candidates(rows, fund, markets, registry, NOW)
        second = rank_candidates(rows, fund, markets, registry, NOW, extra_cost_usdt=2)
        self.assertAlmostEqual(first[0]["score_usdt"]-second[0]["score_usdt"], 2)
        self.assertGreater(first[0]["four_fee_estimate_usdt"], 0)
        self.assertEqual(first[0]["classification"], "coarse_conditional_ranking_not_depth_checked")

    def test_hold_preserves_candidate_but_missing_ws_row_drops_it_immediately(self):
        rows, markets, fund, registry = ranking_fixture()
        args = arguments(registry)
        args.economics_pairs, args.candidate_hold_seconds = 1, 10
        pool = SimpleNamespace(exchange="alpha", selected={})
        engine = EconomicData(args, [pool])
        engine.legs = {key: {"market": value} for key, value in markets.items()}
        with patch("arbitrage.economic_data.now_ms", return_value=NOW):
            engine.submit({"rows": rows})
        original = engine.candidates[0]["id"]
        engine.funding = fund
        with patch("arbitrage.economic_data.now_ms", return_value=NOW+1000):
            engine.submit({"rows": rows})
            self.assertEqual(engine.candidates[0]["id"], original)
        with patch("arbitrage.economic_data.now_ms", return_value=NOW+11000):
            engine.submit({"rows": rows})
            self.assertEqual(engine.candidates[0]["id"], "negative-price-positive-funding")
        with patch("arbitrage.economic_data.now_ms", return_value=NOW+12000):
            engine.submit({"rows": []})
            self.assertEqual(engine.candidates, [])
            self.assertEqual(engine.selected_at, {})

    def test_funding_universe_includes_registered_non_candidate_symbols_only(self):
        rows, markets, fund, registry = ranking_fixture()
        args = arguments(registry)
        selected = {"BTC_USDT": markets[("alpha", "BTC_USDT")],
                    "UNKNOWN_USDT": dict(markets[("alpha", "BTC_USDT")], id="UNKNOWN_USDT")}
        pool = SimpleNamespace(exchange="alpha", selected=selected)
        engine = EconomicData(args, [pool])
        self.assertEqual(engine.candidates, [])
        self.assertEqual(engine.funding_selection(), {"alpha": {"BTC_USDT"}})
