import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import time
import math
from contextlib import closing

from .collector import EXCHANGES, collect
from .models import Quote
from .scanner import Registry, scan


def save(path, snapshots, scan_result=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, collected_at_ms INTEGER NOT NULL, exchange TEXT NOT NULL, payload TEXT NOT NULL)")
        for snapshot in snapshots:
            db.execute("INSERT INTO snapshots(collected_at_ms, exchange, payload) VALUES (?, ?, ?)",
                       (snapshot["status"]["checked_at_ms"], snapshot["status"]["exchange"],
                        json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))
        if scan_result is not None:
            db.execute("CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, generated_at_ms INTEGER NOT NULL, payload TEXT NOT NULL)")
            db.execute("INSERT INTO scans(generated_at_ms, payload) VALUES (?, ?)",
                       (scan_result["generated_at_ms"], json.dumps(scan_result, ensure_ascii=False, allow_nan=False)))


def publish(args, snapshots, transport):
    timestamp = int(time.time()*1000)
    scan_result = scan(snapshots, args.registry, timestamp, args.capital_usdt, args.max_skew_ms, args.min_spread_pct)
    save(args.database, snapshots, scan_result)
    document = {"schema_version": 2, "mode": "monitor", "transport": transport, "generated_at_ms": timestamp,
                "exchanges": snapshots, "scanner": scan_result}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(args.output)
    print(" | ".join(f"{s['status']['exchange']} {s['status']['state']} "
                     f"{s['status']['usable_quotes']}/{s['status'].get('markets', s['status']['quotes']) if transport == 'WebSocket' else s['status']['quotes']} fresh"
                     for s in snapshots), flush=True)
    for snapshot in snapshots:
        status = snapshot["status"]
        if status.get("omitted_markets"):
            print(f"  {status['exchange']}: capacity limit omitted {status['omitted_markets']} markets; "
                  f"connections={status['connections']}", flush=True)
        for error in status.get("errors", []):
            print(f"  {status['exchange']}: {error}", flush=True)
        if status.get("last_error"):
            print(f"  {status['exchange']}: {status['last_error']}", flush=True)
    print(f"SPREADS preliminary: {scan_result['displayed_pairs']} directed pairs; "
          f"{len(scan_result['exclusions'])} excluded quotes; "
          f"{len(scan_result['skipped_pairs'])} skipped pairs; fees/funding/depth NOT included", flush=True)
    if args.top and scan_result["rows"]:
        print(f"  {'ASSET':10} {'LONG':12} {'SHORT':12} {'ASK':>14} {'BID':>14} {'OPEN %':>10}", flush=True)
        for row in scan_result["rows"][:args.top]:
            print(f"  {row['asset']:10} {row['long_exchange']:12} {row['short_exchange']:12} "
                  f"{row['long_ask_usdt_per_unit']:14.8g} {row['short_bid_usdt_per_unit']:14.8g} "
                  f"{row['open_spread_pct']:+10.5f}", flush=True)
    return scan_result


async def run(args):
    if args.transport == "ws":
        from .pools import run_pools
        return await run_pools(args, publish)
    if args.symbols == "REGISTERED":
        bases = {entry["base"] for entry in args.registry.bindings.values()}
    else:
        bases = None if args.symbols == "ALL" else set(args.symbols.split(","))
    while True:
        snapshots = await asyncio.gather(*(collect(e, bases) for e in args.exchanges))
        now = int(time.time() * 1000)
        for snapshot in snapshots:
            for quote in snapshot["quotes"]:
                quote["quality"] = Quote(**{k: v for k, v in quote.items() if k != "quality"}).quality(now)
            snapshot["status"]["quality_evaluated_at_ms"] = now
            snapshot["status"]["usable_quotes"] = sum(q["quality"] == "ok" for q in snapshot["quotes"])
        publish(args, snapshots, "REST")
        if not args.watch:
            return 0 if all(s["status"]["state"] == "ok" for s in snapshots) else 1
        await asyncio.sleep(args.interval)


def main():
    parser = argparse.ArgumentParser(description="Public USDT perpetual market-data collector")
    parser.add_argument("--transport", choices=("rest", "ws"), default="ws")
    parser.add_argument("--exchanges", nargs="+", choices=EXCHANGES, default=list(EXCHANGES))
    parser.add_argument("--symbols", default="REGISTERED", help="Base currencies, REGISTERED (default), or ALL within capacity limits")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=None, help="Snapshot period; default WS=1, REST=15 seconds")
    parser.add_argument("--duration", type=float, default=0, help="Stop WS after N seconds; 0 means until Ctrl+C")
    parser.add_argument("--output", type=Path, default=Path("data/latest.json"))
    parser.add_argument("--database", type=Path, default=Path("data/market.sqlite3"))
    parser.add_argument("--registry", type=Path, default=None, help="Explicit instrument registry; default bundled ten-asset registry")
    parser.add_argument("--capital-usdt", type=float, default=1000, help="Combined illustrative entry notional ceiling")
    parser.add_argument("--max-skew-ms", type=int, default=1000, help="Maximum source timestamp gap between legs")
    parser.add_argument("--min-spread-pct", type=float, default=None, help="Optional raw spread filter; default includes negative spreads")
    parser.add_argument("--top", type=int, default=5, help="Show top N pairs in console; JSON/SQLite keep all filtered pairs")
    parser.add_argument("--group-size", type=int, default=20, help="Markets per WS connection (1–20; MEXC at most 5)")
    parser.add_argument("--max-connections", type=int, default=8, help="Maximum connections per exchange (1–64)")
    parser.add_argument("--market-refresh-seconds", type=float, default=900, help="Instrument refresh period (at least 60 seconds)")
    parser.add_argument("--mexc-depth-limit", type=int, default=20, help="Maximum synchronized MEXC depth books (1–40)")
    parser.add_argument("--report", type=Path, default=Path("data/run-report.json"), help="WS diagnostics report")
    parser.add_argument("--report-warmup-seconds", type=float, default=60, help="Exclude startup from sampled freshness ratios")
    args = parser.parse_args()
    args.interval = args.interval if args.interval is not None else (1 if args.transport == "ws" else 15)
    minimum_interval = 0.25 if args.transport == "ws" else 5
    if not math.isfinite(args.interval) or args.interval < minimum_interval:
        parser.error(f"--interval must be at least {minimum_interval} seconds")
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error("--duration must be finite and nonnegative")
    if not math.isfinite(args.capital_usdt) or not 0 < args.capital_usdt <= 1000:
        parser.error("--capital-usdt must be positive and no more than 1000")
    if args.max_skew_ms < 0:
        parser.error("--max-skew-ms must be nonnegative")
    if args.min_spread_pct is not None and not math.isfinite(args.min_spread_pct):
        parser.error("--min-spread-pct must be finite")
    if not 0 <= args.top <= 100:
        parser.error("--top must be between 0 and 100")
    if not 1 <= args.group_size <= 20 or not 1 <= args.max_connections <= 64:
        parser.error("--group-size must be 1–20; --max-connections must be 1–64")
    if not math.isfinite(args.market_refresh_seconds) or args.market_refresh_seconds < 60:
        parser.error("--market-refresh-seconds must be at least 60")
    if not 1 <= args.mexc_depth_limit <= 40:
        parser.error("--mexc-depth-limit must be 1–40")
    if not math.isfinite(args.report_warmup_seconds) or args.report_warmup_seconds < 0:
        parser.error("--report-warmup-seconds must be nonnegative")
    if len({args.report.resolve(), args.output.resolve(), args.database.resolve()}) != 3:
        parser.error("--report, --output and --database must use different file paths")
    try:
        args.registry = Registry.load(args.registry)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(f"Invalid instrument registry: {exc}")
    bases = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not bases:
        parser.error("--symbols must not be empty")
    args.symbols = ",".join(bases)
    if any(special in bases for special in ("ALL", "REGISTERED")) and len(bases) != 1:
        parser.error("ALL or REGISTERED must be used alone")
    if args.transport == "ws" and args.watch:
        parser.error("WS already runs continuously; use --watch only with --transport rest")
    if args.transport == "rest" and args.duration:
        parser.error("--duration is only available with --transport ws")
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
