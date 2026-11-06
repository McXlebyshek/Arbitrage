"""Bounded public funding-history import and explicit 7/30-day coverage reports."""
import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from .collector import EXCHANGES, public_client_config
from .models import is_target
from .scanner import Registry, decimal

DAY = 86400000


def window_stats(events, end_ms, days):
    start = end_ms-days*DAY
    ordered = sorted(events, key=lambda x: x["timestamp"])
    rows = [row for row in ordered if start < row["timestamp"] <= end_ms]
    anchors = [row for row in ordered if row["timestamp"] <= start]
    context = ([anchors[-1]] if anchors else [])+rows
    gaps = [b["timestamp"]-a["timestamp"] for a, b in zip(context, context[1:])]
    nominal = round(gaps[0]/60000)*60000 if gaps else None
    deviation = max(abs(gap-nominal) for gap in gaps) if gaps else None
    regular = bool(gaps) and 60000 <= nominal <= DAY and deviation <= 1000
    covered = bool(anchors and rows and regular and end_ms-rows[-1]["timestamp"] <= nominal+1000)
    rates = [decimal(row["rate"]) for row in rows]
    return dict(days=days, start_exclusive_ms=start, end_inclusive_ms=end_ms, events=len(rows),
        coverage="regular_observed_coverage" if covered else "partial_or_irregular",
        has_left_boundary_anchor=bool(anchors), regular_interval_ms=nominal if regular else None,
        cadence_tolerance_ms=1000, max_cadence_deviation_ms=deviation,
        max_observed_gap_ms=max(gaps) if gaps else None,
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
        elif exchange in ("bybit", "gate"):
            params = {"until": cursor}
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


def persist(database, exchange, binding, result, end_ms):
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS funding_history_events (exchange TEXT,native_id TEXT,timestamp_ms INTEGER,asset TEXT,rate TEXT,observed_at_ms INTEGER,PRIMARY KEY(exchange,native_id,timestamp_ms))")
        db.execute("CREATE TABLE IF NOT EXISTS funding_history_imports (exchange TEXT,native_id TEXT,end_ms INTEGER,payload TEXT,PRIMARY KEY(exchange,native_id))")
        for event in result["events"]:
            db.execute("INSERT INTO funding_history_events VALUES (?,?,?,?,?,?) ON CONFLICT(exchange,native_id,timestamp_ms) DO UPDATE SET rate=excluded.rate,observed_at_ms=excluded.observed_at_ms",
                (exchange, binding["id"], event["timestamp"], binding["asset"], event["rate"], int(time.time()*1000)))
        report = dict(exchange=exchange, native_id=binding["id"], asset=binding["asset"], end_ms=end_ms,
            requests=result["requests"], error=result["error"], imported_events=len(result["events"]),
            windows=[window_stats(result["events"], end_ms, days) for days in (7, 30)])
        if result["error"]:
            for window in report["windows"]:
                window["coverage"] = "partial_or_irregular"
        db.execute("INSERT INTO funding_history_imports VALUES (?,?,?,?) ON CONFLICT(exchange,native_id) DO UPDATE SET end_ms=excluded.end_ms,payload=excluded.payload",
                   (exchange, binding["id"], end_ms, json.dumps(report, ensure_ascii=False)))
    return report


def read_reports(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+"?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='funding_history_imports'").fetchone():
            return []
        reports = [json.loads(row[0]) for row in db.execute("SELECT payload FROM funding_history_imports ORDER BY exchange,native_id")]
        for report in reports:
            events = [dict(timestamp=stamp, rate=rate) for stamp, rate in db.execute(
                "SELECT timestamp_ms,rate FROM funding_history_events WHERE exchange=? AND native_id=? AND timestamp_ms<=? ORDER BY timestamp_ms",
                (report["exchange"], report["native_id"], report["end_ms"]))]
            report["stored_events"] = len(events)
            report["windows"] = [window_stats(events, report["end_ms"], days) for days in (7, 30)]
            if report["error"]:
                for window in report["windows"]:
                    window["coverage"] = "partial_or_irregular"
        return reports


async def collect(database, exchanges, assets=None):
    import ccxt.async_support as ccxt
    registry, end_ms = Registry.load(), int(time.time()*1000)
    async def venue(exchange):
        client = getattr(ccxt, exchange)(public_client_config(exchange))
        reports = []
        bindings = [binding for (venue_id, _), binding in registry.bindings.items()
                    if venue_id == exchange and (not assets or binding["base"] in assets)]
        try:
            async with asyncio.timeout(60):
                await client.load_markets()
            for binding in bindings:
                market = next((market for market in client.markets.values() if market["id"] == binding["id"] and is_target(market)), None)
                if market is None or market["base"] != binding["base"] or decimal(market["contractSize"]) != binding["contract_size"]:
                    result = dict(events=[], requests=0, error="Registry market mismatch")
                else:
                    result = await fetch_history(client, exchange, market["symbol"], end_ms-31*DAY, end_ms)
                report = persist(database, exchange, binding, result, end_ms)
                reports.append(report)
                print(exchange, binding["id"], report["imported_events"],
                      [window["coverage"] for window in report["windows"]], report["error"] or "", flush=True)
        except Exception as exc:
            already = {report["native_id"] for report in reports}
            for binding in bindings:
                if binding["id"] not in already:
                    reports.append(persist(database, exchange, binding,
                        dict(events=[], requests=0, error=f"{type(exc).__name__}: {exc}"), end_ms))
        finally:
            await client.close()
        return reports
    return [report for group in await asyncio.gather(*(venue(exchange) for exchange in exchanges)) for report in group]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/market.sqlite3"))
    parser.add_argument("--exchanges", nargs="+", choices=EXCHANGES, default=list(EXCHANGES))
    parser.add_argument("--assets", nargs="+", help="Registered base symbols, e.g. BTC ETH")
    parser.add_argument("--report", type=Path, default=Path("data/funding-history-report.json"))
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()
    if args.report.resolve() == args.database.resolve():
        parser.error("Database and report paths must differ")
    if args.assets:
        args.assets = {value.upper() for value in args.assets}
        known = {binding["base"] for binding in Registry.load().bindings.values()}
        if not args.assets <= known:
            parser.error("Unknown asset outside explicit registry")
    if args.read_only:
        print(json.dumps(read_reports(args.database), ensure_ascii=False, indent=2))
    else:
        reports = asyncio.run(collect(args.database, args.exchanges, args.assets))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
