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
    args = parser.parse_args()
    try:
        print(json.dumps(read_events(args.database, args.pair, args.limit), ensure_ascii=False, indent=2))
    except (ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
