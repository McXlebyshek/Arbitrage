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
from .recovery import SessionInUse


def save(path, snapshots, scan_result=None, economics=None, signals=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, collected_at_ms INTEGER NOT NULL, exchange TEXT NOT NULL, payload TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS snapshots_time ON snapshots(collected_at_ms)")
        db.execute("CREATE INDEX IF NOT EXISTS snapshots_exchange_id ON snapshots(exchange,id)")
        for snapshot in snapshots:
            db.execute("INSERT INTO snapshots(collected_at_ms, exchange, payload) VALUES (?, ?, ?)",
                       (snapshot["status"]["checked_at_ms"], snapshot["status"]["exchange"],
                        json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))
        if scan_result is not None:
            db.execute("CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, generated_at_ms INTEGER NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS scans_time ON scans(generated_at_ms)")
            db.execute("INSERT INTO scans(generated_at_ms, payload) VALUES (?, ?)",
                       (scan_result["generated_at_ms"], json.dumps(scan_result, ensure_ascii=False, allow_nan=False)))
        if economics is not None:
            db.execute("CREATE TABLE IF NOT EXISTS economics (id INTEGER PRIMARY KEY, generated_at_ms INTEGER NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS economics_time ON economics(generated_at_ms)")
            db.execute("INSERT INTO economics(generated_at_ms, payload) VALUES (?, ?)",
                       (economics.get("generated_at_ms", int(time.time()*1000)), json.dumps(economics, ensure_ascii=False, allow_nan=False)))
        if signals is not None:
            db.execute("CREATE TABLE IF NOT EXISTS signal_snapshots (id INTEGER PRIMARY KEY, generated_at_ms INTEGER NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS signal_snapshots_time ON signal_snapshots(generated_at_ms)")
            db.execute("CREATE TABLE IF NOT EXISTS signal_events (event_id TEXT PRIMARY KEY, timestamp_ms INTEGER NOT NULL, run_id TEXT NOT NULL, pair_id TEXT NOT NULL, episode_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS signal_events_pair_time ON signal_events(pair_id, timestamp_ms)")
            db.execute("CREATE INDEX IF NOT EXISTS signal_events_retention ON signal_events(episode_id,timestamp_ms,kind)")
            from .recovery import recover_sessions
            recover_sessions(db, signals)
            db.execute("INSERT INTO signal_snapshots(generated_at_ms, run_id, payload) VALUES (?, ?, ?)",
                       (signals["generated_at_ms"], signals["run_id"], json.dumps(signals, ensure_ascii=False, allow_nan=False)))
            for event in signals["events"]:
                db.execute("INSERT OR IGNORE INTO signal_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event["event_id"], event["timestamp_ms"], event["run_id"], event["pair_id"], event["episode_id"], event["kind"],
                     json.dumps(event, ensure_ascii=False, allow_nan=False)))
        if economics is not None:
            from .storage import rollup
            rollup(db, max_snapshots=25)


def publish(args, snapshots, transport):
    timestamp = int(time.time()*1000)
    unfiltered = scan(snapshots, args.registry, timestamp, args.capital_usdt, args.max_skew_ms)
    scan_result = unfiltered
    if args.min_spread_pct is not None:
        rows = [row for row in unfiltered["rows"] if row["open_spread_pct"] >= args.min_spread_pct]
        scan_result = dict(unfiltered, rows=rows, min_spread_pct=args.min_spread_pct, displayed_pairs=len(rows),
                           positive_displayed_pairs=sum(row["open_spread_pct"] > 0 for row in rows))
    engine = getattr(args, "economic_engine", None)
    if engine is not None:
        engine.submit(unfiltered)
    economics = engine.snapshot(unfiltered, timestamp) if engine is not None else None
    signals = None
    if economics is not None:
        if not hasattr(args, "signal_tracker"):
            from .signals import SignalTracker
            args.signal_tracker = SignalTracker(getattr(args, "confirmations", 3), getattr(args, "confirm_min_seconds", 2),
                getattr(args, "confirm_max_gap_seconds", 10), getattr(args, "signal_min_usdt", 0), args.max_skew_ms)
        signals = args.signal_tracker.update(economics, unfiltered, timestamp)
    save(args.database, snapshots, scan_result, economics, signals)
    document = {"schema_version": 3, "mode": "monitor", "transport": transport, "generated_at_ms": timestamp,
                "exchanges": snapshots, "scanner": scan_result, "economics": economics, "signals": signals,
                "maintenance": args.autocare.snapshot() if getattr(args, "autocare", None) else None}
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
    if economics is not None:
        print(f"ECONOMICS {economics['state']}: " + ", ".join(f"{row['long_exchange']}->{row['short_exchange']} {row['status']}"
              + (f" ({'; '.join(row['reasons'])})" if row["reasons"] else "") for row in economics["rows"]), flush=True)
        if economics.get("last_error"):
            print(f"  economics: {economics['last_error']}", flush=True)
        for row in economics["rows"]:
            if "base_quantity" in row:
                scenario = row.get("convergence_net_scenario_usdt")
                forecast = f"{scenario:+.4f}" if scenario is not None else "incomplete"
                print(f"  {row['asset']} {row['long_exchange']}->{row['short_exchange']}: "
                      f"Q={row['base_quantity']:.8g}; fees4~{row['four_fee_convergence_estimate_usdt']:.4f}; "
                      f"close_now~{row['close_now_net_before_funding_usdt']:+.4f}; "
                      f"conditional_convergence~{forecast} USDT (assumptions, not actual PnL)", flush=True)
    if signals is not None:
        if signals.get("recovered_events"):
            print(f"SIGNAL recovery: closed {len(signals['recovered_events'])} interrupted episodes; confirmations restart from zero", flush=True)
        for event in signals["events"]:
            print(f"SIGNAL conditional {event['kind']}: {event['pair_id']} -> {event['to_state']} "
                  f"({', '.join(event['reasons']) or 'scenario conditions'})", flush=True)
    return scan_result


async def run(args):
    from .recovery import SessionLease
    with SessionLease(args.database):
        care_task = None
        if getattr(args, "auto_maintenance", False):
            from .autocare import AutoCare, CarePolicy
            args.autocare = AutoCare(args.database,
                CarePolicy(args.raw_history_days, args.summary_history_days, args.funding_history_days,
                           args.backup_count, int(args.backup_max_mib*1024*1024), args.maintenance_batch_rows),
                args.maintenance_seconds, args.funding_refresh_hours, args.exchanges, args.registry,
                None if args.symbols in ("ALL", "REGISTERED") else set(args.symbols.split(",")))
            care_task = asyncio.create_task(args.autocare.run(), name="automatic-history-care")
        try:
            return await run_session(args)
        finally:
            if care_task:
                care_task.cancel()
                await asyncio.gather(care_task, return_exceptions=True)


async def run_session(args):
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
    parser.add_argument("--auto-maintenance", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--maintenance-seconds", type=float, default=3600, help="Background retention period, at least 30 seconds")
    parser.add_argument("--raw-history-days", type=float, default=7)
    parser.add_argument("--summary-history-days", type=float, default=30)
    parser.add_argument("--funding-history-days", type=float, default=90)
    parser.add_argument("--backup-count", type=int, default=3)
    parser.add_argument("--backup-max-mib", type=float, default=512)
    parser.add_argument("--maintenance-batch-rows", type=int, default=200)
    parser.add_argument("--funding-refresh-hours", type=float, default=24, help="Long history refresh period; 0 disables, otherwise at least 1 hour")
    parser.add_argument("--interval", type=float, default=None, help="Snapshot period; default WS=1, REST=15 seconds")
    parser.add_argument("--duration", type=float, default=0, help="Stop WS after N seconds; 0 means until Ctrl+C")
    parser.add_argument("--output", type=Path, default=Path("data/latest.json"))
    parser.add_argument("--database", type=Path, default=Path("data/market.sqlite3"))
    parser.add_argument("--registry", type=Path, default=None, help="Explicit instrument registry; default bundled ten-asset registry")
    parser.add_argument("--capital-usdt", type=float, default=1000, help="Combined illustrative entry notional ceiling")
    parser.add_argument("--max-skew-ms", type=int, default=1000, help="Maximum source timestamp gap between legs")
    parser.add_argument("--min-spread-pct", type=float, default=None, help="Raw table display filter; economic funding selection stays unfiltered")
    parser.add_argument("--top", type=int, default=5, help="Show top N pairs in console; JSON/SQLite keep all filtered pairs")
    parser.add_argument("--group-size", type=int, default=20, help="Markets per WS connection (1–20; MEXC at most 5)")
    parser.add_argument("--max-connections", type=int, default=8, help="Maximum connections per exchange (1–64)")
    parser.add_argument("--market-refresh-seconds", type=float, default=900, help="Instrument refresh period (at least 60 seconds)")
    parser.add_argument("--mexc-depth-limit", type=int, default=20, help="Maximum synchronized MEXC depth books (1–40)")
    parser.add_argument("--report", type=Path, default=Path("data/run-report.json"), help="WS diagnostics report")
    parser.add_argument("--report-warmup-seconds", type=float, default=60, help="Exclude startup from sampled freshness ratios")
    parser.add_argument("--economics-pairs", type=int, default=3, help="Enrich top N pairs via public REST (0 disables, maximum 10)")
    parser.add_argument("--confirmations", type=int, default=3, help="Independent depth samples required for a conditional signal (2–20)")
    parser.add_argument("--confirm-min-seconds", type=float, default=2, help="Minimum qualifying observation span")
    parser.add_argument("--confirm-max-gap-seconds", type=float, default=10, help="Maximum gap between counted depth samples")
    parser.add_argument("--signal-min-usdt", type=float, default=0, help="Conditional net must be strictly above this nonnegative threshold")
    parser.add_argument("--candidate-hold-seconds", type=float, default=10, help="Minimum candidate residence while WS data remain eligible (0–60)")
    parser.add_argument("--fees-profile", type=Path, default=None, help="Optional JSON exchange -> fractional taker fee assumptions")
    parser.add_argument("--hold-hours", type=float, default=8, help="Conditional funding scenario horizon (0–72 hours)")
    parser.add_argument("--max-impact-pct", type=float, default=0.1, help="Maximum VWAP impact per fill, in percent")
    parser.add_argument("--min-budget-utilization", type=float, default=0.9, help="Minimum selected quantity as a share of budget (0–1)")
    parser.add_argument("--extra-cost-usdt", type=float, default=0, help="Additional cost assumption/reserve per pair")
    parser.add_argument("--min-volume-usdt", type=float, default=1000000, help="Minimum public 24h quote volume on both venues; 0 disables")
    args = parser.parse_args()
    if not math.isfinite(args.maintenance_seconds) or args.maintenance_seconds < 30:
        parser.error("--maintenance-seconds must be finite and at least 30")
    if not math.isfinite(args.backup_max_mib) or args.backup_max_mib < 1:
        parser.error("--backup-max-mib must be finite and at least 1")
    if not math.isfinite(args.funding_refresh_hours) or (args.funding_refresh_hours != 0 and args.funding_refresh_hours < 1):
        parser.error("--funding-refresh-hours must be 0 or at least 1")
    try:
        from .autocare import CarePolicy
        CarePolicy(args.raw_history_days, args.summary_history_days, args.funding_history_days,
                   args.backup_count, int(args.backup_max_mib*1024*1024), args.maintenance_batch_rows)
    except ValueError as exc:
        parser.error(str(exc))
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
    if args.auto_maintenance and Path(str(args.database)+".maintenance.json").resolve() in {args.report.resolve(), args.output.resolve()}:
        parser.error("Output/report cannot use the automatic maintenance companion path")
    if not 0 <= args.economics_pairs <= 10:
        parser.error("--economics-pairs must be 0–10")
    if not 2 <= args.confirmations <= 20:
        parser.error("--confirmations must be 2–20")
    if (not math.isfinite(args.confirm_min_seconds) or not math.isfinite(args.confirm_max_gap_seconds)
            or not 0 <= args.confirm_min_seconds <= args.confirm_max_gap_seconds or args.confirm_max_gap_seconds <= 0):
        parser.error("Confirmation times must be finite, 0 <= min <= max gap, max gap > 0")
    if not math.isfinite(args.signal_min_usdt) or args.signal_min_usdt < 0:
        parser.error("--signal-min-usdt must be finite and nonnegative")
    if not math.isfinite(args.candidate_hold_seconds) or not 0 <= args.candidate_hold_seconds <= 60:
        parser.error("--candidate-hold-seconds must be 0–60")
    if not math.isfinite(args.hold_hours) or not 0 <= args.hold_hours <= 72:
        parser.error("--hold-hours must be 0–72")
    if not math.isfinite(args.max_impact_pct) or not 0 <= args.max_impact_pct <= 5:
        parser.error("--max-impact-pct must be 0–5")
    if not math.isfinite(args.min_budget_utilization) or not 0 <= args.min_budget_utilization <= 1:
        parser.error("--min-budget-utilization must be 0–1")
    if not math.isfinite(args.extra_cost_usdt) or args.extra_cost_usdt < 0:
        parser.error("--extra-cost-usdt must be nonnegative")
    if not math.isfinite(args.min_volume_usdt) or args.min_volume_usdt < 0:
        parser.error("--min-volume-usdt must be nonnegative")
    try:
        from .economic_data import load_fee_overrides
        args.fee_overrides = load_fee_overrides(args.fees_profile)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f"Invalid fee profile: {exc}")
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
    except SessionInUse as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
