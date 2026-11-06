"""Incremental minute chart observations; no synthetic prices or account PnL."""
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3

MINUTE = 60000


def ensure(db):
    db.execute("CREATE TABLE IF NOT EXISTS chart_rollups(series TEXT NOT NULL,start_ms INTEGER NOT NULL,first_ms INTEGER NOT NULL,last_ms INTEGER NOT NULL,samples INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(series,start_ms))")
    db.execute("CREATE INDEX IF NOT EXISTS chart_rollups_time ON chart_rollups(start_ms)")
    db.execute("CREATE TABLE IF NOT EXISTS chart_checkpoints(stream TEXT PRIMARY KEY,last_id INTEGER NOT NULL)")


def pending(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {}
    for stream in ('scans', 'economics'):
        checkpoint = db.execute("SELECT last_id FROM chart_checkpoints WHERE stream=?", (stream,)).fetchone() if 'chart_checkpoints' in names else None
        result[stream] = db.execute(f"SELECT count(*) FROM {stream} WHERE id>?", (checkpoint[0] if checkpoint else 0,)).fetchone()[0] if stream in names else 0
    return result


def accumulate(db, series, stamp, values):
    start = stamp//MINUTE*MINUTE
    previous = db.execute("SELECT first_ms,last_ms,samples,payload FROM chart_rollups WHERE series=? AND start_ms=?", (series, start)).fetchone()
    first, last, samples, payload = previous if previous else (stamp, stamp, 0, '{}')
    saved = json.loads(payload)
    for key, value in values.items():
        if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError('Invalid chart number')
        field = saved.setdefault(key, dict(last=None, min=None, max=None))
        if stamp >= last:
            field['last'] = value
        if value is not None:
            field['min'] = value if field['min'] is None else min(field['min'], value)
            field['max'] = value if field['max'] is None else max(field['max'], value)
    db.execute("INSERT INTO chart_rollups VALUES (?,?,?,?,?,?) ON CONFLICT(series,start_ms) DO UPDATE SET first_ms=excluded.first_ms,last_ms=excluded.last_ms,samples=excluded.samples,payload=excluded.payload",
               (series, start, min(first, stamp), max(last, stamp), samples+1, json.dumps(saved, separators=(',', ':'), allow_nan=False)))


def rollup(db, max_snapshots=25):
    """Caller owns the transaction. A checkpoint and its buckets commit together."""
    ensure(db)
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    processed = {}
    for stream in ('scans', 'economics'):
        processed[stream] = 0
        if stream not in names:
            continue
        checkpoint = db.execute("SELECT last_id FROM chart_checkpoints WHERE stream=?", (stream,)).fetchone()
        last = checkpoint[0] if checkpoint else 0
        while max_snapshots is None or processed[stream] < max_snapshots:
            limit = 25 if max_snapshots is None else min(25, max_snapshots-processed[stream])
            batch = db.execute(f"SELECT id,generated_at_ms,payload FROM {stream} WHERE id>? ORDER BY id LIMIT ?", (last, limit)).fetchall()
            if not batch:
                break
            for identity, stamp, payload in batch:
                document = json.loads(payload)
                if stream == 'scans':
                    rows = document.get('rows', [])
                    indexed = {(r['asset'], r['long_exchange'], r['short_exchange'], r['long_symbol'], r['short_symbol']): r for r in rows}
                    for row in rows:
                        reverse = indexed.get((row['asset'], row['short_exchange'], row['long_exchange'], row['short_symbol'], row['long_symbol']))
                        accumulate(db, 'pair:'+row['id'], stamp, dict(entry=row['open_spread_pct'], exit=reverse['open_spread_pct'] if reverse else None,
                            long_price=row['long_ask_usdt_per_unit'], short_price=row['short_bid_usdt_per_unit']))
                else:
                    for rate in document.get('funding_observations', []):
                        received = rate.get('received_at_ms')
                        if received is not None and 0 <= stamp-received <= 120000 and rate.get('rate') is not None:
                            accumulate(db, 'fund:'+rate['exchange']+':'+rate['native_id'], stamp,
                                       dict(value=float(rate['rate'])*100, interval_ms=rate.get('interval_ms')))
                last = identity
                processed[stream] += 1
            db.execute("INSERT INTO chart_checkpoints VALUES (?,?) ON CONFLICT(stream) DO UPDATE SET last_id=excluded.last_id", (stream, last))
    return processed


def read(db, series, since, until):
    # Index covers the chosen series/time range; at most 1441 minute buckets per day.
    values = db.execute("SELECT start_ms,first_ms,last_ms,samples,payload FROM chart_rollups WHERE series=? AND start_ms>=? AND start_ms<=? AND last_ms>=? AND last_ms<=? ORDER BY start_ms LIMIT 1442",
                        (series, since//MINUTE*MINUTE, until, since, until)).fetchall()
    result = []
    for start, first, last, samples, payload in values:
        fields = json.loads(payload)
        result.append(dict(time=last, start_ms=start, first_ms=first, samples=samples,
                           **{key: field['last'] for key, field in fields.items()},
                           ranges={key: dict(min=field['min'], max=field['max']) for key, field in fields.items()}))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path('data/market.sqlite3'))
    args = parser.parse_args()
    from .recovery import SessionLease
    with SessionLease(args.database), closing(sqlite3.connect(args.database.resolve().as_uri()+'?mode=rw', uri=True)) as db:
        processed = dict(scans=0, economics=0)
        while True:
            with db:
                batch = rollup(db, 25)
            for key in processed:
                processed[key] += batch[key]
            if not any(batch.values()):
                break
        print(json.dumps(dict(processed=processed, pending=pending(db), buckets=db.execute('SELECT count(*) FROM chart_rollups').fetchone()[0])))


if __name__ == '__main__':
    main()
