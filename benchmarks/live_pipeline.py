"""Bounded live probe using public sources and a temporary history database."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor import Monitor, now_ms  # noqa: E402


async def probe(venues, seconds, futures_ws):
    snapshots = []
    drift = []
    with TemporaryDirectory() as directory:
        monitor = Monitor(venues, Path(directory) / 'probe.sqlite3', futures_ws=futures_ws)
        collector = asyncio.create_task(monitor.run())
        started = monotonic()
        next_sample = started
        generation = None
        try:
            while monotonic() - started < seconds:
                if collector.done():
                    await collector
                    raise RuntimeError('Monitor stopped during the probe')
                now = monotonic()
                drift.append(max(0, (now - next_sample) * 1000))
                current = monitor.snapshot
                if current['generatedAt'] != generation:
                    generation = current['generatedAt']
                    stats = monitor.health(now_ms())
                    snapshots.append(dict(generatedAt=generation, cycleMs=stats['cycleMs'],
                                          snapshotMarketsMs=stats['snapshotMarketsMs'],
                                          legs=sum(map(len, current['markets'].values())),
                                          readySources=sum(v.get('state') == 'ok' for v in current['venues'].values()),
                                          totalSources=len(current['venues']),
                                          oiLegs=sum(leg['oi'] is not None for group in current['markets'].values() for leg in group),
                                          streamPairs=len(monitor.futures_pairs)))
                next_sample = now + .02
                await asyncio.sleep(.02)
        finally:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
            final = monitor.health(now_ms())
            await monitor.close()
        drift.sort()
        return dict(snapshots=snapshots, driftSamples=len(drift),
                    eventLoopDelayP95Ms=round(drift[int(len(drift) * .95)], 3),
                    eventLoopDelayP99Ms=round(drift[int(len(drift) * .99)], 3),
                    eventLoopDelayMaxMs=round(drift[-1], 3), health=final)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--venues', nargs='+', required=True)
    parser.add_argument('--seconds', type=int, default=90)
    parser.add_argument('--futures-ws', action='store_true')
    args = parser.parse_args()
    if args.seconds < 10:
        parser.error('Use at least ten seconds')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    started = datetime.now(timezone.utc).isoformat()
    result = asyncio.run(probe(args.venues, args.seconds, args.futures_ws))
    print(json.dumps(dict(started=started, revision=revision, seconds=args.seconds, venues=args.venues,
                          futuresWs=args.futures_ws, **result), indent=2))


if __name__ == '__main__':
    main()
