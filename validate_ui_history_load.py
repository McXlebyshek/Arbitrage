"""CONTROLLED synthetic month-size history benchmark; never genuine market history."""
import argparse
import asyncio
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time

from aiohttp.test_utils import TestClient, TestServer
from arbitrage.chart_history import ensure, MINUTE
from arbitrage.ui import Dashboard, create_app


def metrics(values):
    ordered = sorted(values)
    return dict(count=len(values), median_ms=statistics.median(values), p95_ms=ordered[int((len(ordered)-1)*.95)], max_ms=max(values))


def build(args):
    if args.reuse_fixture:
        marker=json.loads(args.snapshot.read_text(encoding='utf-8')).get('validation_fixture','')
        if not marker.startswith('CONTROLLED SYNTHETIC BENCHMARK'):
            raise ValueError('Only a marked controlled fixture may be reused')
        document=Dashboard(args.snapshot,args.database).archive()
        selected=document['scanner']['rows'][:args.pairs]
        with closing(sqlite3.connect(args.database)) as db:
            if db.execute('SELECT count(*) FROM chart_rollups').fetchone()[0] != (args.days*1440+1)*args.pairs-1:
                raise ValueError('Existing fixture dimensions do not match')
        end=document['generated_at_ms']
        previous=json.loads(args.report.read_text(encoding='utf-8')) if args.report.exists() else {}
        return end,selected,end//MINUTE*MINUTE-9000*MINUTE,previous.get('build_seconds')
    if args.database.exists() or args.snapshot.exists():
        raise ValueError('Benchmark output paths must be new')
    document = Dashboard(args.source_snapshot, args.source_db).archive()
    if not document or len(document['scanner']['rows']) < args.pairs:
        raise ValueError('Source must contain enough real scan directions for templates')
    args.database.parent.mkdir(parents=True, exist_ok=True)
    end, count = document['generated_at_ms'], args.days*1440+1
    first = end//MINUTE*MINUTE-(count-1)*MINUTE
    selected = document['scanner']['rows'][:args.pairs]
    reverse = {(r['asset'],r['long_exchange'],r['short_exchange']):r for r in document['scanner']['rows']}
    began = time.perf_counter()
    with closing(sqlite3.connect(args.database)) as db, db:
        ensure(db)
        db.execute('CREATE TABLE scans(id INTEGER PRIMARY KEY,generated_at_ms INTEGER NOT NULL,payload TEXT NOT NULL)')
        db.execute('CREATE INDEX scans_time ON scans(generated_at_ms)')
        # Small legacy raw rows exercise the indexed history alongside a large rollup table.
        db.execute("WITH RECURSIVE ticks(i) AS (VALUES(0) UNION ALL SELECT i+1 FROM ticks WHERE i<199999) INSERT INTO scans(generated_at_ms,payload) SELECT ?+i*1000,'{\"rows\":[]}' FROM ticks", (end-200000000,))
        db.execute('INSERT INTO scans(generated_at_ms,payload) VALUES (?,?)', (end,json.dumps(document['scanner'])))
        db.execute('CREATE TABLE economics(id INTEGER PRIMARY KEY,generated_at_ms INTEGER NOT NULL,payload TEXT NOT NULL)')
        db.execute('CREATE INDEX economics_time ON economics(generated_at_ms)')
        db.execute('INSERT INTO economics(generated_at_ms,payload) VALUES (?,?)', (end,json.dumps(document['economics'] or {})))
        db.execute('CREATE TABLE snapshots(id INTEGER PRIMARY KEY,collected_at_ms INTEGER,exchange TEXT,payload TEXT)')
        db.execute('CREATE INDEX snapshots_exchange_id ON snapshots(exchange,id)')
        db.execute('CREATE INDEX snapshots_time ON snapshots(collected_at_ms)')
        for exchange in document['exchanges']:
            db.execute('INSERT INTO snapshots(collected_at_ms,exchange,payload) VALUES (?,?,?)', (end,exchange['status']['exchange'],json.dumps(exchange)))
        for index,row in enumerate(selected):
            other = reverse.get((row['asset'],row['short_exchange'],row['long_exchange']))
            fields = dict(entry=row['open_spread_pct'],exit=other['open_spread_pct'] if other else None,
                          long_price=row['long_ask_usdt_per_unit'],short_price=row['short_bid_usdt_per_unit'])
            payload = json.dumps({key:dict(last=value,min=value,max=value) for key,value in fields.items()}, separators=(',',':'))
            db.execute('WITH RECURSIVE ticks(i) AS (VALUES(0) UNION ALL SELECT i+1 FROM ticks WHERE i<?) INSERT INTO chart_rollups SELECT ?,?+i*60000,?+i*60000,?+i*60000,1,? FROM ticks',
                       (count-1,'pair:'+row['id'],first,first,first,payload))
            print(f'CONTROLLED FIXTURE: {index+1}/{len(selected)} series',flush=True)
        # Missing minute in one series must remain absent after a large indexed read.
        gap = end//MINUTE*MINUTE-9000*MINUTE
        db.execute('DELETE FROM chart_rollups WHERE series=? AND start_ms=?', ('pair:'+selected[0]['id'],gap))
        db.execute("INSERT INTO chart_checkpoints VALUES ('scans',200001)")
        db.execute("INSERT INTO chart_checkpoints VALUES ('economics',1)")
    stopped = dict(document, validation_fixture='CONTROLLED SYNTHETIC BENCHMARK: REPEATED PRICE TEMPLATES, NOT MARKET HISTORY')
    for exchange in stopped['exchanges']:
        exchange['status']['state'] = 'stopped'
    stopped['scanner'] = dict(document['scanner'],rows=[])
    args.snapshot.write_text(json.dumps(stopped),encoding='utf-8')
    return end,selected,gap,time.perf_counter()-began


async def run(args):
    end, selected, gap, build_seconds = build(args)
    timings, sizes, errors, verified, gap_checks = [], [], [], 0, 0
    store = Dashboard(args.snapshot,args.database)
    start = time.perf_counter()
    state = store.state()
    cold_state_ms = (time.perf_counter()-start)*1000
    with closing(sqlite3.connect(args.database)) as db:
        rows = db.execute('SELECT count(*) FROM chart_rollups').fetchone()[0]
        plan = db.execute('EXPLAIN QUERY PLAN SELECT payload FROM chart_rollups WHERE series=? AND start_ms>=? AND start_ms<=?',('pair:'+selected[0]['id'],end-86400000,end)).fetchall()
        assert any('SEARCH' in row[3] and 'INDEX' in row[3] for row in plan), plan
    async with TestClient(TestServer(create_app(args.snapshot,args.database))) as client:
        async def request(index):
            nonlocal verified, gap_checks
            row = selected[index % len(selected)]
            boundary = end-(index % (args.days-1))*86400000
            if index==0 and args.days>=8:
                boundary=end-6*86400000  # Explicitly exercise the deliberately missing minute.
            begin = boundary-86400000
            expected = math.floor(boundary/MINUTE)-math.ceil(begin/MINUTE)+1
            if row['id']==selected[0]['id'] and begin<=gap<=boundary:
                expected -= 1
            start = time.perf_counter()
            try:
                async with client.get('/api/pair',params=dict(id=row['id'],minutes=1440,end_ms=boundary)) as response:
                    body = await response.read()
                    assert response.status==200,(response.status,body[:100])
                value = json.loads(body)
                assert len(value['points'])==expected,(len(value['points']),expected)
                assert value['history_source']=='minute' and not value['truncated']
                assert all(begin<=p['time']<=boundary for p in value['points'])
                if row['id']==selected[0]['id']:
                    assert all(p['time']!=gap for p in value['points'])
                    if begin<=gap<=boundary:
                        gap_checks += 1
                assert not value['item']['confirmed_current']
                verified += 1
                sizes.append(len(body))
            except Exception as exc:
                errors.append(f'{type(exc).__name__}: {exc}')
            timings.append((time.perf_counter()-start)*1000)
        for batch in range(0,args.requests,args.concurrency):
            await asyncio.gather(*(request(index) for index in range(batch,min(batch+args.concurrency,args.requests))))
    with closing(sqlite3.connect(args.database)) as db:
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    result = dict(classification='CONTROLLED_SYNTHETIC_HISTORY_NOT_MARKET_DATA',state='passed' if not errors and integrity=='ok' and (gap_checks or args.days<8) else 'failed',
        days=args.days, directions=len(selected), chart_rows=rows, raw_rows=200001, database_bytes=args.database.stat().st_size,
        build_seconds=build_seconds, cold_archive_state_ms=cold_state_ms, concurrency=args.concurrency, requests=metrics(timings),
        verified_responses=verified, verified_gap_windows=gap_checks, fixture_reused=args.reuse_fixture, max_response_bytes=max(sizes) if sizes else 0, errors=errors, integrity=integrity,
        query_plan=plan, observation='Repeated normalized templates, intentionally missing minute; no 30-day public market coverage or 300-direction scale claim')
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)
    return result['state']=='passed'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db',type=Path,required=True)
    parser.add_argument('--source-snapshot',type=Path,required=True)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--days',type=int,default=30)
    parser.add_argument('--pairs',type=int,default=30)
    parser.add_argument('--requests',type=int,default=120)
    parser.add_argument('--concurrency',type=int,default=4)
    parser.add_argument('--reuse-fixture',action='store_true',help='Read only the existing marked fixture; validate its dimensions first')
    args=parser.parse_args()
    if not 2<=args.days<=30 or not 1<=args.pairs<=30 or not 1<=args.requests<=1000 or not 1<=args.concurrency<=8:
        parser.error('Benchmark bounds exceeded')
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__=='__main__':
    main()
