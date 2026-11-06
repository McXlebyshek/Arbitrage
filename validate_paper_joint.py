"""Controlled concurrent paper marking/funding/read checks, with artificial time and prices."""
import argparse
import asyncio
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
import time

from arbitrage.funding_history import persist
from arbitrage.paper import PaperLedger,encode
from arbitrage.paper_dashboard import read_dashboard
from arbitrage.paper_funding_watch import FundingWatch
from arbitrage.paper_watch import tick


async def check(output,duration):
    sys.path.insert(0,str(Path(__file__).parent/'tests'))
    from test_paper import document,later
    from test_economics import NOW,HOUR
    snapshot,registry=document()
    output.mkdir(parents=True,exist_ok=False)
    ledger_path,market,path,prices_path=[output/name for name in ('ARTIFICIAL-paper.sqlite3','ARTIFICIAL-market.sqlite3','ARTIFICIAL-latest.json','ARTIFICIAL-prices.json')]
    ledger=PaperLedger(ledger_path)
    position=ledger.open(snapshot,'test',registry,now_ms=NOW)
    for exchange,hours,rates in (('alpha',[-16,-8,0,8],['.001']*4),('beta',[-8,-4,0,4,8,12],['.001','.001','.001','.002','.001','.001'])):
        persist(market,exchange,dict(id='BTC_USDT',asset='bitcoin'),dict(events=[dict(timestamp=NOW+h*HOUR,rate=r) for h,r in zip(hours,rates)],requests=0,error=None),NOW+12*HOUR)
    prices=[dict(exchange=x,native_id='BTC_USDT',timestamp_ms=NOW+h*HOUR,mark_price_native=p,source='ARTIFICIAL JOINT CHECK') for x,h,p in [('alpha',8,'100'),('beta',4,'102'),('beta',8,'104'),('beta',12,'103')]]
    prices_path.write_text(encode(prices),encoding='utf-8')
    worker=FundingWatch(ledger_path,market,registry,prices=prices_path)
    lock=asyncio.Lock()
    done=False
    clock=NOW
    marks=reads=fundings=0
    failures=[]
    class NoNetwork:
        def get(self,*args,**kwargs): raise AssertionError('Network request in offline joint check')
    session=NoNetwork()
    started=time.monotonic()
    async def produce():
        nonlocal done,clock
        for index in range(1,61):
            value=later(snapshot,index*12*HOUR//60)
            async with lock:
                temporary=path.with_suffix('.tmp')
                temporary.write_text(encode(value),encoding='utf-8')
                temporary.replace(path)
                clock=value['generated_at_ms']
            await asyncio.sleep(duration/60)
        done=True
    async def marking():
        nonlocal marks
        while not done:
            async with lock:
                if path.exists():
                    result=await asyncio.to_thread(tick,ledger_path,path,registry,clock)
                    if result['state']!='ready': failures.append(dict(kind='mark',result=result))
                    marks+=1
            await asyncio.sleep(.05)
    async def funding():
        nonlocal fundings
        while not done:
            result=await worker.tick(session)
            if result['state']!='ready': failures.append(dict(kind='funding',result=result))
            fundings+=1
            await asyncio.sleep(.1)
    def inspect():
        value=read_dashboard(ledger_path,now_ms=clock)
        if value['summary']['open_count']!=1: raise AssertionError('Position unexpectedly closed')
        with closing(sqlite3.connect(ledger_path)) as db:
            item=json.loads(db.execute('SELECT payload FROM paper_positions WHERE id=?',(position['id'],)).fetchone()[0])
        v=item['valuation']
        if v['net_pnl_usdt'] is not None:
            if item['funding']['end_inclusive_ms']!=v['timestamp_ms']: raise AssertionError('Mixed funding/valuation window')
            if Decimal(v['net_pnl_usdt'])!=Decimal(v['pnl_before_funding_usdt'])+Decimal(v['funding_usdt']): raise AssertionError('Mixed totals')
    async def reading():
        nonlocal reads
        while not done:
            await asyncio.to_thread(inspect)
            reads+=1
            await asyncio.sleep(.05)
    await asyncio.gather(produce(),marking(),funding(),reading())
    final=json.loads(path.read_text(encoding='utf-8'))
    ledger.value(final,position['id'],registry,close=True,now_ms=clock)
    await worker.tick(session)
    report=ledger.report()
    expected_funding=Decimal('4.944')*(-Decimal('100')*Decimal('.001')+Decimal('102')*Decimal('.002')+Decimal('104')*Decimal('.001')+Decimal('103')*Decimal('.001'))
    expected_net=Decimal('7.900512')+expected_funding
    with closing(sqlite3.connect(ledger_path)) as db:
        integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
    passed=not failures and integrity=='ok' and report['closed_count']==1 and report['open_count']==0 and Decimal(report['net_pnl_usdt'])==expected_net
    result=dict(classification='ARTIFICIAL_CONCURRENT_PAPER_CHECK_NOT_MARKET_RESULT',wall_seconds=round(time.monotonic()-started,2),
        accelerated_market_hours=12,producer_frames=60,mark_checks=marks,funding_checks=fundings,read_checks=reads,
        failures=failures,integrity=integrity,expected_funding_usdt=str(expected_funding),observed_funding_usdt=report['funding_usdt'],
        expected_net_usdt=str(expected_net),observed_net_usdt=report['net_pnl_usdt'],network_requests=0,passed=passed)
    (output/'metrics.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('data/paper-joint-check'))
    parser.add_argument('--duration',type=int,default=30)
    args=parser.parse_args()
    if not 5<=args.duration<=300: parser.error('--duration must be 5–300')
    result=asyncio.run(check(args.output,args.duration))
    print(json.dumps(result,indent=2))
    if not result['passed']: raise SystemExit(1)


if __name__=='__main__': main()
