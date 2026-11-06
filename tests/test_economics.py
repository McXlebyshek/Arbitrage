from copy import deepcopy
from decimal import Decimal
import unittest

from arbitrage.economics import (EconomicBlock, amount_step, common_step, evaluate_pair, fill,
                                 funding_events, normalize_book, normalize_funding)
from arbitrage.scanner import Registry

NOW = 1700000000000
HOUR = 3600000


def funding(interval=8, rate=0.001):
    history = [{"timestamp": NOW-hours*HOUR, "fundingRate": rate} for hours in range(48, 0, -interval)]
    return normalize_funding(dict(interval=f"{interval}h", fundingRate=rate, fundingTimestamp=NOW+HOUR),
                             history, NOW, NOW)


def fixture():
    bindings, legs = [], {}
    for exchange, bid, ask, step in (("alpha", 99, 100, 0.003), ("beta", 102, 103, 0.002)):
        bindings.append(dict(exchange=exchange, id="BTC_USDT", asset="bitcoin", base="BTC", price_units="1", contract_size="1"))
        market = dict(id="BTC_USDT", symbol="BTC/USDT:USDT", base="BTC", quote="USDT", settle="USDT",
                      swap=True, linear=True, active=True, contractSize=1, precision={"amount": step},
                      limits={"amount": {"min": step}, "cost": {"min": 5}}, taker=0.0005, percentage=True)
        legs[(exchange, "BTC_USDT")] = dict(market=market, precision_mode=4, received_at_ms=NOW,
            book=dict(symbol="BTC/USDT:USDT", timestamp=NOW, bids=[[bid, 100]], asks=[[ask, 100]]),
            funding=funding(8 if exchange == "alpha" else 4),
            liquidity={"quote_volume_24h_usdt": 5000000, "received_at_ms": NOW})
    pair = dict(id="test", asset="bitcoin", long_exchange="alpha", short_exchange="beta",
                long_symbol="BTC_USDT", short_symbol="BTC_USDT")
    return pair, legs, Registry(dict(version="economics-test", bindings=bindings))


class EconomicsTests(unittest.TestCase):
    def calculate(self, **options):
        pair, legs, registry = fixture()
        return evaluate_pair(pair, legs, registry, NOW, **options)

    def test_rational_common_lot_and_precision_modes(self):
        self.assertEqual(common_step("0.002", "0.003"), Decimal("0.006"))
        self.assertEqual(common_step("0.0001", "0.001"), Decimal("0.001"))
        self.assertEqual(amount_step({"precision": {"amount": 3}}, 2), Decimal("0.001"))
        with self.assertRaises(EconomicBlock):
            amount_step({"precision": {"amount": 3}}, 3)

    def test_vwap_uses_depth_without_separate_slippage_deduction(self):
        vwap, value = fill([(Decimal(100), Decimal(1)), (Decimal(102), Decimal(3))], Decimal(2))
        self.assertEqual((vwap, value), (Decimal(101), Decimal(202)))
        with self.assertRaises(EconomicBlock):
            fill([(Decimal(100), Decimal(1))], Decimal(2))

    def test_equal_base_quantity_fits_steps_and_budget_including_entry_fees(self):
        result = self.calculate()
        self.assertEqual(result["status"], "scenario_estimated")
        self.assertEqual(result["base_quantity"], 4.944)
        self.assertEqual(result["long_contracts"], result["short_contracts"])
        self.assertLessEqual(result["entry_budget_used_usdt"], 1000)
        self.assertAlmostEqual(result["four_fee_convergence_estimate_usdt"], 0.998688)
        self.assertAlmostEqual(result["convergence_net_before_funding_usdt"], 8.889312)
        self.assertAlmostEqual(result["close_now_net_before_funding_usdt"], -10.886688)
        self.assertIsNone(result["net_pnl_usdt"])
        self.assertFalse(result["execution_verified"])
        self.assertFalse(result["account_fees_verified"])

    def test_funding_schedule_uses_each_exchange_events_and_signs(self):
        result = self.calculate()
        self.assertEqual(len(result["funding_events"]), 3)
        self.assertEqual([event["side"] for event in result["funding_events"]], ["long", "short", "short"])
        expected = 4.944*(2*102.5-99.5)*0.001
        self.assertAlmostEqual(result["funding_scenario_usdt"], expected)
        self.assertAlmostEqual(result["convergence_net_scenario_usdt"], 8.889312+expected)

    def test_missing_funding_does_not_become_zero(self):
        pair, legs, registry = fixture()
        legs[("beta", "BTC_USDT")]["funding"] = None
        result = evaluate_pair(pair, legs, registry, NOW)
        self.assertEqual(result["status"], "entry_checked")
        self.assertIn("funding_missing", result["reasons"])
        self.assertNotIn("convergence_net_scenario_usdt", result)

    def test_zero_horizon_has_no_funding_events(self):
        self.assertEqual(funding_events(None, NOW, NOW, Decimal(1), Decimal(100), "long"), [])

    def test_negative_funding_long_receives_and_short_pays(self):
        data = funding(8, -0.001)
        long = funding_events(data, NOW, NOW+8*HOUR, Decimal(2), Decimal(100), "long")
        short = funding_events(data, NOW, NOW+8*HOUR, Decimal(2), Decimal(100), "short")
        self.assertEqual(long[0]["cashflow_usdt"], 0.2)
        self.assertEqual(short[0]["cashflow_usdt"], -0.2)

    def test_volume_filter_fails_closed(self):
        for value, expected in [(None, "volume_24h_unknown"), (100, "volume_24h_below_threshold")]:
            pair, legs, registry = fixture()
            legs[("beta", "BTC_USDT")]["liquidity"]["quote_volume_24h_usdt"] = value
            self.assertIn(expected, evaluate_pair(pair, legs, registry, NOW)["reasons"])

    def test_stale_depth_skew_and_unknown_time_block(self):
        for stamp, expected in [(NOW-4000, "depth_stale"), (NOW+2000, "depth_future_time"),
                                (None, "depth_source_time_unknown"), (NOW-2000, "depth_time_skew")]:
            pair, legs, registry = fixture()
            legs[("beta", "BTC_USDT")]["book"]["timestamp"] = stamp
            result = evaluate_pair(pair, legs, registry, NOW)
            self.assertEqual(result["status"], "blocked")
            self.assertIn(expected, result["reasons"])

    def test_thin_depth_or_small_budget_is_not_reported_as_full_position(self):
        pair, legs, registry = fixture()
        legs[("beta", "BTC_USDT")]["book"].update(bids=[[102, 1]], asks=[[103, 1]])
        result = evaluate_pair(pair, legs, registry, NOW)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("insufficient_budget_utilization", result["reasons"])

    def test_minimum_notional_and_fee_missing_are_explicit(self):
        pair, legs, registry = fixture()
        legs[("beta", "BTC_USDT")]["market"]["limits"]["cost"]["min"] = 1000
        self.assertIn("below_cost_minimum", evaluate_pair(pair, legs, registry, NOW)["reasons"])
        legs[("beta", "BTC_USDT")]["market"]["taker"] = None
        self.assertIn("taker_fee_unknown", evaluate_pair(pair, legs, registry, NOW)["reasons"])
        self.assertEqual(self.calculate(fee_overrides={"alpha": -0.1})["status"], "blocked")

    def test_contract_units_and_price_package_are_normalized(self):
        pair, legs, registry = fixture()
        binding = dict(registry.bindings[("beta", "BTC_USDT")], price_units=Decimal(1000), contract_size=Decimal("0.01"))
        market = dict(legs[("beta", "BTC_USDT")]["market"], contractSize=0.01)
        book = dict(timestamp=NOW, bids=[[100000, 2]], asks=[[101000, 3]])
        normalized = normalize_book(book, binding, market, NOW, NOW)
        self.assertEqual(normalized["bids"][0], (Decimal(100), Decimal(20)))
        self.assertEqual(normalized["units_per_contract"], Decimal(10))

    def test_future_rate_field_is_not_mistaken_for_nearest_payment(self):
        history = [{"timestamp": NOW-hours*HOUR, "fundingRate": 0.001} for hours in range(48, 0, -8)]
        result = normalize_funding(dict(fundingRate=0.001, fundingTimestamp=NOW+HOUR,
                                      nextFundingRate=0.1, nextFundingTimestamp=NOW+9*HOUR), history, NOW, NOW)
        self.assertEqual(result["rate"], 0.001)
        self.assertEqual(result["funding_timestamp_ms"], NOW+HOUR)
        self.assertEqual(result["interval_source"], "inferred_from_history")
        self.assertTrue(result["history_24h_covered"])

    def test_incomplete_history_or_expired_schedule_cannot_complete_scenario(self):
        for change, expected in [({"history_24h_covered": False}, "funding_history_incomplete"),
                                 ({"received_at_ms": NOW-180000}, "funding_stale"),
                                 ({"funding_timestamp_ms": NOW-1}, "funding_schedule_unknown_or_expired")]:
            pair, legs, registry = fixture()
            legs[("beta", "BTC_USDT")]["funding"].update(change)
            result = evaluate_pair(pair, legs, registry, NOW)
            self.assertEqual(result["status"], "entry_checked")
            self.assertIn(expected, result["reasons"])

    def test_crossed_or_duplicate_depth_is_rejected(self):
        pair, legs, registry = fixture()
        for bids, asks, expected in [([[104, 10]], [[103, 10]], "crossed_depth"),
                                     ([[102, 10], [102, 5]], [[103, 10]], "invalid_depth_levels")]:
            legs[("beta", "BTC_USDT")]["book"].update(bids=bids, asks=asks)
            self.assertIn(expected, evaluate_pair(pair, legs, registry, NOW)["reasons"])
