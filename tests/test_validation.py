from copy import deepcopy
import unittest

from arbitrage.economics import evaluate_pair
from arbitrage.validation import PROFILES, audit_result
from validate_economics_matrix import calculate, summary
from test_economics import fixture, NOW


class ValidationTests(unittest.TestCase):
    def test_matrix_covers_directions_profiles_and_keeps_attempts_separate(self):
        pair, legs, registry = fixture()
        batch = dict(asset="bitcoin", timestamp_ms=NOW, bindings=list(registry.bindings.values()),
                     legs=[dict(exchange=key[0], native_id=key[1], **leg) for key, leg in legs.items()])
        batch["cases"] = calculate(batch, registry)
        self.assertEqual(len(batch["cases"]), 24)
        identities = {(case["pair"]["id"], case["profile"]["capital"], case["profile"]["hold_hours"])
                      for case in batch["cases"]}
        self.assertEqual(len(identities), 24)
        second = deepcopy(batch)
        second["cases"][0]["result"].update(status="blocked", reasons=["depth_time_skew"])
        result = summary(dict(batches=[second, batch], input_errors=[]))
        self.assertEqual(result["unique_cases"]["cases"], 24)
        self.assertEqual(result["unique_cases"]["statuses"], {"scenario_estimated": 24})
        self.assertEqual(result["all_attempts"]["cases"], 48)
        self.assertEqual(result["all_attempts"]["statuses"]["blocked"], 1)
        self.assertTrue(all(value["cases"] == 2 for value in result["by_profile"].values()))

    def test_all_profiles_preserve_accounting_and_zero_horizon(self):
        pair, legs, registry = fixture()
        self.assertEqual(len(PROFILES), 12)
        for profile in PROFILES:
            with self.subTest(profile=profile):
                result = evaluate_pair(pair, legs, registry, NOW, **profile)
                result["validation_timestamp_ms"] = NOW
                self.assertEqual(result["status"], "scenario_estimated")
                self.assertEqual(audit_result(result, pair, legs, registry, profile), [])
                if profile["hold_hours"] == 0:
                    self.assertEqual(result["funding_events"], [])

    def test_audit_detects_budget_quantity_fee_and_funding_corruption(self):
        pair, legs, registry = fixture()
        profile = dict(capital=1000, hold_hours=8)
        original = evaluate_pair(pair, legs, registry, NOW, **profile)
        original["validation_timestamp_ms"] = NOW
        for field, value, error in (("entry_budget_used_usdt", 1001, "budget_exceeded"),
                                    ("short_contracts", 1, "short_base_quantity"),
                                    ("four_fee_convergence_estimate_usdt", 0, "four_fees"),
                                    ("funding_scenario_usdt", 123, "funding_sum")):
            result = dict(original, **{field: value})
            self.assertIn(error, audit_result(result, pair, legs, registry, profile))
        result = deepcopy(original)
        result["funding_events"][0]["cashflow_usdt"] *= -1
        self.assertIn("funding_cashflow_sign_and_value", audit_result(result, pair, legs, registry, profile))
        result = deepcopy(original)
        result["funding_events"].pop()
        self.assertIn("short_funding_schedule", audit_result(result, pair, legs, registry, profile))

    def test_blocked_case_requires_reason_and_never_claims_execution(self):
        pair, legs, registry = fixture()
        result = evaluate_pair(pair, {}, registry, NOW)
        self.assertEqual(audit_result(result, pair, {}, registry, PROFILES[0]), [])
        result.update(reasons=[], execution_verified=True)
        self.assertEqual(set(audit_result(result, pair, {}, registry, PROFILES[0])),
                         {"blocked_without_reason", "conditional_result_claims_execution"})
