"""Sizing and execution cost helpers for the read-only monitor."""

from decimal import Decimal
from math import lcm

from .market import decimal, positive


class EconomicBlock(ValueError):
    pass


def common_step(first: Decimal, second: Decimal) -> Decimal:
    first, second = positive(first), positive(second)
    places = max(0, -first.normalize().as_tuple().exponent, -second.normalize().as_tuple().exponent)
    if places > 18:
        raise EconomicBlock("unsupported_quantity_precision")
    scale = 10**places
    return Decimal(lcm(int(first*scale), int(second*scale))) / scale


def amount_step(market: dict, mode: int) -> Decimal:
    precision = market.get("precision", {}).get("amount")
    if mode == 4:  # CCXT TICK_SIZE
        return positive(precision)
    if mode == 2:  # CCXT DECIMAL_PLACES
        places = int(precision)
        if places != precision or abs(places) > 18:
            raise EconomicBlock("unsupported_quantity_precision")
        return Decimal(10)**(-places)
    raise EconomicBlock("unsupported_precision_mode")


def fill(levels: list[tuple[Decimal, Decimal]], quantity: Decimal) -> tuple[Decimal, Decimal]:
    remaining, value = quantity, Decimal(0)
    for price, available in levels:
        take = min(remaining, available)
        value += take*price
        remaining -= take
        if remaining == 0:
            return value/quantity, value
    raise EconomicBlock("insufficient_depth")


def check_limits(market: dict, contracts: Decimal, notional: Decimal) -> None:
    limits = market.get("limits", {})
    for name, value in (("amount", contracts), ("market", contracts), ("cost", notional)):
        bounds = limits.get(name) or {}
        if bounds.get("min") is not None and value < decimal(bounds["min"]):
            raise EconomicBlock(f"below_{name}_minimum")
        if bounds.get("max") is not None and value > decimal(bounds["max"]):
            raise EconomicBlock(f"above_{name}_maximum")


def economic_message(code: str) -> str:
    messages = {
        'insufficient_depth': 'В стакане недостаточно ликвидности для этого объёма',
        'unsupported_quantity_precision': 'Точность объёма контракта недоступна',
        'unsupported_precision_mode': 'Биржа не сообщила поддерживаемый шаг объёма',
        'below_amount_minimum': 'Объём меньше минимального количества контрактов',
        'above_amount_maximum': 'Объём больше максимального количества контрактов',
        'below_market_minimum': 'Объём меньше минимума рыночного ордера',
        'above_market_maximum': 'Объём больше максимума рыночного ордера',
        'below_cost_minimum': 'Номинал меньше минимального номинала ордера',
        'above_cost_maximum': 'Номинал больше максимального номинала ордера',
    }
    return messages.get(code, code)
