"""Numeric validation and the local market identity registry."""

import json
import math
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .thresholds import FUTURE_CLOCK_TOLERANCE_MS, QUOTE_MAX_AGE_MS


def now_ms() -> int:
    return int(time.time()*1000)


def numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def is_target(market: dict[str, Any], currency: str = 'USDT') -> bool:
    return (market.get("swap") is True and market.get("linear") is True
            and market.get("quote") == currency and market.get("settle") == currency
            and market.get("active") is True)


def decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid numeric value") from None
    if not result.is_finite():
        raise ValueError("Nonfinite numeric value")
    return result


def positive(value: Any) -> Decimal:
    result = decimal(value)
    if result <= 0:
        raise ValueError("Expected a positive number")
    return result


class Registry:
    def __init__(self, document: Any) -> None:
        if not isinstance(document, dict) or not isinstance(document.get("bindings"), list):
            raise ValueError("Registry must be an object with a bindings list")
        version = document["version"]
        if not isinstance(version, str) or not version:
            raise ValueError("Registry version must be a nonempty string")
        self.version = version
        self.bindings: dict[tuple[str, str, str], dict[str, Any]] = {}
        assets_by_market: set[tuple[str, str, str, str]] = set()
        for entry in document["bindings"]:
            if not isinstance(entry, dict):
                raise ValueError("Registry binding must be an object")
            for key in ("exchange", "id", "asset", "base"):
                if not isinstance(entry.get(key), str) or not entry[key]:
                    raise ValueError(f"Registry entry requires {key}")
            kind = entry.get("kind", "swap")
            quote = entry.get("quote", "USDT" if kind == "swap" else None)
            if kind not in ("spot", "swap") or quote not in ("USDT", "USDC"):
                raise ValueError("Registry binding requires a supported market type and quote")
            key = entry["exchange"], kind, entry["id"]
            asset_key = entry["exchange"], kind, quote, entry["asset"]
            if key in self.bindings or asset_key in assets_by_market:
                raise ValueError("Ambiguous or duplicate registry binding")
            group = entry.get("group")
            if group is not None and (not isinstance(group, str) or not re.fullmatch(r"[A-Z0-9]{1,30}", group)):
                raise ValueError("Registry market group must be an ASCII ticker")
            binding: dict[str, Any] = dict(entry, kind=kind, quote=quote)
            if kind == "swap":
                units = positive(entry["price_units"])
                if units != units.to_integral_value() or units > 2**53 - 1:
                    raise ValueError("Registry price units must be a safe positive integer")
                binding['price_units'] = int(units)
                binding['contract_size'] = positive(entry['contract_size'])
            self.bindings[key] = binding
            assets_by_market.add(asset_key)

        assets = {asset for _, _, _, asset in assets_by_market}
        coingecko_ids = document.get("coingecko_ids", {})
        if not isinstance(coingecko_ids, dict) or any(
            asset not in assets or not isinstance(provider_id, str) or not re.fullmatch(r"[a-z0-9-]+", provider_id)
            for asset, provider_id in coingecko_ids.items()
        ) or len(set(coingecko_ids.values())) != len(coingecko_ids):
            raise ValueError("CoinGecko IDs must uniquely identify assets in the registry")
        self.coingecko_ids: dict[str, str] = coingecko_ids

    def match(self, exchange: str, kind: str, market: dict[str, Any]) -> dict[str, Any] | None:
        market_id = market.get('id')
        if not isinstance(market_id, str):
            return None
        binding = self.bindings.get((exchange, kind, market_id))
        if (not binding or market.get(kind) is not True or market.get("active") is not True
                or binding["base"] != market.get("base") or binding["quote"] != market.get("quote")):
            return None
        if kind == "swap":
            if (not is_target(market, binding["quote"])
                    or numeric(market.get("contractSize")) is None
                    or binding["contract_size"] != decimal(market["contractSize"])):
                return None
            underlying = binding.get("underlying")
            if underlying:
                info = market.get("info") or {}
                if info.get("uly") != underlying or info.get("ctValCcy") != binding["base"]:
                    return None
        return binding

    @classmethod
    def load(cls, path: Path | None = None) -> 'Registry':
        return cls(json.loads((path or Path(__file__).with_name("instruments.json")).read_text(encoding="utf-8")))


def fresh(stamp: int | float | None, now: int | float,
          max_age: int | float = QUOTE_MAX_AGE_MS) -> bool:
    return stamp is not None and -FUTURE_CLOCK_TOLERANCE_MS <= now - stamp <= max_age
