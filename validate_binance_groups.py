"""Concurrent depth5@500ms group-size comparison with 30-second windows. No orders."""
import argparse
import asyncio
import json
from pathlib import Path
import time

from arbitrage.atomic_json import write_json
from arbitrage.live import now_ms
from arbitrage.pools import ExchangePool
from arbitrage.scanner import Registry


async def check(output,duration):
    output.mkdir(parents=True,exist_ok=False)
    registry=Registry.load()
    catalog=json.loads(Path('docs/reports/REGISTRY_V033_CATALOG.json').read_text(encoding='utf-8'))
    pools={}
    values={}
    started=time.monotonic()
    try:
        exchange='binanceusdm'
        markets={row['id']:row for row in catalog[exchange]}
        bases={row['base'] for row in markets.values()}
        for size in (20,10,5):
            mode=f'group{size}'
            pool=ExchangePool(exchange,bases,registry,group_size=size,binance_stream='depth5@500ms')
            pools[mode]=pool
            values[mode]=dict(samples=0,selected_sum=0,fresh_sum=0,max_fresh=0,
                reconnects=0,received_stale_updates=0,stale_stream_reconnects=0,updates=0,
                max_recent_source_lag_p95_ms=None,recent_failures=[],windows={})
            await pool.reconcile(markets)
        while time.monotonic()-started<duration:
            for key,pool in pools.items():
                status=pool.snapshot(now_ms())['status']
                row=values[key]
                row['max_fresh']=max(row['max_fresh'],status['usable_quotes'])
                for field in ('reconnects','received_stale_updates','stale_stream_reconnects','updates'):
                    row[field]=status[field]
                groups=status['connection_statuses']
                lags=[group['source_lag_p95_ms'] for group in groups if group['source_lag_p95_ms'] is not None]
                if lags: row['max_recent_source_lag_p95_ms']=max(lags+[row['max_recent_source_lag_p95_ms'] or max(lags)])
                for group in groups:
                    failure=group.get('last_failure')
                    if failure and failure not in row['recent_failures']:row['recent_failures'].append(failure)
                if time.monotonic()-started>=30:
                    row['samples']+=1;row['selected_sum']+=status['markets'];row['fresh_sum']+=status['usable_quotes']
                    window=str(int((time.monotonic()-started)//30)*30)
                    bucket=row['windows'].setdefault(window,dict(samples=0,selected_sum=0,fresh_sum=0))
                    bucket['samples']+=1;bucket['selected_sum']+=status['markets'];bucket['fresh_sum']+=status['usable_quotes']
            if int(time.monotonic()-started)%15==0:
                print(f'{time.monotonic()-started:.0f}s '+str({key:round(row["fresh_sum"]/row["selected_sum"],3) if row["selected_sum"] else None for key,row in values.items()}),flush=True)
            await asyncio.sleep(1)
        for row in values.values():
            row['mean_sampled_fresh_fraction']=row['fresh_sum']/row['selected_sum'] if row['selected_sum'] else None
            for bucket in row['windows'].values():bucket['fresh_fraction']=bucket['fresh_sum']/bucket['selected_sum'] if bucket['selected_sum'] else None
        result=dict(classification='PUBLIC_CONCURRENT_BINANCE_GROUP_DIAGNOSTIC',elapsed_seconds=time.monotonic()-started,
            warmup_seconds=30,registry_version=registry.version,results=values,
            interpretation='Three pools, same 20 instruments and depth5@500ms, groups 20/10/5, strict freshness, no monitor SQLite workload. Multiple 30s windows; not proof of long-term stability.')
        write_json(output/'metrics.json',result,indent=2)
        return result
    finally:
        for pool in pools.values():
            for key in list(pool.groups):await pool.stop_group(key)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--duration',type=int,default=180)
    args=parser.parse_args()
    if not 90<=args.duration<=600:parser.error('duration must be 90–600')
    print(json.dumps(asyncio.run(check(args.output,args.duration)),indent=2))


if __name__=='__main__':main()
