"""Coarse, conditional funding/price/fee ranking before bounded depth checks."""
from decimal import Decimal

from .economics import fee_rate, funding_events
from .models import is_target
from .scanner import decimal, output_number, positive


def rank_candidates(rows, funding, markets, registry, timestamp, hold_hours=8,
                    fee_overrides=None, extra_cost_usdt=0):
    ranked = []
    for pair in rows:
        item = {"id": pair["id"], "asset": pair.get("asset"),
                "open_spread_pct": pair.get("open_spread_pct"),
                "status": "price_bootstrap", "score_usdt": None, "reasons": [],
                "classification": "coarse_conditional_ranking_not_depth_checked"}
        try:
            quantity = positive(pair["reference_base_quantity"])
            long_price, short_price = positive(pair["long_ask_usdt_per_unit"]), positive(pair["short_bid_usdt_per_unit"])
            midpoint = (long_price+short_price)/2
            rates, events = {}, []
            for side, price in (("long", long_price), ("short", short_price)):
                key = pair[f"{side}_exchange"], pair[f"{side}_symbol"]
                binding, market = registry.bindings.get(key), markets.get(key)
                if binding is None or market is None or not is_target(market):
                    raise ValueError("ranking_metadata_missing")
                if (binding["asset"] != pair["asset"] or market["base"] != binding["base"]
                        or positive(market["contractSize"]) != binding["contract_size"]):
                    raise ValueError("ranking_instrument_mismatch")
                rates[side], _ = fee_rate(key[0], market, fee_overrides or {})
                events.extend(funding_events(funding.get(key), timestamp,
                    timestamp+int(hold_hours*3600000), quantity, price, side))
            edge = quantity*(short_price-long_price)
            fees = quantity*(long_price*rates["long"]+short_price*rates["short"]
                             +midpoint*(rates["long"]+rates["short"]))
            projected_funding = sum((decimal(event["cashflow_usdt"]) for event in events), Decimal(0))
            score = edge+projected_funding-fees-decimal(extra_cost_usdt)
            item.update(status="price_funding_fee_estimate", score_usdt=output_number(score),
                        gross_edge_usdt=output_number(edge), funding_scenario_usdt=output_number(projected_funding),
                        four_fee_estimate_usdt=output_number(fees))
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            item["reasons"].append(str(exc))
        ranked.append(item)
    ranked.sort(key=lambda item: (item["score_usdt"] is None,
        -item["score_usdt"] if item["score_usdt"] is not None else -float(item.get("open_spread_pct") or 0), item["id"]))
    return ranked
