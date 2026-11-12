"""Sizing and execution cost helpers for the read-only monitor."""

from decimal import Decimal
from math import lcm

from .market import decimal, positive


class EconomicBlock(ValueError):
    pass


def common_step(first, second):
    first, second = positive(first), positive(second)
    places = max(0, -first.normalize().as_tuple().exponent, -second.normalize().as_tuple().exponent)
    if places > 18:
        raise EconomicBlock("Неподдерживаемая точность количества")
    scale = 10**places
    return Decimal(lcm(int(first*scale), int(second*scale))) / scale


def amount_step(market, mode):
    precision = market.get("precision", {}).get("amount")
    if mode == 4:  # CCXT TICK_SIZE
        return positive(precision)
    if mode == 2:  # CCXT DECIMAL_PLACES
        places = int(precision)
        if places != precision or abs(places) > 18:
            raise EconomicBlock("Неподдерживаемая точность количества")
        return Decimal(10)**(-places)
    raise EconomicBlock("Неподдерживаемый режим точности количества")


def fill(levels, quantity):
    remaining, value = quantity, Decimal(0)
    for price, available in levels:
        take = min(remaining, available)
        value += take*price
        remaining -= take
        if remaining == 0:
            return value/quantity, value
    raise EconomicBlock("Недостаточная глубина стакана")


def check_limits(market, contracts, notional):
    limits = market.get("limits", {})
    names = {"amount": "количество", "market": "количество рыночного ордера", "cost": "номинал"}
    for name, value in (("amount", contracts), ("market", contracts), ("cost", notional)):
        bounds = limits.get(name) or {}
        if bounds.get("min") is not None and value < decimal(bounds["min"]):
            raise EconomicBlock(f"Меньше минимума биржи: {names[name]}")
        if bounds.get("max") is not None and value > decimal(bounds["max"]):
            raise EconomicBlock(f"Больше максимума биржи: {names[name]}")
