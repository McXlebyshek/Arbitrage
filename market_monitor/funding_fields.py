"""Normalize upcoming funding terms from public exchange responses."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

from .funding_history import DAY
from .market import numeric
from .sources import CYCLE_FUNDING_VENUES

HOUR_MS = 3_600_000
INTERVAL = re.compile(r'(\d+(?:\.\d+)?)h')
Fields = Mapping[str, Any]


class FundingFields(TypedDict):
    rate: float | None
    hours: float | None
    next: int | None
    cap: float | None
    floor: float | None


@dataclass(frozen=True)
class FundingInput:
    raw: Fields
    market: Fields
    now: int
    schedule: Mapping[str, Fields] | None

    @property
    def info(self) -> Fields:
        return self.raw.get('info') or {}

    @property
    def instrument(self) -> Fields:
        return self.market.get('info') or {}


@dataclass
class FundingTerms:
    rate: float | None
    hours: float | None
    stamp: float | None
    cap: float | None = None
    floor: float | None = None


def bybit(context: FundingInput, terms: FundingTerms) -> None:
    info, instrument = context.info, context.instrument
    minutes = numeric(instrument.get('fundingInterval'))
    terms.hours = terms.hours or numeric(info.get('fundingIntervalHour')) or (minutes / 60 if minutes else None)
    if terms.rate is None:
        terms.rate = numeric(info.get('fundingRate'))
    terms.stamp = numeric(info.get('nextFundingTime')) or terms.stamp
    terms.cap = numeric(instrument.get('upperFundingRate'))
    terms.floor = numeric(instrument.get('lowerFundingRate'))


def gate(context: FundingInput, terms: FundingTerms) -> None:
    info, instrument = context.info, context.instrument
    seconds = numeric(info.get('funding_interval') or instrument.get('funding_interval'))
    terms.hours = terms.hours or (seconds / 3600 if seconds else None)
    terms.cap = numeric(info.get('funding_rate_limit') or instrument.get('funding_rate_limit'))
    terms.floor = -terms.cap if terms.cap else None
    if terms.rate is None:
        terms.rate = numeric(info.get('funding_rate'))
    next_seconds = numeric(info.get('funding_next_apply') or instrument.get('funding_next_apply'))
    if terms.stamp is None and next_seconds is not None:
        terms.stamp = next_seconds * 1000


def bitget(context: FundingInput, terms: FundingTerms) -> None:
    info = context.info
    terms.hours = terms.hours or numeric(info.get('fundingRateInterval') or context.instrument.get('fundInterval'))
    terms.stamp = numeric(info.get('nextUpdate') or info.get('nextFundingTime')) or terms.stamp
    terms.cap, terms.floor = numeric(info.get('maxFundingRate')), numeric(info.get('minFundingRate'))


def mexc(context: FundingInput, terms: FundingTerms) -> None:
    info = context.info
    terms.hours = terms.hours or numeric(info.get('collectCycle'))
    if terms.rate is None:
        terms.rate = numeric(info.get('fundingRate'))
    terms.stamp = numeric(info.get('nextSettleTime')) or terms.stamp
    terms.cap, terms.floor = numeric(info.get('maxFundingRate')), numeric(info.get('minFundingRate'))


def schedule_adjustment(context: FundingInput, terms: FundingTerms, cap: str, floor: str) -> bool:
    adjustment = context.schedule.get(context.market['symbol']) if context.schedule is not None else None
    if not adjustment:
        return False
    rules = adjustment.get('info') or {}
    terms.hours = terms.hours or numeric(rules.get('fundingIntervalHours'))
    terms.cap, terms.floor = numeric(rules.get(cap)), numeric(rules.get(floor))
    return True


def binance(context: FundingInput, terms: FundingTerms) -> None:
    adjusted = schedule_adjustment(context, terms, 'adjustedFundingRateCap', 'adjustedFundingRateFloor')
    if not adjusted and context.schedule is not None and terms.stamp and terms.stamp % (8 * HOUR_MS) == 0:
        # A successful FundingInfo response omits contracts on the standard schedule.
        terms.hours = terms.hours or 8


def aster(context: FundingInput, terms: FundingTerms) -> None:
    terms.stamp = numeric(context.raw.get('nextFundingTimestamp')) or terms.stamp
    schedule_adjustment(context, terms, 'fundingFeeCap', 'fundingFeeFloor')


def kucoin(context: FundingInput, terms: FundingTerms) -> None:
    info = context.info
    period = numeric(info.get('granularity') or info.get('currentGranularity'))
    terms.hours = terms.hours or (period / HOUR_MS if period else None)
    terms.stamp = numeric(info.get('fundingTime')) or terms.stamp
    terms.cap, terms.floor = numeric(info.get('fundingRateCap')), numeric(info.get('fundingRateFloor'))


def whitebit(context: FundingInput, terms: FundingTerms) -> None:
    info = context.info
    minutes = numeric(info.get('funding_interval_minutes'))
    terms.hours = terms.hours or (minutes / 60 if minutes else None)
    terms.cap, terms.floor = numeric(info.get('funding_cap')), numeric(info.get('funding_floor'))


def edgex(context: FundingInput, terms: FundingTerms) -> None:
    info = context.info
    minutes = numeric(info.get('fundingRateIntervalMin'))
    terms.hours = minutes / 60 if minutes else None
    terms.rate = numeric(info.get('fundingRate'))
    terms.stamp = numeric(info.get('fundingTime'))
    if terms.stamp is not None and terms.stamp <= context.now and terms.hours is not None:
        terms.stamp += terms.hours * HOUR_MS


def htx(context: FundingInput, terms: FundingTerms) -> None:
    info = context.info
    following = numeric(info.get('next_funding_time'))
    current = numeric(info.get('funding_time'))
    if current and following and following > current:
        terms.hours = terms.hours or (following - current) / HOUR_MS
    terms.stamp = current
    if terms.hours is None and current and context.schedule is not None:
        schedule = context.schedule.get(context.market['symbol'], {}).get('info', {})
        if numeric(schedule.get('settlement_date')) == current:
            terms.hours = numeric(schedule.get('settlement_period'))
    terms.rate = numeric(info.get('funding_rate'))
    terms.cap, terms.floor = numeric(info.get('max_funding_rate')), numeric(info.get('min_funding_rate'))


def okx(context: FundingInput, terms: FundingTerms) -> None:
    info = context.info
    if terms.rate is None:
        terms.rate = numeric(info.get('fundingRate'))
    terms.stamp = numeric(info.get('fundingTime')) or terms.stamp
    following = numeric(context.raw.get('nextFundingTimestamp') or info.get('nextFundingTime'))
    if terms.hours is None and terms.stamp and following and following > terms.stamp:
        terms.hours = (following - terms.stamp) / HOUR_MS
    terms.cap, terms.floor = numeric(info.get('maxFundingRate')), numeric(info.get('minFundingRate'))


ADAPTERS: dict[str, Callable[[FundingInput, FundingTerms], None]] = {
    'bybit': bybit, 'gate': gate, 'bitget': bitget, 'mexc': mexc, 'binanceusdm': binance,
    'aster': aster, 'kucoinfutures': kucoin, 'whitebit': whitebit, 'edgex': edgex, 'htx': htx, 'okx': okx,
}


def funding_fields(raw: Fields, market: Fields, venue: str, now: int,
                   schedule: Mapping[str, Fields] | None = None) -> FundingFields:
    interval = INTERVAL.fullmatch(str(raw.get('interval')))
    terms = FundingTerms(numeric(raw.get('fundingRate')), float(interval[1]) if interval else None,
                         numeric(raw.get('fundingTimestamp')))
    adapter = ADAPTERS.get(venue)
    if adapter is not None:
        adapter(FundingInput(raw, market, now, schedule), terms)
    next_at = int(terms.stamp) if terms.stamp and now < terms.stamp <= now + DAY else None
    if venue in CYCLE_FUNDING_VENUES and next_at is None:
        terms.rate = terms.hours = None
    return {
        'rate': terms.rate * 100 if terms.rate is not None else None,
        'hours': terms.hours if terms.hours and 0 < terms.hours <= 24 else None,
        'next': next_at,
        'cap': terms.cap * 100 if terms.cap is not None else None,
        'floor': terms.floor * 100 if terms.floor is not None else None,
    }
