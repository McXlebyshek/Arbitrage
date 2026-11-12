"""Public funding events and coverage of settlement windows."""

import asyncio

from .market import decimal


DAY = 86400000


def cadence_stats(context, tolerance_ms=1000):
    """Observed cadence only; raw event timestamps and boundaries stay intact."""
    gaps = [b['timestamp']-a['timestamp'] for a,b in zip(context, context[1:])]
    nominal = round(gaps[0]/60000)*60000 if gaps else None
    deviation = max(abs(gap-nominal) for gap in gaps) if gaps else None
    regular = bool(gaps) and 60000 <= nominal <= DAY and deviation <= tolerance_ms
    return dict(regular_interval_ms=nominal if regular else None, cadence_tolerance_ms=tolerance_ms,
                max_cadence_deviation_ms=deviation, max_observed_gap_ms=max(gaps) if gaps else None)


def window_stats(events, end_ms, days, tolerance_ms=1000):
    start = end_ms-days*DAY
    ordered = sorted(events, key=lambda x: x["timestamp"])
    rows = [row for row in ordered if start < row["timestamp"] <= end_ms]
    anchors = [row for row in ordered if row["timestamp"] <= start]
    context = ([anchors[-1]] if anchors else [])+rows
    cadence = cadence_stats(context, tolerance_ms)
    nominal = cadence['regular_interval_ms']
    regular = nominal is not None
    covered = bool(anchors and rows and regular and end_ms-rows[-1]["timestamp"] <= nominal+tolerance_ms)
    rates = [decimal(row["rate"]) for row in rows]
    return dict(days=days, start_exclusive_ms=start, end_inclusive_ms=end_ms, events=len(rows),
        coverage="regular_observed_coverage" if covered else "partial_or_irregular",
        has_left_boundary_anchor=bool(anchors), **cadence,
        latest_event_age_ms=end_ms-rows[-1]["timestamp"] if rows else None,
        mean_rate=float(sum(rates)/len(rates)) if rates else None,
        min_rate=float(min(rates)) if rates else None, max_rate=float(max(rates)) if rates else None,
        interpretation="historical_public_rates_not_account_cashflows_or_future_forecast")


async def fetch_history(client, exchange, symbol, start_ms, end_ms, max_pages=12):
    events, requests, error = {}, 0, None
    cursor, previous = (start_ms if exchange == "binanceusdm" else end_ms), None
    for page in range(1, max_pages+1):
        params = {}
        since = start_ms
        if exchange == "binanceusdm":
            since, params = cursor, {"until": end_ms}
        elif exchange == 'kucoinfutures':
            # CCXT maps until->to. Keep since unset so its local limit selects
            # the newest 100 records, not the oldest 100 of the API response.
            since, params = None, {'from': start_ms, 'until': cursor, 'uta': False}
        elif exchange in ("bybit", "gate"):
            params = {"until": cursor}
        elif exchange == 'htx':
            # The linear v5 API uses an inclusive end_time in milliseconds.
            params = {'end_time': cursor}
        elif exchange == "okx":
            since, params = None, {"after": str(cursor+1)}
        elif exchange in ("bitget", "mexc"):
            since, params = None, {"pageNo" if exchange == "bitget" else "page_num": page}
        try:
            requests += 1
            async with asyncio.timeout(25):
                rows = await client.fetch_funding_rate_history(symbol, since=since, limit=100, params=params)
            normalized = {}
            for row in rows:
                if row.get("symbol") != symbol or not isinstance(row.get("timestamp"), int):
                    raise ValueError("History instrument or timestamp mismatch")
                stamp, rate = row["timestamp"], str(decimal(row["fundingRate"]))
                if exchange == 'kucoinfutures' and not start_ms <= stamp <= cursor:
                    raise ValueError('KuCoin history response outside requested range')
                if exchange == 'htx' and stamp > cursor:
                    raise ValueError('HTX history response outside requested range')
                if stamp > end_ms:
                    continue
                if stamp in normalized and normalized[stamp] != rate:
                    raise ValueError("Conflicting funding rates inside page")
                normalized[stamp] = rate
            if not normalized:
                break
            fingerprint = tuple(sorted(normalized.items()))
            if fingerprint == previous:
                raise ValueError("History pagination did not advance")
            previous = fingerprint
            for stamp, rate in normalized.items():
                if stamp in events and events[stamp] != rate:
                    raise ValueError("Conflicting funding rates across pages")
                events[stamp] = rate
            low, high = min(normalized), max(normalized)
            if exchange == "binanceusdm":
                cursor = high+1
                if cursor >= end_ms or len(rows) < 100:
                    break
            else:
                cursor = low-1
                if low <= start_ms or len(rows) < 100:
                    break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            break
    else:
        error = "History page budget exhausted"
    return dict(events=[dict(timestamp=stamp, rate=rate) for stamp, rate in sorted(events.items())],
                requests=requests, error=error)
