"""Sizing and execution cost helpers for the read-only monitor."""

from decimal import Decimal, ROUND_FLOOR
from math import lcm

from .contract_details import contract_limits
from .market import decimal, fresh, positive
from .sources import VENUES


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


def estimate_pair(instruments, books, budget, fees, now):
    stamps = [book['sourceAt'] for book in books]
    if not all(fresh(stamp, now, 3000) for stamp in stamps) or abs(stamps[0] - stamps[1]) > 1500:
        raise EconomicBlock('Нужны синхронные стаканы с биржевым временем')
    sides = [{side: [(decimal(price), decimal(size)) for price, size in book[side]]
              for side in ('asks', 'bids')} for book in books]
    left, right = sides
    sizes = [positive(market['contractSize']) for _, market in instruments]
    steps = [amount_step(market, source.client.precisionMode) * size
             for (source, market), size in zip(instruments, sizes)]
    step = common_step(*steps)
    # Size by the marginal cost of opening one additional base unit on both legs.
    quantity = Decimal(0)
    a = iter(left['asks'])
    b = iter(right['bids'])
    ask, aq = next(a)
    bid, bq = next(b)
    while budget > 0:
        take = min(aq, bq)
        level_cost = take * (ask + bid)
        if budget <= level_cost:
            quantity += budget / (ask + bid)
            break
        quantity += take
        budget -= level_cost
        aq -= take
        bq -= take
        if aq == 0:
            level = next(a, None)
            if level is None:
                break
            ask, aq = level
        if bq == 0:
            level = next(b, None)
            if level is None:
                break
            bid, bq = level
    quantity = (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if quantity <= 0:
        raise EconomicBlock('Бюджета недостаточно для общего шага объёма')
    entry = [fill(left['asks'], quantity), fill(right['bids'], quantity)]
    exit_ = [fill(left['bids'], quantity), fill(right['asks'], quantity)]
    for i, (source, market) in enumerate(instruments):
        contracts = quantity / sizes[i]
        maximum = (contract_limits(source.venue, market) or {}).get('marketMax')
        if maximum is not None and contracts > decimal(maximum):
            raise EconomicBlock(f'{VENUES[source.venue]}: превышен максимум одного рыночного ордера')
        for observations in (entry, exit_):
            check_limits(market, contracts, observations[i][1])
    fee_total = sum((entry[i][1] + exit_[i][1]) * fees[i] for i in range(2))
    gross = (exit_[0][1] - entry[0][1]) + (entry[1][1] - exit_[1][1])
    return dict(quantity=float(quantity), entry=[float(price) for price, _ in entry],
                exit=[float(price) for price, _ in exit_], used=float(entry[0][1] + entry[1][1]),
                fees=float(fee_total), gross=float(gross), net=float(gross - fee_total))
