"""Depth-based sizing and explicitly conditional economics for linear USDT swaps."""
from decimal import Decimal, ROUND_FLOOR
from math import lcm
import re

from .models import is_target
from .scanner import decimal, positive, output_number


class EconomicBlock(ValueError):
    pass


def common_step(first, second):
    first, second = positive(first), positive(second)
    places = max(0, -first.normalize().as_tuple().exponent, -second.normalize().as_tuple().exponent)
    if places > 18:
        raise EconomicBlock("unsupported_quantity_precision")
    scale = 10**places
    return Decimal(lcm(int(first*scale), int(second*scale))) / scale


def amount_step(market, mode):
    precision = market.get("precision", {}).get("amount")
    if mode == 4:  # CCXT TICK_SIZE
        return positive(precision)
    if mode == 2:  # CCXT DECIMAL_PLACES
        places = int(precision)
        if places != precision or abs(places) > 18:
            raise EconomicBlock("unsupported_quantity_precision")
        return Decimal(10)**(-places)
    raise EconomicBlock("unsupported_precision_mode")


def normalize_book(book, binding, market, received_at_ms, timestamp):
    if not is_target(market) or market["id"] != binding["id"] or market["base"] != binding["base"]:
        raise EconomicBlock("depth_instrument_mismatch")
    size = positive(market["contractSize"])
    if size != binding["contract_size"]:
        raise EconomicBlock("depth_contract_size_changed")
    source_time = book.get("timestamp")
    if source_time is None:
        raise EconomicBlock("depth_source_time_unknown")
    age = timestamp-int(source_time)
    if age < -1000 or timestamp-received_at_ms < -1000:
        raise EconomicBlock("depth_future_time")
    if age > 3000 or timestamp-received_at_ms > 3000:
        raise EconomicBlock("depth_stale")
    units = binding["price_units"]
    sides = {}
    for side in ("bids", "asks"):
        levels, seen = [], set()
        for row in book.get(side, []):
            price, quantity = positive(row[0])/units, decimal(row[1])*size*units
            if quantity < 0 or price in seen:
                raise EconomicBlock("invalid_depth_levels")
            seen.add(price)
            if quantity:
                levels.append((price, quantity))
        if not levels:
            raise EconomicBlock("empty_depth_side")
        sides[side] = sorted(levels, reverse=side == "bids")
    if sides["bids"][0][0] >= sides["asks"][0][0]:
        raise EconomicBlock("crossed_depth")
    return dict(sides, source_timestamp_ms=int(source_time), received_at_ms=received_at_ms,
                units_per_contract=size*units)


def fill(levels, quantity):
    remaining, value = quantity, Decimal(0)
    for price, available in levels:
        take = min(remaining, available)
        value += take*price
        remaining -= take
        if remaining == 0:
            return value/quantity, value
    raise EconomicBlock("insufficient_depth")


def check_limits(market, contracts, notional):
    limits = market.get("limits", {})
    for name, value in (("amount", contracts), ("market", contracts), ("cost", notional)):
        bounds = limits.get(name) or {}
        if bounds.get("min") is not None and value < decimal(bounds["min"]):
            raise EconomicBlock(f"below_{name}_minimum")
        if bounds.get("max") is not None and value > decimal(bounds["max"]):
            raise EconomicBlock(f"above_{name}_maximum")


def fee_rate(exchange, market, overrides):
    if exchange in overrides:
        raw, source = overrides[exchange], "configured_taker_rate"
    else:
        raw, source = market.get("taker"), "public_ccxt_metadata_estimate"
    if exchange not in overrides and exchange == "gate" and market.get("info", {}).get("taker_fee_rate") is not None:
        raw, source = market["info"]["taker_fee_rate"], "public_contract_fee_estimate"
    if raw is None or market.get("percentage") is False:
        raise EconomicBlock("taker_fee_unknown")
    rate = decimal(raw)
    if not 0 <= rate <= Decimal("0.01"):
        raise EconomicBlock("invalid_taker_fee")
    return rate, source


def normalize_funding(raw, history, received_at_ms, history_received_at_ms):
    events = {}
    for row in history:
        stamp = row.get("timestamp")
        if stamp is None:
            continue
        rate = decimal(row.get("fundingRate"))
        stamp = int(stamp)
        if stamp > received_at_ms:
            continue
        if stamp in events and events[stamp] != rate:
            raise EconomicBlock("conflicting_funding_history")
        events[stamp] = rate
    ordered = sorted(events)
    interval, interval_source = None, "unknown"
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", raw.get("interval") or "")
    if match:
        interval = int(match[1])*{"s": 1000, "m": 60000, "h": 3600000, "d": 86400000}[match[2]]
        interval_source = "public_funding_interval"
    elif len(ordered) >= 3:
        differences = [b-a for a, b in zip(ordered, ordered[1:])]
        rounded = round(differences[-1]/60000)*60000
        if rounded > 0 and all(abs(difference-rounded) <= 60000 for difference in differences):
            interval, interval_source = rounded, "inferred_from_history"
    span = ordered[-1]-ordered[0] if ordered else 0
    gaps_ok = interval is not None and all(abs((b-a)-interval) <= 60000 for a, b in zip(ordered, ordered[1:]))
    coverage = bool(ordered and interval and gaps_ok and span >= 24*3600000-1000
                    and received_at_ms-ordered[-1] <= interval+60000)
    rate = raw.get("fundingRate")
    # fundingTimestamp belongs to fundingRate; nextFundingRate may refer to TWO periods ahead.
    return {"rate": output_number(decimal(rate)) if rate is not None else None,
            "funding_timestamp_ms": raw.get("fundingTimestamp"), "interval_ms": interval,
            "interval_source": interval_source, "received_at_ms": received_at_ms,
            "history_received_at_ms": history_received_at_ms,
            "history_24h_covered": coverage,
            "history": [{"timestamp": stamp, "rate": output_number(events[stamp])} for stamp in ordered]}


def funding_events(data, start_ms, end_ms, quantity, reference_price, side):
    if end_ms <= start_ms:
        return []
    if data is None:
        raise EconomicBlock("funding_missing")
    if start_ms-data["received_at_ms"] > 120000 or start_ms-data["received_at_ms"] < -1000:
        raise EconomicBlock("funding_stale")
    if start_ms-data["history_received_at_ms"] > 1800000 or not data["history_24h_covered"]:
        raise EconomicBlock("funding_history_incomplete")
    first, interval, rate = data["funding_timestamp_ms"], data["interval_ms"], data["rate"]
    if first is None or interval is None or rate is None or int(first) <= start_ms:
        raise EconomicBlock("funding_schedule_unknown_or_expired")
    if interval < 60000 or interval > 86400000:
        raise EconomicBlock("unsupported_funding_interval")
    events = []
    stamp = int(first)
    while stamp <= end_ms:
        if len(events) >= 1500:
            raise EconomicBlock("too_many_funding_events")
        cashflow = quantity*reference_price*decimal(rate)*(-1 if side == "long" else 1)
        events.append({"timestamp": stamp, "side": side, "assumed_rate": rate,
                       "cashflow_usdt": output_number(cashflow)})
        stamp += interval
    return events


def evaluate_pair(pair, legs, registry, timestamp, capital=1000, hold_hours=8,
                  max_impact=Decimal("0.001"), min_utilization=Decimal("0.9"), extra_cost=0,
                  fee_overrides=None, max_skew_ms=1000, min_volume_usdt=1000000):
    result = {"id": pair["id"], "status": "blocked", "reasons": [], "net_pnl_usdt": None,
              "execution_verified": False, "classification": "conditional_economics"}
    result.update({key: pair.get(key) for key in ("asset", "long_exchange", "short_exchange", "long_symbol", "short_symbol")})
    try:
        books, markets, rates, fee_sources, bindings, funding = {}, {}, {}, {}, {}, {}
        for side in ("long", "short"):
            key = (pair[f"{side}_exchange"], pair[f"{side}_symbol"])
            if key not in legs:
                raise EconomicBlock("awaiting_depth")
            leg = legs[key]
            binding = registry.bindings.get(key)
            if binding is None or binding["asset"] != pair["asset"]:
                raise EconomicBlock("unregistered_economic_instrument")
            market = leg["market"]
            if market["id"] != key[1] or market["symbol"] != leg["book"].get("symbol"):
                raise EconomicBlock("depth_symbol_mismatch")
            books[side] = normalize_book(leg["book"], binding, market, leg["received_at_ms"], timestamp)
            markets[side], bindings[side] = market, binding
            rates[side], fee_sources[side] = fee_rate(key[0], market, fee_overrides or {})
            funding[side] = leg.get("funding")
            if min_volume_usdt > 0:
                liquidity = leg.get("liquidity")
                if liquidity is None or liquidity.get("quote_volume_24h_usdt") is None:
                    raise EconomicBlock("volume_24h_unknown")
                if timestamp-liquidity["received_at_ms"] > 120000 or timestamp-liquidity["received_at_ms"] < -1000:
                    raise EconomicBlock("volume_24h_stale")
                if decimal(liquidity["quote_volume_24h_usdt"]) < decimal(min_volume_usdt):
                    raise EconomicBlock("volume_24h_below_threshold")
        if abs(books["long"]["source_timestamp_ms"]-books["short"]["source_timestamp_ms"]) > max_skew_ms:
            raise EconomicBlock("depth_time_skew")
        budget, extra = positive(capital), decimal(extra_cost)
        if budget > 1000 or extra < 0:
            raise EconomicBlock("invalid_economic_budget")
        steps = {side: amount_step(markets[side], legs[(pair[f"{side}_exchange"], pair[f"{side}_symbol"])]["precision_mode"])
                 *books[side]["units_per_contract"] for side in ("long", "short")}
        step = common_step(steps["long"], steps["short"])
        best_entry_sum = books["long"]["asks"][0][0]+books["short"]["bids"][0][0]
        upper = int((budget/best_entry_sum/step).to_integral_value(rounding=ROUND_FLOOR))
        for side in ("long", "short"):
            for bound in ("amount", "market"):
                maximum = (markets[side].get("limits", {}).get(bound) or {}).get("max")
                if maximum is not None:
                    upper = min(upper, int(decimal(maximum)*books[side]["units_per_contract"]/step))

        def calculate(lots):
            quantity = step*lots
            fills = {}
            for name, side, depth_side in (("long_entry", "long", "asks"), ("short_entry", "short", "bids"),
                                           ("long_exit_now", "long", "bids"), ("short_exit_now", "short", "asks")):
                price, value = fill(books[side][depth_side], quantity)
                best = books[side][depth_side][0][0]
                impact = price/best-1 if depth_side == "asks" else 1-price/best
                if impact > max_impact:
                    raise EconomicBlock("price_impact_exceeded")
                fills[name] = {"price": price, "value": value, "impact": impact}
            entry_fees = fills["long_entry"]["value"]*rates["long"]+fills["short_entry"]["value"]*rates["short"]
            entry_cash = fills["long_entry"]["value"]+fills["short_entry"]["value"]+entry_fees+extra
            if entry_cash > budget:
                raise EconomicBlock("budget_exceeded")
            for side, name in (("long", "long_entry"), ("short", "short_entry")):
                maximum = (markets[side].get("limits", {}).get("cost") or {}).get("max")
                if maximum is not None and fills[name]["value"] > decimal(maximum):
                    raise EconomicBlock("above_cost_maximum")
            return quantity, fills, entry_fees, entry_cash

        lower, feasible = 1, None
        while lower <= upper:
            middle = (lower+upper)//2
            try:
                values = calculate(middle)
                feasible, lower = values, middle+1
            except EconomicBlock:
                upper = middle-1
        if feasible is None:
            raise EconomicBlock("no_feasible_common_quantity")
        quantity, fills, entry_fees, entry_cash = feasible
        for side, name in (("long", "long_entry"), ("short", "short_entry")):
            contracts = quantity/books[side]["units_per_contract"]
            check_limits(markets[side], contracts, fills[name]["value"])
        entry_notional = fills["long_entry"]["value"]+fills["short_entry"]["value"]
        if entry_notional/budget < min_utilization:
            raise EconomicBlock("insufficient_budget_utilization")
        gross_edge = fills["short_entry"]["value"]-fills["long_entry"]["value"]
        exit_now_fees = fills["long_exit_now"]["value"]*rates["long"]+fills["short_exit_now"]["value"]*rates["short"]
        close_now_gross = gross_edge+fills["long_exit_now"]["value"]-fills["short_exit_now"]["value"]
        exit_reference = (books["long"]["asks"][0][0]+books["long"]["bids"][0][0]
                         +books["short"]["asks"][0][0]+books["short"]["bids"][0][0])/4
        future_exit_fees = quantity*exit_reference*(rates["long"]+rates["short"])
        convergence_before_funding = gross_edge-entry_fees-future_exit_fees-extra
        result.update(status="entry_checked", base_quantity=output_number(quantity),
            long_contracts=output_number(quantity/books["long"]["units_per_contract"]),
            short_contracts=output_number(quantity/books["short"]["units_per_contract"]),
            common_base_step=output_number(step), entry_budget_used_usdt=output_number(entry_cash),
            entry_notional_usdt=output_number(entry_notional),
            entry_fee_estimate_usdt=output_number(entry_fees), four_fee_convergence_estimate_usdt=output_number(entry_fees+future_exit_fees),
            fee_rates={side: output_number(rate) for side, rate in rates.items()}, fee_sources=fee_sources,
            account_fees_verified=False, extra_cost_assumption_usdt=output_number(extra),
            gross_entry_edge_usdt=output_number(gross_edge),
            close_now_net_before_funding_usdt=output_number(close_now_gross-entry_fees-exit_now_fees-extra),
            convergence_net_before_funding_usdt=output_number(convergence_before_funding),
            break_even_exit_edge_usdt_per_unit=output_number((entry_fees+future_exit_fees+extra-gross_edge)/quantity),
            exit_assumption="equal_future_exit_prices_without_exit_depth_or_extra_slippage",
            exit_reference_price_usdt=output_number(exit_reference), hold_hours=hold_hours,
            depth_source_times={side: book["source_timestamp_ms"] for side, book in books.items()},
            fills={name: {key: output_number(value) for key, value in values.items()} for name, values in fills.items()},
            funding_inputs=funding)
        result["volume_filter_min_usdt"] = min_volume_usdt
        try:
            events = []
            for side in ("long", "short"):
                # Constant current midpoint is an explicit forecast notional assumption.
                reference = (books[side]["bids"][0][0]+books[side]["asks"][0][0])/2
                events.extend(funding_events(funding[side], timestamp, timestamp+int(hold_hours*3600000), quantity, reference, side))
            net_funding = sum((decimal(event["cashflow_usdt"]) for event in events), Decimal(0))
            result.update(status="scenario_estimated", funding_events=sorted(events, key=lambda event:(event["timestamp"], event["side"])),
                funding_scenario_usdt=output_number(net_funding),
                convergence_net_scenario_usdt=output_number(convergence_before_funding+net_funding),
                funding_assumption="constant_current_rate_interval_and_midpoint_not_actual_cashflows")
        except (ValueError, TypeError, KeyError) as exc:
            result["reasons"].append(str(exc))
        return result
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        result["reasons"].append(str(exc))
        return result
