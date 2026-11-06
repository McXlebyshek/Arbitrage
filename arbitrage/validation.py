"""Independent accounting checks for saved conditional economic results."""
from decimal import Decimal
from .scanner import decimal


PROFILES = [{"capital": capital, "hold_hours": hours}
            for capital in (100, 500, 1000) for hours in (0, 4, 8, 24)]


def audit_result(result, pair, legs, registry, profile):
    errors = []
    tolerance = Decimal("0.00000001")

    def equal(label, actual, expected):
        if abs(decimal(actual)-expected) > tolerance*max(Decimal(1), abs(expected)):
            errors.append(label)

    if result.get("net_pnl_usdt") is not None or result.get("execution_verified") is not False:
        errors.append("conditional_result_claims_execution")
    if result["status"] == "blocked":
        if not result.get("reasons"):
            errors.append("blocked_without_reason")
        return errors
    quantity = decimal(result["base_quantity"])
    if quantity <= 0:
        errors.append("nonpositive_quantity")
    if decimal(result["entry_budget_used_usdt"]) > decimal(profile["capital"])+tolerance:
        errors.append("budget_exceeded")
    if decimal(result["entry_notional_usdt"]) < decimal(profile["capital"])*Decimal("0.9")-tolerance:
        errors.append("utilization_below_policy")
    rates = {side: decimal(result["fee_rates"][side]) for side in ("long", "short")}
    fills = result["fills"]
    entry_fees = Decimal(0)
    for side in ("long", "short"):
        key = pair[f"{side}_exchange"], pair[f"{side}_symbol"]
        market, binding = legs[key]["market"], registry.bindings[key]
        contracts = decimal(result[f"{side}_contracts"])
        equal(f"{side}_base_quantity", contracts*decimal(market["contractSize"])*binding["price_units"], quantity)
        precision = market["precision"]["amount"]
        step = decimal(precision) if legs[key]["precision_mode"] == 4 else Decimal(10)**(-int(precision))
        lots = contracts/step
        if abs(lots-lots.to_integral_value()) > Decimal("0.000001"):
            errors.append(f"{side}_quantity_step")
        notional = decimal(fills[f"{side}_entry"]["value"])
        for name, value in (("amount", contracts), ("market", contracts), ("cost", notional)):
            bounds = market.get("limits", {}).get(name) or {}
            if bounds.get("min") is not None and value < decimal(bounds["min"])-tolerance:
                errors.append(f"{side}_{name}_minimum")
            if bounds.get("max") is not None and value > decimal(bounds["max"])+tolerance:
                errors.append(f"{side}_{name}_maximum")
        entry_fees += notional*rates[side]
    entry_notional = sum((decimal(fills[f"{side}_entry"]["value"]) for side in rates), Decimal(0))
    extra = decimal(result["extra_cost_assumption_usdt"])
    equal("entry_fees", result["entry_fee_estimate_usdt"], entry_fees)
    equal("entry_notional", result["entry_notional_usdt"], entry_notional)
    equal("entry_budget", result["entry_budget_used_usdt"], entry_notional+entry_fees+extra)
    exit_fees = quantity*decimal(result["exit_reference_price_usdt"])*sum(rates.values())
    equal("four_fees", result["four_fee_convergence_estimate_usdt"], entry_fees+exit_fees)
    edge = decimal(fills["short_entry"]["value"])-decimal(fills["long_entry"]["value"])
    equal("gross_edge", result["gross_entry_edge_usdt"], edge)
    equal("convergence_before_funding", result["convergence_net_before_funding_usdt"], edge-entry_fees-exit_fees-extra)
    immediate_fees = sum((decimal(fills[f"{side}_exit_now"]["value"])*rates[side] for side in rates), Decimal(0))
    immediate_gross = edge+decimal(fills["long_exit_now"]["value"])-decimal(fills["short_exit_now"]["value"])
    equal("close_now_accounting", result["close_now_net_before_funding_usdt"], immediate_gross-entry_fees-immediate_fees-extra)
    if result["status"] == "scenario_estimated":
        events = result["funding_events"]
        for side in ("long", "short"):
            data = legs[(pair[f"{side}_exchange"], pair[f"{side}_symbol"])].get("funding")
            expected = []
            if profile["hold_hours"] > 0 and data:
                stamp, end = data["funding_timestamp_ms"], result["validation_timestamp_ms"]+profile["hold_hours"]*3600000
                while stamp <= end:
                    expected.append(stamp)
                    stamp += data["interval_ms"]
            if [event["timestamp"] for event in events if event["side"] == side] != expected:
                errors.append(f"{side}_funding_schedule")
        for event in events:
            if not 0 < event["timestamp"]-result["validation_timestamp_ms"] <= profile["hold_hours"]*3600000:
                errors.append("funding_event_outside_horizon")
            side = event["side"]
            key = pair[f"{side}_exchange"], pair[f"{side}_symbol"]
            leg, binding = legs[key], registry.bindings[key]
            midpoint = (decimal(leg["book"]["bids"][0][0])+decimal(leg["book"]["asks"][0][0]))/2/binding["price_units"]
            rate = decimal(leg["funding"]["rate"])
            equal("funding_rate", event["assumed_rate"], rate)
            equal("funding_cashflow_sign_and_value", event["cashflow_usdt"], quantity*midpoint*rate*(-1 if side == "long" else 1))
        total = sum((decimal(event["cashflow_usdt"]) for event in events), Decimal(0))
        equal("funding_sum", result["funding_scenario_usdt"], total)
        equal("convergence_with_funding", result["convergence_net_scenario_usdt"], edge-entry_fees-exit_fees-extra+total)
    return errors
