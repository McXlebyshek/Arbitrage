"""Bounded public-monitor/UI integration run in a NEW directory, never the main DB."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import time

import aiohttp


def metrics(values):
    ordered = sorted(values)
    return dict(count=len(values), median_ms=statistics.median(values) if values else None,
                p95_ms=ordered[int((len(ordered)-1)*.95)] if ordered else None, max_ms=max(values) if values else None)


async def run(args):
    args.directory.mkdir(parents=True, exist_ok=False)
    database, snapshot = args.directory/'market.sqlite3', args.directory/'latest.json'
    if args.seed_db:
        with closing(sqlite3.connect(args.seed_db.resolve().as_uri()+'?mode=ro', uri=True)) as source, closing(sqlite3.connect(database)) as destination:
            source.backup(destination)
    initial_bytes = database.stat().st_size if database.exists() else 0
    flags = dict(creationflags=subprocess.CREATE_NO_WINDOW) if os.name == 'nt' else {}
    producer = server = None
    began, modes, errors, timings = time.monotonic(), Counter(), [], dict(state=[], pair=[])
    max_rows = max_fresh = expected_pair_misses = checks = 0
    started_ms = int(time.time()*1000)
    report = dict(state='running', started_at_ms=started_ms, duration_requested_seconds=args.duration,
                  source='public_websocket_monitor_with_optional_copied_funding_archive', directory=str(args.directory), port=args.port)
    def save_report():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    save_report()
    try:
        with (args.directory/'monitor.log').open('wb') as monitor_log, (args.directory/'ui.log').open('wb') as ui_log:
            server = await asyncio.create_subprocess_exec(sys.executable, '-u', '-m', 'arbitrage.ui', '--snapshot', str(snapshot), '--database', str(database), '--port', str(args.port), stdout=ui_log, stderr=ui_log, **flags)
            producer = await asyncio.create_subprocess_exec(sys.executable, '-u', '-m', 'arbitrage', '--duration', str(args.duration), '--interval', '2', '--top', '0', '--maintenance-seconds', '60', '--output', str(snapshot), '--database', str(database), '--report', str(args.directory/'monitor-report.json'), stdout=monitor_log, stderr=monitor_log, **flags)
            print(f'RUNNING: http://127.0.0.1:{args.port}/ for {args.duration}s', flush=True)
            base, sample_index, stop_seen, next_progress = f'http://127.0.0.1:{args.port}', 0, None, began+30
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as client:
                while time.monotonic()-began < args.duration+90:
                    if server.returncode is not None:
                        raise RuntimeError(f'UI exited unexpectedly: {server.returncode}')
                    try:
                        start = time.perf_counter()
                        async with client.get(base+'/api/state') as response:
                            state = await response.json()
                            if response.status != 200:
                                raise RuntimeError(f'state HTTP {response.status}')
                        timings['state'].append((time.perf_counter()-start)*1000)
                        if state.get('error'):
                            errors.append(dict(kind='state_error',message=state['error'],elapsed_seconds=time.monotonic()-began))
                        modes[state['mode']] += 1
                        max_rows = max(max_rows, len(state['rows']))
                        max_fresh = max(max_fresh, sum(row['data_fresh'] for row in state['rows']))
                        for row in state['rows']:
                            if row['confirmed_current']:
                                assert state['mode'] == 'live' and row['data_fresh'] and row['economic_fresh']
                        if state['rows']:
                            row = state['rows'][sample_index % len(state['rows'])]
                            params = dict(id=row['id'], minutes=1440 if sample_index % 2 else 15, days=(1,7,30)[sample_index % 3])
                            if sample_index % 5 == 0:
                                params['end_ms'] = max(0, state['generated_at_ms']-30000)
                            start = time.perf_counter()
                            async with client.get(base+'/api/pair', params=params) as response:
                                if response.status == 404:
                                    expected_pair_misses += 1  # Pair can leave the eligible scan between requests.
                                else:
                                    if response.status != 200:
                                        raise RuntimeError(f'pair HTTP {response.status}: {(await response.text())[:200]}')
                                    detail = await response.json()
                                    assert detail['item']['id'] == row['id']
                                    assert detail['funding_comparison']['end_inclusive_ms'] == detail['chart_range']['latest_ms']
                                    assert all(p['time'] <= detail['chart_range']['end_ms'] for p in detail['points'])
                                    if detail['mode'] != 'live':
                                        assert not detail['item']['confirmed_current']
                                    checks += 1
                            timings['pair'].append((time.perf_counter()-start)*1000)
                        if producer.returncode is not None and state.get('active_state') == 'stopped':
                            assert state['mode'] == 'archive' and state['rows']
                            assert not any(row['data_fresh'] or row['confirmed_current'] for row in state['rows'])
                            stop_seen = state['mode']
                            break
                    except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as exc:
                        if time.monotonic()-began > 15:
                            errors.append(dict(kind=type(exc).__name__, elapsed_seconds=time.monotonic()-began))
                    except Exception as exc:
                        errors.append(dict(kind=type(exc).__name__, message=str(exc), elapsed_seconds=time.monotonic()-began))
                    sample_index += 1
                    if time.monotonic() >= next_progress:
                        print(f'{time.monotonic()-began:.0f}s: modes={dict(modes)} pair_checks={checks} errors={len(errors)}', flush=True)
                        report.update(modes=dict(modes), pair_checks=checks, errors=errors, elapsed_seconds=time.monotonic()-began)
                        save_report()
                        next_progress += 30
                    await asyncio.sleep(2)
            if producer.returncode is None:
                raise RuntimeError('Producer did not finish within the bounded run')
            with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as db:
                integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
                from arbitrage.chart_history import pending
                backlog = pending(db)
            report.update(state='passed' if modes['live'] and stop_seen and not errors and integrity=='ok' and not any(backlog.values()) else 'failed',
                elapsed_seconds=time.monotonic()-began, modes=dict(modes), errors=errors, pair_checks=checks,
                max_scan_rows=max_rows, max_fresh_rows=max_fresh, expected_pair_misses=expected_pair_misses,
                requests={key:metrics(value) for key,value in timings.items()}, stopped_mode=stop_seen,
                producer_exit_code=producer.returncode, integrity=integrity, chart_backlog=backlog,
                database_bytes=database.stat().st_size, database_growth_bytes=database.stat().st_size-initial_bytes)
            save_report()
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if args.keep_ui_seconds:
                print(f'UI ARCHIVE AVAILABLE for {args.keep_ui_seconds}s', flush=True)
                await asyncio.sleep(args.keep_ui_seconds)
    except BaseException as exc:
        report.update(state='failed', fatal_error=f'{type(exc).__name__}: {exc}')
        save_report()
        raise
    finally:
        for process in (producer, server):
            if process and process.returncode is None:
                process.terminate()
                await process.wait()
    return report['state'] == 'passed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--duration', type=int, default=600)
    parser.add_argument('--port', type=int, default=8769)
    parser.add_argument('--seed-db', type=Path)
    parser.add_argument('--keep-ui-seconds', type=int, default=0)
    args = parser.parse_args()
    if not 30 <= args.duration <= 86400 or not 1024 <= args.port <= 65535 or not 0 <= args.keep_ui_seconds <= 300:
        parser.error('Invalid duration, port, or preview grace period')
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == '__main__':
    main()
