"""Concurrent public Gate size-format comparison; bounded matching wire evidence."""
import argparse
import asyncio
from collections import Counter,OrderedDict
import json
from pathlib import Path
import time
from unittest.mock import patch

from arbitrage.atomic_json import write_json
from arbitrage.live import LiveExchange,now_ms
from arbitrage.models import number
from arbitrage.pools import ExchangePool
from arbitrage.scanner import Registry


async def check(output,duration):
    output.mkdir(parents=True,exist_ok=False)
    registry=Registry.load()
    markets={row['id']:row for row in json.loads(Path('docs/reports/REGISTRY_V033_CATALOG.json').read_text(encoding='utf-8'))['gate']}
    pending={False:OrderedDict(),True:OrderedDict()};evidence=[];seen=set()
    class CaptureExchange(LiveExchange):
        async def parse_quotes(self,session,payload,received):
            if payload.get('channel')=='futures.book_ticker' and payload.get('event')=='update':
                row=payload.get('result') or {}
                key=(row.get('s'),str(row.get('u')),str(row.get('t')))
                peer=pending[not self.gate_decimal_sizes].pop(key,None)
                if peer is not None:
                    old,new=(peer,row) if self.gate_decimal_sizes else (row,peer)
                    same_prices=all(number(old.get(field))==number(new.get(field)) for field in ('b','a'))
                    recovered=[field for field in ('B','A') if number(old.get(field))==0 and number(new.get(field)) is not None and 0<number(new[field])<1]
                    if same_prices and recovered and key not in seen and len(evidence)<20:
                        seen.add(key);evidence.append(dict(symbol=key[0],update_id=key[1],timestamp_ms=row.get('t'),
                            recovered_fields=recovered,legacy={field:old.get(field) for field in ('b','a','B','A')},
                            decimal={field:new.get(field) for field in ('b','a','B','A')}))
                else:
                    saved=pending[self.gate_decimal_sizes];saved[key]=row
                    if len(saved)>10000:saved.popitem(last=False)
            return await super().parse_quotes(session,payload,received)
    pools={};values={};started=time.monotonic()
    try:
        with patch('arbitrage.pools.LiveExchange',CaptureExchange):
            for enabled in (False,True):
                name='decimal' if enabled else 'legacy_integer'
                pool=ExchangePool('gate',{row['base'] for row in markets.values()},registry,group_size=5,gate_decimal_sizes=enabled)
                pools[name]=pool
                values[name]=dict(samples=0,fresh_sum=0,selected_sum=0,quality_counts=Counter(),by_symbol={},
                    reconnects=0,fractional_size_updates=0)
                await pool.reconcile(markets)
            progress=15
            while time.monotonic()-started<duration:
                elapsed=time.monotonic()-started
                for name,pool in pools.items():
                    snapshot=pool.snapshot(now_ms());status=snapshot['status'];row=values[name]
                    row['reconnects']=status['reconnects'];row['fractional_size_updates']=status['fractional_size_updates']
                    if elapsed>=20:
                        row['samples']+=1;row['fresh_sum']+=status['usable_quotes'];row['selected_sum']+=status['markets']
                        quotes={q['exchange_symbol']:q for q in snapshot['quotes']}
                        for symbol in markets:
                            quality=quotes[symbol]['quality'] if symbol in quotes else 'absent'
                            if symbol in quotes and quotes[symbol]['connection_state']!='streaming':quality='disconnected'
                            row['quality_counts'][quality]+=1
                            row['by_symbol'].setdefault(symbol,Counter())[quality]+=1
                if elapsed>=progress:
                    print(f'{elapsed:.0f}s matches={len(evidence)} '+str({key:round(row['fresh_sum']/row['selected_sum'],3) if row['selected_sum'] else None for key,row in values.items()}),flush=True)
                    progress+=15
                await asyncio.sleep(1)
            for row in values.values():row['mean_sampled_fresh_fraction']=row['fresh_sum']/row['selected_sum'] if row['selected_sum'] else None
            result=dict(classification='PUBLIC_GATE_DECIMAL_SIZE_COMPARISON',elapsed_seconds=time.monotonic()-started,
                warmup_seconds=20,group_size=5,results=values,matched_rounding_evidence=evidence,
                interpretation='Same instruments and concurrent sockets. Wire evidence matches symbol/u/t/prices; sampled quality is not uptime.')
            write_json(output/'metrics.json',result,indent=2)
            return result
    finally:
        for pool in pools.values():
            for key in list(pool.groups):await pool.stop_group(key)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--duration',type=int,default=120)
    args=parser.parse_args()
    if not 30<=args.duration<=300:parser.error('duration must be 30–300')
    result=asyncio.run(check(args.output,args.duration))
    print(json.dumps(dict(elapsed_seconds=result['elapsed_seconds'],evidence_count=len(result['matched_rounding_evidence']),
        results={key:{k:v for k,v in row.items() if k!='by_symbol'} for key,row in result['results'].items()}),indent=2))


if __name__=='__main__':main()
