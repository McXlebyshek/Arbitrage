"""Artificial multi-process recovery check. No exchange traffic or real orders."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import aiohttp

from arbitrage.atomic_json import write_json
from arbitrage.funding_history import persist
from arbitrage.paper import PaperLedger
from arbitrage.paper_rules import RulePolicy


async def check(args):
    with socket.socket() as probe: probe.bind(('127.0.0.1', args.port))
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.output.resolve()
    sys.path.insert(0, str(Path(__file__).parent/'tests'))
    from test_paper import document, later
    from test_economics import NOW
    source, registry = document()
    label = 'ARTIFICIAL_MULTIPROCESS_RECOVERY_NOT_MARKET_RESULT'
    paper, market, snapshot, registry_path, config, prices = (root/name for name in
        ('ARTIFICIAL-paper.sqlite3','ARTIFICIAL-market.sqlite3','ARTIFICIAL-latest.json','registry.json','rules-config.json','ARTIFICIAL-prices.json'))
    write_json(registry_path, json.loads(json.dumps(dict(version=label, bindings=list(registry.bindings.values())), default=str)))
    write_json(config, RulePolicy().document())
    epoch = int(time.time()*1000)
    event_ms = epoch+25000
    write_json(prices, [dict(exchange=exchange, native_id='BTC_USDT', timestamp_ms=event_ms,
        mark_price_native=price, source=label) for exchange, price in (('alpha','100'),('beta','104'))])
    flags = dict(creationflags=subprocess.CREATE_NO_WINDOW) if os.name == 'nt' else {}
    processes, logs, starts = {}, [], Counter()
    observations, rule_states, mark_states = Counter(), Counter(), Counter()
    errors, latencies = [], []
    proof = dict(opened=False, depth_blocked=False, stale_blocked=False, stale_ui=False,
        stopped_blocked=False, resumed_hold=False, open_funding_covered=False,
        closed=False, closed_funding_covered=False)
    report = dict(classification=label, state='running', no_exchange_requests=True, real_orders=False,
        synthetic_prices=True, synthetic_funding=True, port=args.port, duration_seconds=args.duration)
    started = time.monotonic()
    restarted = False

    def save():
        report.update(elapsed_seconds=round(time.monotonic()-started,2), proof=proof,
            observations=dict(observations), process_starts=dict(starts), rule_states=dict(rule_states),
            mark_states=dict(mark_states), errors=errors)
        write_json(root/'metrics.json', report, indent=2)

    def shifted(value, offset):
        if isinstance(value, dict): return {key: shifted(item, offset) for key, item in value.items()}
        if isinstance(value, list): return [shifted(item, offset) for item in value]
        if type(value) is int and value >= NOW-10000: return value+offset
        return value

    def produce(elapsed):
        if 14 <= elapsed < 22: return  # Deliberately stop publication until stale.
        now = int(time.time()*1000)
        for exchange, rate in (('alpha','.001'),('beta','.002')):
            events = [dict(timestamp=event_ms+offset, rate=rate) for offset in (-180000,-120000,-60000,0)
                if event_ms+offset <= now]
            persist(market, exchange, dict(id='BTC_USDT',asset='bitcoin'),
                dict(events=events,requests=0,error=None), now)
        value = shifted(later(source) if elapsed >= 48 else source, now-NOW-(1000 if elapsed >= 48 else 0))
        value['validation_source'] = label
        if elapsed >= 8:
            value['signals']['rows'] = []
        if 8 <= elapsed < 14:
            value['economics']['inputs'] = []
        if 22 <= elapsed < 28:
            value['economics']['state'] = 'stopped'
            value['signals']['state'] = 'stopped'
        write_json(snapshot, value)
        observations['producer_frames'] += 1

    async def start(name, module, *arguments):
        log = (root/(name+'.log')).open('ab'); logs.append(log)
        processes[name] = await asyncio.create_subprocess_exec(sys.executable, '-u', '-m', module,
            *arguments, stdout=log, stderr=log, **flags)
        starts[name] += 1

    async def stop(name):
        child = processes.get(name)
        if child and child.returncode is None:
            child.terminate()
            try: await asyncio.wait_for(child.wait(), 10)
            except asyncio.TimeoutError: child.kill(); await child.wait()

    async def workers():
        await start('rules','arbitrage.paper_rules','--watch','--apply','--database',str(paper),
            '--snapshot',str(snapshot),'--status',str(root/'rules-status.json'),'--registry',str(registry_path),'--config',str(config))
        await start('mark','arbitrage.paper_watch','--watch','--database',str(paper),'--snapshot',str(snapshot),
            '--status',str(root/'mark-status.json'),'--registry',str(registry_path))
        await start('funding','arbitrage.paper_funding_watch','--watch','--database',str(paper),'--market-database',str(market),
            '--status',str(root/'funding-status.json'),'--registry',str(registry_path),'--funding-prices',str(prices))

    save()
    try:
        produce(0)
        await start('ui','arbitrage.ui','--database',str(market),'--snapshot',str(snapshot),
            '--paper-database',str(paper),'--registry',str(registry_path),'--port',str(args.port))
        await workers()
        print('Artificial four-process recovery check started; isolated port '+str(args.port),flush=True)
        next_progress = 10
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as client:
            while time.monotonic()-started < args.duration:
                elapsed = time.monotonic()-started
                for name, child in processes.items():
                    if child.returncode is not None: raise RuntimeError(name+' unexpectedly exited')
                produce(elapsed)
                if not restarted and elapsed >= 28:
                    for name in ('rules','mark','funding'): await stop(name)
                    write_json(config, RulePolicy(take_profit_usdt='999').document())
                    await workers()
                    restarted = True
                    report['restart_at_seconds'] = round(elapsed,2)
                try:
                    before = time.perf_counter()
                    async with client.get(f'http://127.0.0.1:{args.port}/api/paper?state=all') as response:
                        if response.status != 200: raise AssertionError('HTTP '+str(response.status))
                        view = await response.json()
                    latencies.append((time.perf_counter()-before)*1000)
                    observations['http_reads'] += 1
                    assert view['classification'] == 'paper_dashboard'
                    assert view['summary']['open_count']+view['summary']['closed_count'] <= 1
                    for row in view['rows']:
                        valuation = row['valuation']
                        if valuation['net_pnl_usdt'] is not None:
                            assert Decimal(valuation['net_pnl_usdt']) == Decimal(valuation['pnl_before_funding_usdt'])+Decimal(valuation['funding_usdt'])
                        if row['state'] == 'open':
                            proof['opened'] = True
                            if 20 <= elapsed < 22 and not row['valuation_fresh']: proof['stale_ui'] = True
                            if valuation['net_pnl_usdt'] is not None and elapsed >= 28: proof['open_funding_covered'] = True
                        else:
                            assert elapsed >= 48, 'Position closed during blocked-data phase'
                            proof['closed'] = True
                            if valuation['net_pnl_usdt'] is not None: proof['closed_funding_covered'] = True
                    for name, counter in (('rules',rule_states),('mark',mark_states)):
                        status_path = root/(name+'-status.json')
                        if not status_path.exists(): continue
                        status = json.loads(status_path.read_text(encoding='utf-8'))
                        counter[status['state']] += 1
                        message = status.get('error','')
                        decisions = status.get('decisions',[])
                        if name == 'rules':
                            if any('awaiting_depth' in row.get('error','') for row in decisions): proof['depth_blocked'] = True
                            if 'snapshot_stale' in message: proof['stale_blocked'] = True
                            if 'economic_snapshot_unavailable' in message: proof['stopped_blocked'] = True
                            if restarted and elapsed > 30 and any(row['action'] == 'hold' for row in decisions): proof['resumed_hold'] = True
                except (aiohttp.ClientConnectorError,asyncio.TimeoutError) as exc:
                    if elapsed > 10: errors.append(dict(kind=type(exc).__name__,at=round(elapsed,2)))
                except Exception as exc:
                    errors.append(dict(kind=type(exc).__name__,message=str(exc),at=round(elapsed,2)))
                if elapsed >= next_progress:
                    save(); print(f'{elapsed:.0f}s: reads={observations["http_reads"]}, proof={sum(proof.values())}/{len(proof)}, errors={len(errors)}',flush=True)
                    next_progress += 10
                await asyncio.sleep(.25)
        for name in ('rules','mark','funding'): await stop(name)
        ledger = PaperLedger(paper).report()
        with closing(sqlite3.connect(paper)) as db:
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
            event_counts = dict(db.execute('SELECT kind,count(*) FROM paper_events GROUP BY kind'))
            position = json.loads(db.execute('SELECT payload FROM paper_positions').fetchone()[0])
        with closing(sqlite3.connect(market)) as db:
            market_integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        funding_ticks = [json.loads(line) for line in (root/'funding.log').read_text(encoding='utf-8').splitlines() if line.startswith('{')]
        assert len(funding_ticks) >= 3, 'Expected initial, restarted and scheduled funding passes'
        assert all(row['requests'] == 0 and not row['network_enabled'] for row in funding_ticks)
        assert position['automation']['policy']['take_profit_usdt'] == '5'
        assert position['close_reason']['reason'] == 'take_profit_before_funding'
        assert position['funding']['end_inclusive_ms'] == position['valuation']['timestamp_ms']
        assert event_counts.get('opened') == event_counts.get('closed') == 1
        assert integrity == market_integrity == 'ok'
        assert len(position['funding']['events']) == 2
        expected_funding = Decimal('4.944')*(-Decimal('100')*Decimal('.001')+Decimal('104')*Decimal('.002'))
        expected_net = Decimal('7.900512')+expected_funding
        assert Decimal(ledger['net_pnl_usdt']) == expected_net
        assert all(proof.values()) and not errors, 'Recovery proof incomplete or errors present'
        latencies.sort()
        report.update(state='passed', sqlite_integrity=integrity, market_sqlite_integrity=market_integrity,
            funding_process_passes=len(funding_ticks), event_counts=event_counts,
            expected_funding_usdt=str(expected_funding), observed_funding_usdt=ledger['funding_usdt'],
            expected_net_usdt=str(expected_net), observed_net_usdt=ledger['net_pnl_usdt'],
            http_p95_ms=latencies[int((len(latencies)-1)*.95)], http_max_ms=max(latencies),
            open_count=ledger['open_count'],closed_count=ledger['closed_count'])
        save();return report
    except BaseException as exc:
        report.update(state='failed',fatal_error=type(exc).__name__+': '+str(exc));save();raise
    finally:
        for name in tuple(processes): await stop(name)
        for log in logs: log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8777)
    parser.add_argument('--duration',type=int,default=95)
    args = parser.parse_args()
    if not 95 <= args.duration <= 300 or not 1024 <= args.port <= 65535: parser.error('Invalid duration/port')
    print(json.dumps(asyncio.run(check(args)),indent=2))


if __name__ == '__main__': main()
