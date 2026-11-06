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


def save(path, snapshots):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, collected_at_ms INTEGER NOT NULL, exchange TEXT NOT NULL, payload TEXT NOT NULL)")
        for snapshot in snapshots:
            db.execute("INSERT INTO snapshots(collected_at_ms, exchange, payload) VALUES (?, ?, ?)",
                       (snapshot["status"]["checked_at_ms"], snapshot["status"]["exchange"],
                        json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))


def publish(args, snapshots, transport):
    save(args.database, snapshots)
    document = {"mode": "monitor", "transport": transport, "generated_at_ms": int(time.time()*1000),
                "exchanges": snapshots}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(args.output)
    print(" | ".join(f"{s['status']['exchange']} {s['status']['state']} "
                     f"{s['status']['usable_quotes']}/{s['status']['quotes']} fresh"
                     for s in snapshots), flush=True)
    for snapshot in snapshots:
        status = snapshot["status"]
        for error in status.get("errors", []):
            print(f"  {status['exchange']}: {error}", flush=True)
        if status.get("last_error"):
            print(f"  {status['exchange']}: {status['last_error']}", flush=True)


async def run(args):
    if args.transport == "ws":
        from .live import run_live
        return await run_live(args, publish)
    bases = None if args.symbols.upper() == "ALL" else set(args.symbols.upper().split(","))
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
    parser.add_argument("--symbols", default="BTC,ETH", help="Base currencies separated by commas, or ALL")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=None, help="Snapshot period; default WS=1, REST=15 seconds")
    parser.add_argument("--duration", type=float, default=0, help="Stop WS after N seconds; 0 means until Ctrl+C")
    parser.add_argument("--output", type=Path, default=Path("data/latest.json"))
    parser.add_argument("--database", type=Path, default=Path("data/market.sqlite3"))
    args = parser.parse_args()
    args.interval = args.interval if args.interval is not None else (1 if args.transport == "ws" else 15)
    minimum_interval = 0.25 if args.transport == "ws" else 5
    if not math.isfinite(args.interval) or args.interval < minimum_interval:
        parser.error(f"--interval must be at least {minimum_interval} seconds")
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error("--duration must be finite and nonnegative")
    bases = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not bases:
        parser.error("--symbols must not be empty")
    args.symbols = ",".join(bases)
    if args.transport == "ws" and ("ALL" in bases or len(bases) > 20):
        parser.error("WS currently requires 1–20 explicit base symbols; ALL is available via --transport rest")
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
