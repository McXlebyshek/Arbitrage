"""Normalize public books and estimate two-leg execution economics."""

from decimal import ROUND_FLOOR, Decimal
from typing import Any

from .contract_details import contract_limits
from .economics import EconomicBlock, amount_step, check_limits, common_step, fill
from .market import decimal, fresh, numeric, positive
from .sources import VENUES, Source
from .thresholds import BOOK_MAX_AGE_MS, SOURCE_TIME_SKEW_MS


def normalized_book(book: dict[str, Any], size: float, received: int, *, clock: Any | None = None) -> dict[str, Any]:
    raw_stamp = numeric(book.get('timestamp'))
    stamp = clock.correct(raw_stamp) if clock is not None else raw_stamp
    if stamp is not None and not fresh(stamp, received, BOOK_MAX_AGE_MS):
        raise ValueError('стакан просрочен')
    sides = {}
    for side in ('bids', 'asks'):
        levels = []
        for row in book.get(side, [])[:50]:
            price, amount = map(numeric, row[:2])
            if price and amount and price > 0 and amount > 0:
                levels.append([price, amount * size])
        sides[side] = sorted(levels, reverse=side == 'bids')
    if not sides['bids'] or not sides['asks'] or sides['bids'][0][0] >= sides['asks'][0][0]:
        raise ValueError('пустой или пересечённый стакан')
    result = dict(**sides, sourceAt=stamp, receivedAt=received)
    if raw_stamp != stamp:
        result['sourceAtRaw'] = raw_stamp
    return result


def estimate_pair(instruments: list[tuple[Source, dict[str, Any]]], books: list[dict[str, Any]],
                  budget: Decimal, fees: tuple[Decimal, Decimal] | list[Decimal], now: int, *,
                  require_full_entry: bool = False) -> dict[str, Any]:
    stamps = [book['sourceAt'] for book in books]
    if (not all(fresh(stamp, now, BOOK_MAX_AGE_MS) for stamp in stamps)
            or abs(stamps[0] - stamps[1]) > SOURCE_TIME_SKEW_MS):
        raise EconomicBlock('Нужны синхронные стаканы с биржевым временем')  # noqa: RUF001
    sides = [{side: [(decimal(price), decimal(size)) for price, size in book[side]]
              for side in ('asks', 'bids')} for book in books]
    left, right = sides
    sizes = [positive(market['contractSize']) for _, market in instruments]
    steps = [amount_step(market, source.client.precisionMode) * size
             for (source, market), size in zip(instruments, sizes, strict=True)]
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
            budget = Decimal(0)
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
    if require_full_entry and budget > 0:
        raise EconomicBlock('Недостаточно глубины для заданного номинала обеих ног')
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
