"""Replay an archived SQLite sample into an isolated database for a simulated day."""

import argparse
from collections import defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import random
import sqlite3
from statistics import median
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_monitor.history import History, MINUTE  # noqa: E402

try:
    from market_monitor.history_selection import history_legs
except ModuleNotFoundError as exc:
    if exc.name != 'market_monitor.history_selection':
        raise
    history_legs = None


def file_bytes(path):
    return sum(p.stat().st_size for p in (path, path.with_name(path.name + '-wal')) if p.exists())


def load_archive(path, group_count):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        market_groups = defaultdict(list)
        for key, in db.execute('SELECT DISTINCT market FROM sample ORDER BY market'):
            venue, kind, group, quote = key.split('.')
            market_groups[group].append((key, venue, kind, quote))
        chosen = random.Random(417).sample(sorted(market_groups), group_count)
        slots = defaultdict(list)
        for group in sorted(chosen):
            for key, venue, kind, quote in market_groups[group]:
                for row in db.execute(
                    'SELECT slot, source, received, bid, ask, price, mark_index, oi FROM sample WHERE market=?', (key,)
                ):
                    slots[row[0]].append((group, venue, kind, quote, key, *row[1:]))
        total = db.execute('SELECT count(*), count(oi) FROM sample').fetchone()
    return [slots[slot] for slot in sorted(slots)], sorted(slots), total


def replay(archive, groups, hours):
    minutes, timestamps, totals = load_archive(archive, groups)
    start = timestamps[0]
    elapsed = []
    observed = 0
    peak = 0
    with TemporaryDirectory() as directory:
        path = Path(directory) / 'replay.sqlite3'
        history = History(path)
        try:
            for i in range(hours * 60):
                j = i % len(minutes)
                slot = start + i * MINUTE
                now = slot + MINUTE - 1
                delta = slot - timestamps[j]
                markets = defaultdict(list)
                for group, venue, kind, quote, key, source, received, bid, ask, price, index, oi in minutes[j]:
                    markets[group].append(dict(id=key, exchange=venue, type=kind, quote=quote, asset=None,
                                               sourceAt=source + delta if source is not None else None,
                                               receivedAt=received + delta if received is not None else None,
                                               bid=bid, ask=ask, last=price, index=index, oi=oi))
                observed += len(minutes[j])
                begin = perf_counter()
                if history_legs is None:
                    legs = [leg for group in markets.values() for leg in group]
                else:
                    legs = history_legs(markets, now, set(), set())
                history.record(legs, now)
                elapsed.append((perf_counter() - begin) * 1000)
                peak = max(peak, file_bytes(path))
            db = history.connection
            count, oi = db.execute('SELECT count(*), count(oi) FROM history_sample').fetchone()
            if count == 0:
                raise RuntimeError('The replay did not retain any historical samples')
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            return dict(archiveRows=totals[0], archiveOIRows=totals[1], archivedMinutes=len(minutes),
                        replayHours=hours, sampledGroups=groups, inputObservations=observed, rows=count,
                        oiRows=oi, bytesAfterCheckpoint=path.stat().st_size, peakDatabaseAndWalBytes=peak,
                        medianRecordMs=round(median(elapsed), 3),
                        p95RecordMs=round(sorted(elapsed)[len(elapsed)*95//100 - 1], 3))
        finally:
            history.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--groups', type=int, default=120)
    parser.add_argument('--hours', type=int, default=24)
    args = parser.parse_args()
    with args.archive.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    result = replay(args.archive, args.groups, args.hours)
    result.update(archiveSha256=digest, revision=subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], text=True).strip(), sqlite=sqlite3.sqlite_version)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
