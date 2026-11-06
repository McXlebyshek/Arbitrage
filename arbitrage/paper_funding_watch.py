"""Opt-in background paper funding reconciliation; offline unless --fetch-prices."""
import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from .atomic_json import write_json

import aiohttp

from .paper import PaperLedger
from .paper_watch import check_status_path
from .recovery import SessionInUse, SessionLease
from .scanner import Registry
from .settlement_marks import fetch_position_prices, load_position

CLASSIFICATION = 'paper_funding_watch_status'


def selection(database, cursor):
    path=Path(database)
    empty=dict(ids=[],cursor=0,open_count=0,closed_count=0,omitted_open=0,state='absent')
    if not path.is_file(): return empty
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_meta'").fetchone():
            return dict(empty,state='unsupported_ledger')
        mode=db.execute('SELECT mode FROM paper_meta').fetchone()
        if not mode or mode[0]!='live_snapshot': return dict(empty,state='ignored_replay_or_empty')
        opened=db.execute("SELECT id FROM paper_positions WHERE state='open' ORDER BY rowid LIMIT 10").fetchall()
        closed=db.execute("SELECT rowid,id FROM paper_positions WHERE state='closed' AND rowid>? ORDER BY rowid LIMIT 5",(cursor,)).fetchall()
        if not closed:
            closed=db.execute("SELECT rowid,id FROM paper_positions WHERE state='closed' ORDER BY rowid LIMIT 5").fetchall()
        counts=dict(db.execute('SELECT state,count(*) FROM paper_positions GROUP BY state'))
        return dict(ids=[row[0] for row in opened]+[row[1] for row in closed],cursor=closed[-1][0] if closed else 0,
            open_count=counts.get('open',0),closed_count=counts.get('closed',0),
            omitted_open=max(0,counts.get('open',0)-10),state='ready')


class FundingWatch:
    def __init__(self,database,market_database,registry,fetch=False,prices=None,interval=60,cursor=0,next_network_ms=0):
        self.database,self.market=Path(database),Path(market_database)
        if self.database.resolve()==self.market.resolve() or self.database.exists() and self.market.exists() and self.database.samefile(self.market):
            raise ValueError('paper_and_market_databases_must_differ')
        self.registry,self.fetch,self.prices=registry,fetch,prices
        self.interval,self.cursor,self.next_network_ms=interval,cursor,next_network_ms

    async def tick(self,session=None,now_ms=None):
        now=int(time.time()*1000) if now_ms is None else now_ms
        report=dict(classification=CLASSIFICATION,generated_at_ms=now,positions=[],requests=0,
            network_enabled=self.fetch,execution_verified=False,opens_positions=False,closes_positions=False)
        try:
            chosen=await asyncio.to_thread(selection,self.database,self.cursor)
            prices=json.loads(Path(self.prices).read_text(encoding='utf-8-sig')) if self.prices else []
            from .paper_funding import price_index
            price_index(prices)
        except (ValueError,KeyError,TypeError,OSError,sqlite3.Error) as exc:
            return dict(report,state='input_error',error=str(exc),cursor=self.cursor,next_network_ms=self.next_network_ms)
        report['selection']=chosen
        self.cursor=chosen['cursor']
        for position_id in chosen['ids']:
            try:
                position=await asyncio.to_thread(load_position,self.database,position_id)
                # Cached prices are useful even in offline mode or during network cooldown.
                budget=3-report['requests'] if self.fetch and now>=self.next_network_ms else 0
                downloaded=await fetch_position_prices(position,self.market,self.registry,prices,session=session,max_requests=budget)
                details=downloaded['report']
                report['requests']+=details['requests']
                errors=[row['error'] for row in details['downloads'] if row['error'] and row['error']!='network_budget_deferred']
                if errors:
                    clock=int(time.time()*1000) if now_ms is None else now_ms
                    self.next_network_ms=max(self.next_network_ms,clock+1000*max(self.interval,details['retry_after_seconds']))
                result=await asyncio.to_thread(PaperLedger(self.database).funding,position_id,self.market,downloaded['prices'],now_ms)
                report['positions'].append(dict(id=position_id,state=result['funding']['status'],
                    end_ms=result['funding']['end_inclusive_ms'],download=details))
            except (ValueError,KeyError,TypeError,ArithmeticError,OSError,sqlite3.Error) as exc:
                report['positions'].append(dict(id=position_id,state='blocked',error=str(exc)))
        ready=bool(report['positions']) and all(row['state']=='covered_paper_cashflows' for row in report['positions'])
        state='ready' if ready else 'partial' if report['positions'] else 'idle'
        if chosen.get('omitted_open'): state='partial'
        report.update(state=state,cursor=self.cursor,next_network_ms=self.next_network_ms)
        return report


async def run(args):
    registry=Registry.load(args.registry)
    check_status_path(args.status,[args.database,args.market_database,args.registry,args.funding_prices],CLASSIFICATION)
    previous=json.loads(args.status.read_text(encoding='utf-8-sig')) if args.status.exists() else {}
    cursor=previous.get('cursor',0)
    cooldown=previous.get('next_network_ms',0)
    if type(cursor) is not int or not 0<=cursor<2**63 or type(cooldown) is not int or not 0<=cooldown<=int(time.time()*1000)+3600000:
        raise ValueError('invalid_funding_watch_checkpoint')
    worker=FundingWatch(args.database,args.market_database,registry,args.fetch_prices,args.funding_prices,args.interval,cursor,cooldown)
    with SessionLease(str(args.database.resolve())+'.funding-watch'):
        async with aiohttp.ClientSession() as session:
            while True:
                report=await worker.tick(session)
                await asyncio.to_thread(write_json,args.status,report)
                print(json.dumps(report,ensure_ascii=False),flush=True)
                if not args.watch: break
                await asyncio.sleep(args.interval)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=Path('data/paper.sqlite3'))
    parser.add_argument('--market-database',type=Path,default=Path('data/market.sqlite3'))
    parser.add_argument('--status',type=Path,default=Path('data/paper-funding-watch.json'))
    parser.add_argument('--registry',type=Path)
    parser.add_argument('--funding-prices',type=Path)
    parser.add_argument('--fetch-prices',action='store_true')
    parser.add_argument('--interval',type=int,default=60)
    parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    if not 60<=args.interval<=3600: parser.error('--interval must be 60–3600 seconds')
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt: pass
    except SessionInUse: parser.error('Another funding watcher is using this ledger')
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error) as exc: parser.error(str(exc))


if __name__=='__main__': main()
