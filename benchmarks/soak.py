"""Record health of a running local monitor without starting it or polling exchanges."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import quantiles
from time import monotonic, sleep, time
from urllib.error import URLError
from urllib.request import urlopen


def poll(url, database):
    row = {'at': datetime.now(timezone.utc).isoformat()}
    try:
        with urlopen(url, timeout=5) as response:
            row.update(json.load(response))
    except (OSError, URLError, ValueError) as exc:
        row['error'] = type(exc).__name__
    row['snapshotAgeMs'] = max(0, int(time() * 1000) - row['generatedAt']) if row.get('generatedAt') else None
    sources = list(row.get('sources', {}).values())
    row['freshSourceFraction'] = sum(not source['batchStale'] for source in sources) / len(sources) if sources else 0
    files = [database, database.with_name(database.name + '-wal')]
    row['databaseBytes'] = sum(path.stat().st_size for path in files if path.exists())
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8780)
    parser.add_argument('--hours', type=float, default=24)
    parser.add_argument('--interval', type=float, default=60)
    parser.add_argument('--history', type=Path, default=Path('data/monitor.sqlite3'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.hours <= 0 or args.interval <= 0 or not 1 <= args.port <= 65535:
        parser.error('Use a valid port and positive duration/interval')
    url = f'http://127.0.0.1:{args.port}/api/health?coverage=1'
    rows = []
    end = monotonic() + args.hours * 3600
    with args.output.open('x', encoding='utf-8') as output:
        while True:
            row = poll(url, args.history)
            rows.append(row)
            output.write(json.dumps(row, ensure_ascii=False) + '\n')
            output.flush()
            remaining = end - monotonic()
            if remaining <= 0:
                break
            sleep(min(args.interval, remaining))

    healthy = [row for row in rows if row['snapshotAgeMs'] is not None and row['snapshotAgeMs'] <= 60_000
               and row['freshSourceFraction'] >= .8]
    cycles = [row['cycleMs'] for row in rows if row.get('cycleMs') is not None]
    cycle_p95 = quantiles(cycles, n=20, method='inclusive')[-1] if len(cycles) > 1 else None
    passed = len(healthy) / len(rows) >= .99 and cycle_p95 is not None and cycle_p95 <= 30_000
    print(json.dumps(dict(passed=passed, samples=len(rows), healthy=len(healthy), cycleP95Ms=cycle_p95,
                          startBytes=rows[0]['databaseBytes'], peakBytes=max(row['databaseBytes'] for row in rows),
                          finalBytes=rows[-1]['databaseBytes'], reconnects=rows[-1].get('reconnects')), indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
