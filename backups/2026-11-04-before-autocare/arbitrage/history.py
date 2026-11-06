"""Read recent signal lifecycle events from an existing database without modifying it."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def read_events(database, pair_id=None, limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError("Event limit must be 1–1000")
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+"?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_events'").fetchone():
            return []
        query = "SELECT payload FROM signal_events"
        values = []
        if pair_id is not None:
            query += " WHERE pair_id=?"
            values.append(pair_id)
        query += " ORDER BY timestamp_ms DESC, rowid DESC LIMIT ?"
        rows = db.execute(query, values+[limit]).fetchall()
    return [json.loads(row[0]) for row in reversed(rows)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/market.sqlite3"))
    parser.add_argument("--pair", default=None)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--summary", action="store_true", help="Read minute economic rollups instead of lifecycle events")
    parser.add_argument("--since-ms", type=int)
    parser.add_argument("--until-ms", type=int)
    args = parser.parse_args()
    try:
        if args.summary:
            result = dict(aggregation=rollup_status(args.database),
                          buckets=read_buckets(args.database, args.pair, args.since_ms, args.until_ms, args.limit))
        else:
            if args.since_ms is not None or args.until_ms is not None:
                parser.error("Time range options require --summary")
            result = read_events(args.database, args.pair, args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))


def read_buckets(database, pair_id=None, since_ms=None, until_ms=None, limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError("Bucket limit must be 1–1000")
    if since_ms is not None and until_ms is not None and since_ms >= until_ms:
        raise ValueError("Expected since-ms < until-ms")
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+"?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_rollups'").fetchone():
            return []
        conditions, values = ["bucket_ms=60000"], []
        for clause, value in (("pair_id=?", pair_id), ("start_ms>=?", since_ms), ("start_ms<?", until_ms)):
            if value is not None:
                conditions.append(clause)
                values.append(value)
        rows = db.execute("SELECT payload FROM history_rollups WHERE "+" AND ".join(conditions)+
                          " ORDER BY start_ms DESC,pair_id DESC,policy_key DESC LIMIT ?", values+[limit]).fetchall()
    return [json.loads(row[0]) for row in reversed(rows)]


def rollup_status(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+"?mode=ro", uri=True)) as db:
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checkpoint = db.execute("SELECT last_id FROM history_checkpoints WHERE bucket_ms=60000").fetchone() if "history_checkpoints" in existing else None
        last = checkpoint[0] if checkpoint else 0
        pending = db.execute("SELECT count(*) FROM economics WHERE id>?", (last,)).fetchone()[0] if "economics" in existing else 0
    return dict(last_processed_economics_id=last, pending_raw_snapshots=pending, caught_up=pending == 0)


if __name__ == "__main__":
    main()
