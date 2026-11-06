import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import time
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


async def run(args):
    bases = None if args.symbols.upper() == "ALL" else set(args.symbols.upper().split(","))
    while True:
        snapshots = await asyncio.gather(*(collect(e, bases) for e in args.exchanges))
        now = int(time.time() * 1000)
        for snapshot in snapshots:
            for quote in snapshot["quotes"]:
                quote["quality"] = Quote(**{k: v for k, v in quote.items() if k != "quality"}).quality(now)
            snapshot["status"]["quality_evaluated_at_ms"] = now
            snapshot["status"]["usable_quotes"] = sum(q["quality"] == "ok" for q in snapshot["quotes"])
        save(args.database, snapshots)
        document = {"mode": "monitor", "transport": "REST", "generated_at_ms": int(time.time()*1000),
                    "exchanges": snapshots}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(args.output)
        for item in snapshots:
            status = item["status"]
            print(f"{status['exchange']:12} {status['state']:10} markets={status['markets']} quotes={status['quotes']} usable={status['usable_quotes']}", flush=True)
            for error in status["errors"]:
                print(f"  {error}", flush=True)
        if not args.watch:
            return 0 if all(s["status"]["state"] == "ok" for s in snapshots) else 1
        await asyncio.sleep(args.interval)


def main():
    parser = argparse.ArgumentParser(description="Public USDT perpetual market-data collector")
    parser.add_argument("--exchanges", nargs="+", choices=EXCHANGES, default=list(EXCHANGES))
    parser.add_argument("--symbols", default="BTC,ETH", help="Base currencies separated by commas, or ALL")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--output", type=Path, default=Path("data/latest.json"))
    parser.add_argument("--database", type=Path, default=Path("data/market.sqlite3"))
    args = parser.parse_args()
    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds")
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
