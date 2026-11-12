"""Run an isolated public-data monitor and record health for a full-day soak."""

import argparse
import asyncio
import json
from itertools import combinations
from pathlib import Path
from statistics import quantiles
from time import monotonic

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from monitor import Monitor, VENUES, compatible_assets, create_app, fresh, now_ms


def coverage(snapshot, now):
    eligible = timed = 0
    for legs in snapshot['markets'].values():
        for a, b in combinations(legs, 2):
            if (a['quote'] != b['quote'] or a['type'] == b['type'] == 'spot' or
                    (a['exchange'], a['type']) == (b['exchange'], b['type']) or not compatible_assets(a, b)):
                continue
            eligible += 1
            if (all(leg['bid'] and leg['ask'] and fresh(leg['sourceAt'], now) and fresh(leg['receivedAt'], now)
                    for leg in (a, b)) and abs(a['sourceAt'] - b['sourceAt']) <= 1500 and
                    max(a['bid'], b['bid']) / min(a['bid'], b['bid']) <= 2):
                timed += 1
    return dict(eligiblePairs=eligible, timedPairs=timed, timedShare=timed / eligible if eligible else None)


def size_on_disk(path):
    return sum(file.stat().st_size for file in (path, path.with_name(path.name + '-wal')) if file.exists())


async def run(args, output):
    monitor = Monitor(dict.fromkeys(args.venues), args.history, futures_ws=True)
    runner = web.AppRunner(create_app(monitor))
    await runner.setup()
    started = monotonic()
    failures, samples, durations = 0, 0, []
    expected_streams = set(monitor.futures_feeds)
    seen_streams = set()
    last_publication = None
    criteria = dict(seconds=args.seconds, interval=args.interval, warmup=args.warmup,
                    maxSnapshotAgeMs=90000, p95CycleMs=15000, maxDatabaseBytes=1_500_000_000)

    def emit(payload):
        output.write(json.dumps(payload, ensure_ascii=False) + '\n')
        output.flush()

    emit(dict(kind='criteria', at=now_ms(), **criteria))
    try:
        site = web.TCPSite(runner, '127.0.0.1', args.port)
        await site.start()
        async with ClientSession(timeout=ClientTimeout(total=8)) as client:
            deadline = started + args.seconds
            while monotonic() < deadline:
                at = now_ms()
                warmed_up = monotonic() - started >= args.warmup
                try:
                    async with client.get(f'http://127.0.0.1:{args.port}/api/health') as response:
                        response.raise_for_status()
                        health = await response.json()
                    counts = await asyncio.to_thread(coverage, monitor.snapshot, at)
                    disk = size_on_disk(args.history)
                    emit(dict(kind='sample', at=at, health=health, databaseBytes=disk, **counts))
                    seen_streams.update(venue for venue, state in health['streams'].items() if state['connected'])
                    if warmed_up:
                        samples += 1
                        age = health['snapshotAgeMs']
                        if (age is None or age > criteria['maxSnapshotAgeMs'] or counts['timedPairs'] == 0
                                or disk > criteria['maxDatabaseBytes']):
                            failures += 1
                        if health['generatedAt'] != last_publication and health['cycleDurationMs'] is not None:
                            durations.append(health['cycleDurationMs'])
                            last_publication = health['generatedAt']
                except (ClientError, TimeoutError, ValueError) as error:
                    emit(dict(kind='sample-error', at=at, error=type(error).__name__))
                    if warmed_up:
                        samples += 1
                        failures += 1
                await asyncio.sleep(max(0, min(args.interval, deadline - monotonic())))
    finally:
        await runner.cleanup()
    p95 = quantiles(durations, n=20, method='inclusive')[-1] if len(durations) >= 2 else None
    missing_streams = sorted(expected_streams - seen_streams)
    passed = (samples > 0 and failures == 0 and p95 is not None and p95 <= criteria['p95CycleMs']
              and not missing_streams)
    summary = dict(kind='summary', at=now_ms(), passed=passed, sampled=samples, failed=failures,
                   p95CycleMs=p95, missingStreams=missing_streams, databaseBytes=size_on_disk(args.history))
    emit(summary)
    print(json.dumps(summary), flush=True)
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=24 * 60 * 60)
    parser.add_argument('--interval', type=int, default=15)
    parser.add_argument('--warmup', type=int, default=120)
    parser.add_argument('--port', type=int, default=18785)
    parser.add_argument('--venues', choices=VENUES, nargs='+', default=tuple(VENUES))
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.interval <= 0 or not 0 <= args.warmup < args.seconds:
        parser.error('Требуются положительный интервал и прогрев короче прогона')
    if args.history.exists() or args.history.resolve() == args.output.resolve():
        parser.error('Укажите новый SQLite-файл, отдельно от журнала')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as output:
        raise SystemExit(asyncio.run(run(args, output)))


if __name__ == '__main__':
    main()
