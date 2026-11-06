"""Preliminary directed price spreads; no profitability or execution claim."""
from collections import defaultdict
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from itertools import permutations
import json
import math
from pathlib import Path

from .models import Quote, is_target


def decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid numeric value") from None
    if not result.is_finite():
        raise ValueError("Nonfinite numeric value")
    return result


def positive(value):
    result = decimal(value)
    if result <= 0:
        raise ValueError("Expected a positive number")
    return result


def output_number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Calculated value exceeds numeric range")
    return result


class Registry:
    def __init__(self, document):
        if not isinstance(document, dict) or not isinstance(document.get("bindings"), list):
            raise ValueError("Registry must be an object with a bindings list")
        self.version = document["version"]
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("Registry version must be a nonempty string")
        self.bindings = {}
        assets_by_exchange = set()
        for entry in document["bindings"]:
            if not isinstance(entry, dict):
                raise ValueError("Registry binding must be an object")
            for key in ("exchange", "id", "asset", "base"):
                if not isinstance(entry.get(key), str) or not entry[key]:
                    raise ValueError(f"Registry entry requires {key}")
            key = entry["exchange"], entry["id"]
            asset_key = entry["exchange"], entry["asset"]
            if key in self.bindings or asset_key in assets_by_exchange:
                raise ValueError("Ambiguous or duplicate registry binding")
            self.bindings[key] = dict(entry, price_units=positive(entry["price_units"]),
                                      contract_size=positive(entry["contract_size"]))
            assets_by_exchange.add(asset_key)

    @classmethod
    def load(cls, path=None):
        return cls(json.loads((path or Path(__file__).with_name("instruments.json")).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class InstrumentQuote:
    quote: Quote
    asset: str
    bid: Decimal
    ask: Decimal
    base_units_per_contract: Decimal


def match_quote(exchange, row, markets, registry, timestamp):
    binding = registry.bindings.get((exchange, row.get("exchange_symbol")))
    if binding is None:
        raise ValueError("unregistered_instrument")
    market = markets.get(row.get("exchange_symbol"))
    if market is None:
        raise ValueError("missing_instrument_metadata")
    if not is_target(market):
        raise ValueError("not_active_linear_usdt_perpetual")
    if (row.get("exchange") != exchange or row.get("symbol") != market.get("symbol")
            or row.get("base") != binding["base"] or market.get("base") != binding["base"]
            or row.get("quote") != "USDT"):
        raise ValueError("instrument_identity_mismatch")
    size = positive(market.get("contractSize"))
    if size != binding["contract_size"] or positive(row.get("contract_size")) != size:
        raise ValueError("contract_size_changed_or_inconsistent")
    quote = Quote(**{field.name: row[field.name] for field in fields(Quote)})
    # Validate numeric fields independently of any cached quality label in JSON.
    bid, ask = positive(quote.bid), positive(quote.ask)
    for value in (quote.source_timestamp_ms, quote.received_at_ms):
        positive(value)
    quality = quote.quality(timestamp)
    if quality != "ok":
        raise ValueError(quality)
    if quote.source != "ws_bbo":
        raise ValueError("unverified_book_source")
    if quote.received_at_ms > timestamp + 1000:
        raise ValueError("future_receipt_time")
    units = binding["price_units"]
    return InstrumentQuote(quote, binding["asset"], bid / units, ask / units, size * units)


def pair_row(long, short, capital, timestamp):
    entry_edge = short.bid - long.ask
    # Equal underlying amount, with a ceiling for the SUM of both entry notionals.
    quantity = capital / (long.ask + short.bid)
    return {
        "id": f"{long.asset}|{long.quote.exchange}:{long.quote.exchange_symbol}|{short.quote.exchange}:{short.quote.exchange_symbol}",
        "asset": long.asset,
        "long_exchange": long.quote.exchange, "short_exchange": short.quote.exchange,
        "long_symbol": long.quote.exchange_symbol, "short_symbol": short.quote.exchange_symbol,
        "long_ask_usdt_per_unit": output_number(long.ask),
        "short_bid_usdt_per_unit": output_number(short.bid),
        "open_edge_usdt_per_unit": output_number(entry_edge),
        "open_spread_pct": output_number(100 * entry_edge / long.ask),
        "reference_base_quantity": output_number(quantity),
        "reference_long_notional_usdt": output_number(quantity * long.ask),
        "reference_short_notional_usdt": output_number(quantity * short.bid),
        "reference_long_contracts_unrounded": output_number(quantity / long.base_units_per_contract),
        "reference_short_contracts_unrounded": output_number(quantity / short.base_units_per_contract),
        "long_base_units_per_contract": output_number(long.base_units_per_contract),
        "short_base_units_per_contract": output_number(short.base_units_per_contract),
        "long_source_timestamp_ms": long.quote.source_timestamp_ms,
        "short_source_timestamp_ms": short.quote.source_timestamp_ms,
        "long_age_ms": timestamp - long.quote.source_timestamp_ms,
        "short_age_ms": timestamp - short.quote.source_timestamp_ms,
        "source_time_skew_ms": abs(long.quote.source_timestamp_ms - short.quote.source_timestamp_ms),
        "classification": "preliminary_price_spread", "sizing_status": "illustrative_unrounded",
        "net_pnl_usdt": None, "execution_verified": False,
    }


def scan(snapshots, registry, timestamp, capital_usdt=1000, max_skew_ms=1000, min_spread_pct=None):
    capital = positive(capital_usdt)
    if capital > 1000:
        raise ValueError("MVP combined notional ceiling is 1000 USDT")
    if not isinstance(max_skew_ms, int) or max_skew_ms < 0:
        raise ValueError("max_skew_ms must be a nonnegative integer")
    threshold = decimal(min_spread_pct) if min_spread_pct is not None else None
    grouped, exclusions, rows, skipped_pairs = defaultdict(list), [], [], []
    seen = set()
    for snapshot in snapshots:
        status = snapshot["status"]
        exchange = status["exchange"]
        markets = {m["id"]: m for m in snapshot.get("instruments", [])}
        for row in snapshot.get("quotes", []):
            reason = None
            key = exchange, row.get("exchange_symbol")
            try:
                if status.get("state") != "streaming":
                    raise ValueError("exchange_not_streaming")
                if key in seen:
                    raise ValueError("duplicate_quote")
                seen.add(key)
                matched = match_quote(exchange, row, markets, registry, timestamp)
                grouped[matched.asset].append(matched)
            except (ValueError, TypeError, KeyError) as exc:
                reason = str(exc)
            if reason is not None:
                exclusions.append({"exchange": exchange, "symbol": row.get("exchange_symbol"), "reason": reason})
    evaluated = 0
    for asset, quotes in grouped.items():
        for long, short in permutations(quotes, 2):
            if long.quote.exchange == short.quote.exchange:
                continue
            if abs(long.quote.source_timestamp_ms - short.quote.source_timestamp_ms) > max_skew_ms:
                skipped_pairs.append({"asset": asset, "long_exchange": long.quote.exchange,
                                      "short_exchange": short.quote.exchange, "reason": "source_time_skew"})
                continue
            try:
                result = pair_row(long, short, capital, timestamp)
            except (ValueError, ArithmeticError):
                skipped_pairs.append({"asset": asset, "long_exchange": long.quote.exchange,
                                      "short_exchange": short.quote.exchange, "reason": "numeric_range"})
                continue
            evaluated += 1
            if threshold is None or decimal(result["open_spread_pct"]) >= threshold:
                rows.append(result)
    rows.sort(key=lambda row: (-row["open_spread_pct"], row["id"]))
    return {"generated_at_ms": timestamp, "registry_version": registry.version,
            "classification": "preliminary_price_spreads", "currency": "USDT",
            "capital_usdt": output_number(capital), "max_source_time_skew_ms": max_skew_ms,
            "max_quote_age_ms": 3000, "min_spread_pct": output_number(threshold) if threshold is not None else None,
            "eligible_quotes": sum(len(quotes) for quotes in grouped.values()),
            "evaluated_pairs": evaluated, "displayed_pairs": len(rows),
            "positive_displayed_pairs": sum(row["open_spread_pct"] > 0 for row in rows),
            "exclusions": exclusions, "skipped_pairs": skipped_pairs, "rows": rows,
            "not_included": ["funding", "fees", "depth_vwap", "lot_rounding", "exit_prices", "other_costs"]}
