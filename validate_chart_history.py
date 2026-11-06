"""Offline replay of saved public scans into a NEW database; no network calls."""
from contextlib import closing
import argparse
import json
from pathlib import Path
import sqlite3
import statistics
import time

from arbitrage.__main__ import save
from arbitrage.ui import Dashboard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve() == args.source.resolve():
        parser.error('Output must be a new database path')
    with closing(sqlite3.connect(args.source.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        scans = [json.loads(p) for (p,) in db.execute('SELECT payload FROM scans ORDER BY id')]
        economics = {t: json.loads(p) for t, p in db.execute('SELECT generated_at_ms,payload FROM economics')}
    elapsed = []
    for scan in scans:
        start = time.perf_counter()
        save(args.output, [], scan, economics.get(scan['generated_at_ms']))
        elapsed.append((time.perf_counter()-start)*1000)
    with closing(sqlite3.connect(args.output)) as db:
        buckets = db.execute('SELECT count(*) FROM chart_rollups').fetchone()[0]
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    store = Dashboard(args.snapshot, args.output)
    identity = store.state()['rows'][0]['id']
    queries = []
    for _ in range(20):
        start = time.perf_counter()
        store.pair(identity, 1440)
        queries.append((time.perf_counter()-start)*1000)
    result = dict(source=str(args.source), snapshots=len(scans), max_directions=max(len(s['rows']) for s in scans), chart_buckets=buckets,
        save_p95_ms=sorted(elapsed)[int((len(elapsed)-1)*.95)], save_max_ms=max(elapsed), query_day_median_ms=statistics.median(queries),
        query_day_max_ms=max(queries), integrity=integrity,
        note='Offline replay: timings include raw/economic saves and rollups; not network or a 24h live test')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
