"""Bounded multi-process public-data soak in a new directory; never seeds a signal."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import aiohttp

from arbitrage.paper import PaperLedger
from arbitrage.atomic_json import write_json
from arbitrage.scanner import Registry


def latency(values):
    values=sorted(values)
    return dict(count=len(values),p95_ms=values[int((len(values)-1)*.95)] if values else None,max_ms=max(values) if values else None)


async def run(args):
    with socket.socket() as probe: probe.bind(('127.0.0.1',args.port))
    args.directory.mkdir(parents=True,exist_ok=False)
    root=args.directory.resolve()
    market,snapshot,paper=root/'market.sqlite3',root/'latest.json',root/'paper.sqlite3'
    report_path=root/'metrics.json'
    registry=Registry.load()
    ledger=PaperLedger(paper)
    flags=dict(creationflags=subprocess.CREATE_NO_WINDOW) if os.name=='nt' else {}
    processes,logs={},[]
    timings={name:[] for name in ('state','paper','alerts')}
    modes,mark_states,funding_states=Counter(),Counter(),Counter()
    errors,entry_rejections,close_rejections=[],Counter(),Counter()
    opened=None
    restarted=False
    archived=False
    max_rows=max_fresh=confirmed_snapshots=0
    report=dict(classification='ISOLATED_PUBLIC_MULTIPROCESS_PAPER_SOAK',state='running',
        requested_seconds=args.duration,started_at_ms=int(time.time()*1000),port=args.port,
        synthetic_signals=False,real_orders=False,external_messages=False)
    started=time.monotonic()

    def save():
        report.update(elapsed_seconds=round(time.monotonic()-started,2),modes=dict(modes),max_rows=max_rows,max_fresh=max_fresh,
            confirmed_snapshots=confirmed_snapshots,opened_position_id=opened,entry_rejections=dict(entry_rejections),close_rejections=dict(close_rejections),
            mark_states=dict(mark_states),funding_states=dict(funding_states),errors=errors,
            requests={name:latency(values) for name,values in timings.items()})
        write_json(report_path,report,indent=2)

    async def start(name,*arguments):
        handle=(root/(name+'.log')).open('ab');logs.append(handle)
        processes[name]=await asyncio.create_subprocess_exec(sys.executable,'-u','-m',*arguments,stdout=handle,stderr=handle,**flags)

    async def stop(name):
        process=processes.get(name)
        if process and process.returncode is None:
            process.terminate()
            try: await asyncio.wait_for(process.wait(),15)
            except asyncio.TimeoutError:
                process.kill();await process.wait()

    async def workers():
        await start('mark','arbitrage.paper_watch','--watch','--database',str(paper),'--snapshot',str(snapshot),'--status',str(root/'mark-status.json'))
        await start('funding','arbitrage.paper_funding_watch','--watch','--database',str(paper),'--market-database',str(market),'--status',str(root/'funding-status.json'))

    save()
    try:
        await start('ui','arbitrage.ui','--database',str(market),'--snapshot',str(snapshot),'--paper-database',str(paper),'--port',str(args.port))
        await workers()
        await start('monitor','arbitrage','--duration',str(args.duration),'--interval','2','--top','0',
            '--maintenance-seconds','60','--database',str(market),'--output',str(snapshot),'--paper-database',str(paper),'--report',str(root/'monitor-report.json'))
        print(f'Public soak started: {args.duration}s, isolated UI http://127.0.0.1:{args.port}',flush=True)
        progress=started+30
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as client:
            while time.monotonic()-started<args.duration+100:
                elapsed=time.monotonic()-started
                for name in ('ui','mark','funding'):
                    if processes[name].returncode is not None: raise RuntimeError(name+' exited unexpectedly')
                if not restarted and elapsed>=args.duration/2:
                    await stop('mark');await stop('funding');await workers()
                    restarted=True;report['paper_workers_restarted_at_seconds']=round(elapsed,2)
                try:
                    replies={}
                    for name in timings:
                        before=time.perf_counter()
                        async with client.get(f'http://127.0.0.1:{args.port}/api/{name}') as response:
                            if response.status!=200: raise RuntimeError(name+' HTTP '+str(response.status))
                            replies[name]=await response.json()
                        timings[name].append((time.perf_counter()-before)*1000)
                    state=replies['state'];modes[state['mode']]+=1
                    max_rows=max(max_rows,len(state['rows']))
                    max_fresh=max(max_fresh,sum(row['data_fresh'] for row in state['rows']))
                    for row in state['rows']:
                        if row['confirmed_current']:
                            assert state['mode']=='live' and row['data_fresh'] and row['economic_fresh']
                    view=replies['paper']
                    assert view['classification']=='paper_dashboard'
                    assert len(view['statistics']['points'])<=281
                    if view['summary']['covered_closed_count']<view['summary']['closed_count']:
                        assert view['summary']['closed_net_usdt'] is None
                    if snapshot.is_file() and processes['monitor'].returncode is None:
                        document=json.loads(snapshot.read_text(encoding='utf-8'))
                        qualifying=[row for row in (document.get('signals') or {}).get('rows',[]) if row['state']=='confirmed']
                        confirmed_snapshots+=bool(qualifying)
                        if qualifying and opened is None and elapsed<args.duration-30:
                            for row in qualifying:
                                try:
                                    position=await asyncio.to_thread(ledger.open,document,row['id'],registry)
                                    opened=position['id'];break
                                except (ValueError,KeyError,TypeError,sqlite3.Error) as exc: entry_rejections[str(exc)]+=1
                        if opened and elapsed>=args.duration-15:
                            try: await asyncio.to_thread(ledger.value,document,opened,registry,True)
                            except (ValueError,KeyError,TypeError,sqlite3.Error) as exc: close_rejections[str(exc)]+=1
                    for filename,counter in (('mark-status.json',mark_states),('funding-status.json',funding_states)):
                        path=root/filename
                        if path.exists(): counter[json.loads(path.read_text(encoding='utf-8'))['state']]+=1
                    if processes['monitor'].returncode is not None and state.get('active_state')=='stopped':
                        assert not any(row['data_fresh'] or row['confirmed_current'] for row in state['rows'])
                        archived=state['mode']=='archive'
                        break
                except (aiohttp.ClientConnectorError,asyncio.TimeoutError) as exc:
                    if elapsed>15: errors.append(dict(kind=type(exc).__name__,at=round(elapsed,2)))
                except Exception as exc:
                    errors.append(dict(kind=type(exc).__name__,message=str(exc),at=round(elapsed,2)))
                if time.monotonic()>=progress:
                    save();print(f'{elapsed:.0f}s: live={modes["live"]} rows={max_rows} confirmed={confirmed_snapshots} paper={opened is not None} errors={len(errors)}',flush=True)
                    progress+=30
                await asyncio.sleep(2)
        if processes['monitor'].returncode is None: raise RuntimeError('Monitor exceeded bounded stop deadline')
        # Allow the 2-second mark watcher to observe the stopped quote source.
        await asyncio.sleep(3)
        integrity={}
        for name,path in (('market',market),('paper',paper)):
            if path.exists():
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                    integrity[name]=db.execute('PRAGMA integrity_check').fetchone()[0]
        positions=ledger.report()
        monitor=json.loads((root/'monitor-report.json').read_text(encoding='utf-8')) if (root/'monitor-report.json').exists() else {}
        passed=bool(modes['live']) and archived and restarted and not errors and integrity.get('market')=='ok' and all(value=='ok' for value in integrity.values())
        report.update(state='passed' if passed else 'failed',orchestration_passed=passed,stopped_archive_seen=archived,integrity=integrity,
            paper_path='exercised_natural_confirmed' if opened else 'not_exercised_no_accepted_confirmed',
            paper_open_count=positions['open_count'],paper_closed_count=positions['closed_count'],
            monitor_exit_code=processes['monitor'].returncode,market_data_quality='complete_at_stop' if processes['monitor'].returncode==0 else 'data_gaps_at_stop',
            monitor_report=monitor,database_bytes={name:path.stat().st_size for name,path in (('market',market),('paper',paper)) if path.exists()})
        report['final_worker_status']={name:json.loads((root/filename).read_text(encoding='utf-8'))
            for name,filename in (('mark','mark-status.json'),('funding','funding-status.json')) if (root/filename).exists()}
        save();print(json.dumps({key:report[key] for key in ('state','paper_path','market_data_quality','elapsed_seconds','errors','integrity')},indent=2),flush=True)
        return passed
    except BaseException as exc:
        report.update(state='failed',fatal_error=type(exc).__name__+': '+str(exc));save();raise
    finally:
        for name in tuple(processes): await stop(name)
        for handle in logs: handle.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--duration',type=int,default=300)
    parser.add_argument('--port',type=int,default=8776)
    args=parser.parse_args()
    if not 60<=args.duration<=86400 or not 1024<=args.port<=65535: parser.error('Invalid duration or port')
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__=='__main__': main()
