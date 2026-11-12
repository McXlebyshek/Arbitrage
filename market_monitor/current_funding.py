"""Normalize current public funding quotes through per-venue adapters."""

import re

from .funding_history import DAY
from .market import numeric
from .sources import CYCLE_FUNDING_VENUES


def bybit(values, raw, market, info, instrument, venue, now, schedule):
    hours, rate, stamp, cap, floor = values
    hours = hours or numeric(info.get('fundingIntervalHour'))
    minutes = numeric(instrument.get('fundingInterval'))
    hours = hours or (minutes / 60 if minutes else None)
    rate = numeric(info.get('fundingRate')) if rate is None else rate
    stamp = numeric(info.get('nextFundingTime')) or stamp
    cap, floor = numeric(instrument.get('upperFundingRate')), numeric(instrument.get('lowerFundingRate'))
    return hours, rate, stamp, cap, floor


def gate(values, raw, market, info, instrument, venue, now, schedule):
    hours, rate, stamp, cap, floor = values
    seconds = numeric(info.get('funding_interval') or instrument.get('funding_interval'))
    hours = hours or (seconds / 3600 if seconds else None)
    cap = numeric(info.get('funding_rate_limit') or instrument.get('funding_rate_limit'))
    floor = -cap if cap else None
    if rate is None:
        rate = numeric(info.get('funding_rate'))
    next_seconds = numeric(info.get('funding_next_apply') or instrument.get('funding_next_apply'))
    if stamp is None and next_seconds is not None:
        stamp = next_seconds * 1000
    return hours, rate, stamp, cap, floor


def bitget(values, raw, market, info, instrument, venue, now, schedule):
    hours, rate, stamp, cap, floor = values
    hours = hours or numeric(info.get('fundingRateInterval') or instrument.get('fundInterval'))
    stamp = numeric(info.get('nextUpdate') or info.get('nextFundingTime')) or stamp
    cap, floor = numeric(info.get('maxFundingRate')), numeric(info.get('minFundingRate'))
    return hours, rate, stamp, cap, floor


def mexc(values, raw, market, info, instrument, venue, now, schedule):
    hours, rate, stamp, cap, floor = values
    hours = hours or numeric(info.get('collectCycle'))
    rate = numeric(info.get('fundingRate')) if rate is None else rate
    stamp = numeric(info.get('nextSettleTime')) or stamp
    cap, floor = numeric(info.get('maxFundingRate')), numeric(info.get('minFundingRate'))
    return hours, rate, stamp, cap, floor


def binanceusdm_aster(values, raw, market, info, instrument, venue, now, schedule):
    hours, rate, stamp, cap, floor = values
    if venue == 'aster':
        stamp = numeric(raw.get('nextFundingTimestamp')) or stamp
    adjustment = schedule.get(market['symbol']) if schedule is not None else None
    if adjustment:
        rules = adjustment.get('info') or {}
        hours = hours or numeric(rules.get('fundingIntervalHours'))
        if venue == 'aster':
            cap, floor = numeric(rules.get('fundingFeeCap')), numeric(rules.get('fundingFeeFloor'))
        else:
            cap, floor = numeric(rules.get('adjustedFundingRateCap')), numeric(rules.get('adjustedFundingRateFloor'))
    elif venue == 'binanceusdm' and schedule is not None and stamp and stamp % (8 * 3_600_000) == 0:
        # FundingInfo lists adjustments only; accept the standard schedule
        # after a successful response and only at a standard settlement time.
        hours = hours or 8
    return hours, rate, stamp, cap, floor


def kucoinfutures(values, raw, market, info, instrument, venue, now, schedule):
    hours, rate, stamp, cap, floor = values
    period = numeric(info.get('granularity') or info.get('currentGranularity'))
    hours = hours or (period / 3_600_000 if period else None)
    stamp = numeric(info.get('fundingTime')) or stamp
    cap, floor = numeric(info.get('fundingRateCap')), numeric(info.get('fundingRateFloor'))
    return hours, rate, stamp, cap, floor


def whitebit(values, raw, market, info, instrument, venue, now, schedule):
    hours, rate, stamp, cap, floor = values
    minutes = numeric(info.get('funding_interval_minutes'))
    hours = hours or (minutes / 60 if minutes else None)
    cap, floor = numeric(info.get('funding_cap')), numeric(info.get('funding_floor'))
    return hours, rate, stamp, cap, floor


def edgex(values, raw, market, info, instrument, venue, now, schedule):
    hours, rate, stamp, cap, floor = values
    minutes = numeric(info.get('fundingRateIntervalMin'))
    hours = minutes / 60 if minutes else None
    rate = numeric(info.get('fundingRate'))
    stamp = numeric(info.get('fundingTime'))
    if stamp is not None and stamp <= now and hours is not None:
        stamp += hours * 3_600_000
    return hours, rate, stamp, cap, floor


def htx(values, raw, market, info, instrument, venue, now, schedule):
    hours, rate, stamp, cap, floor = values
    following = numeric(info.get('next_funding_time'))
    current = numeric(info.get('funding_time'))
    if current and following and following > current:
        hours = hours or (following - current) / 3_600_000
    stamp = current
    if hours is None and stamp and schedule is not None:
        terms = schedule.get(market['symbol'], {}).get('info', {})
        if numeric(terms.get('settlement_date')) == stamp:
            hours = numeric(terms.get('settlement_period'))
    rate = numeric(info.get('funding_rate'))
    cap, floor = numeric(info.get('max_funding_rate')), numeric(info.get('min_funding_rate'))
    return hours, rate, stamp, cap, floor


def okx(values, raw, market, info, instrument, venue, now, schedule):
    hours, rate, stamp, cap, floor = values
    rate = numeric(info.get('fundingRate')) if rate is None else rate
    stamp = numeric(info.get('fundingTime')) or stamp
    following = numeric(raw.get('nextFundingTimestamp') or info.get('nextFundingTime'))
    if hours is None and stamp and following and following > stamp:
        hours = (following - stamp) / 3_600_000
    cap, floor = numeric(info.get('maxFundingRate')), numeric(info.get('minFundingRate'))
    return hours, rate, stamp, cap, floor


ADAPTERS = {
    'bybit': bybit,
    'gate': gate,
    'bitget': bitget,
    'mexc': mexc,
    'binanceusdm': binanceusdm_aster,
    'aster': binanceusdm_aster,
    'kucoinfutures': kucoinfutures,
    'whitebit': whitebit,
    'edgex': edgex,
    'htx': htx,
    'okx': okx,
}


def funding_fields(raw, market, venue, now, schedule=None):
    info = raw.get('info') or {}
    instrument = market.get('info') or {}
    interval = re.fullmatch(r'(\d+(?:\.\d+)?)h', str(raw.get('interval')))
    hours = float(interval[1]) if interval else None
    rate = numeric(raw.get('fundingRate'))
    stamp = numeric(raw.get('fundingTimestamp'))
    cap = floor = None
    adapter = ADAPTERS.get(venue)
    if adapter is not None:
        hours, rate, stamp, cap, floor = adapter(
            (hours, rate, stamp, cap, floor), raw, market, info, instrument, venue, now, schedule,
        )
    next_at = int(stamp) if stamp and now < stamp <= now + DAY else None
    if venue in CYCLE_FUNDING_VENUES and next_at is None:
        rate = hours = None
    return dict(rate=rate * 100 if rate is not None else None,
                hours=hours if hours and 0 < hours <= 24 else None, next=next_at,
                cap=cap * 100 if cap is not None else None,
                floor=floor * 100 if floor is not None else None)
