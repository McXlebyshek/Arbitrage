"""Two bounded public requests to verify Binance historical settlement mark parsing."""
import argparse
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import time

import aiohttp

from arbitrage.scanner import Registry
from arbitrage.settlement_marks import BINANCE_URL, fetch_binance_marks


async def check(symbol):
    now=int(time.time()*1000)
    report=dict(classification='PUBLIC_READ_ONLY_SETTLEMENT_MARK_CHECK',symbol=symbol,checked_at_ms=now,
        requests=0,passed=False,execution_verified=False)
    try:
        async with aiohttp.ClientSession() as session:
            report['requests']+=1
            async with session.get(BINANCE_URL,params=dict(symbol=symbol,startTime=now-86400000,endTime=now,limit=100),
                    timeout=aiohttp.ClientTimeout(total=15),allow_redirects=False) as response:
                if response.status!=200: raise ValueError('public_history_http_'+str(response.status))
                rows=await response.json()
            if not isinstance(rows,list) or not rows: raise ValueError('empty_public_history')
            latest=max(rows,key=lambda row:row['fundingTime'])
            result=await fetch_binance_marks(session,symbol,{latest['fundingTime']:latest['fundingRate']})
            report['requests']+=result['requests']
            report.update(timestamp_ms=latest['fundingTime'],rate=latest['fundingRate'],reference_mark_price_native=latest.get('markPrice'),
                downloaded_mark_price_native=result['prices'][0]['mark_price_native'] if result['prices'] else None,error=result['error'])
            report['passed']=bool(result['prices'] and result['error'] is None
                and Decimal(result['prices'][0]['mark_price_native'])==Decimal(latest['markPrice']))
    except (ValueError,TypeError,KeyError,aiohttp.ClientError,asyncio.TimeoutError) as exc:
        report['error']=type(exc).__name__+': '+str(exc)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol',default='BTCUSDT')
    parser.add_argument('--output',type=Path,default=Path('data/settlement-live-check'))
    args=parser.parse_args()
    if ('binanceusdm',args.symbol) not in Registry.load().bindings:
        parser.error('Symbol is outside the explicit Binance registry')
    args.output.mkdir(parents=True,exist_ok=False)
    report=asyncio.run(check(args.symbol))
    (args.output/'metrics.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
    if not report['passed']: raise SystemExit(1)


if __name__=='__main__': main()
