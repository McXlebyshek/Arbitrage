from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO
import sqlite3
import time
import unittest
from unittest.mock import AsyncMock, patch

from arbitrage.paper import PaperLedger,encode
from arbitrage.paper_funding_watch import FundingWatch,selection,run
from arbitrage.settlement_marks import load_position
import test_paper_funding as funding_tests
import test_settlement_marks as mark_tests
from test_economics import NOW,HOUR
from test_paper import later


class FundingWatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=funding_tests.PaperFundingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path=self.fixture.path
        self.prices=self.path.parent/'marks.json'
        self.prices.write_text(encode(self.fixture.prices),encoding='utf-8')
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("UPDATE paper_meta SET mode='live_snapshot'")
            db.execute("UPDATE paper_positions SET payload=json_set(payload,'$.mode','live_snapshot')")

    def worker(self,**kwargs):
        return FundingWatch(self.path,self.fixture.market,self.fixture.registry,prices=self.prices,**kwargs)

    def duplicate(self,index):
        row=deepcopy(self.fixture.position)
        row.update(id='copy-'+str(index),mode='live_snapshot')
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT INTO paper_positions VALUES (?,?,?,?,?,?,?)',
                (row['id'],'pair-'+str(index),'closed','1','episode-'+str(index),'hash-'+str(index),encode(row)))

    async def test_offline_cycle_and_repeat_are_idempotent(self):
        worker=self.worker()
        session=mark_tests.Session([])
        first=await worker.tick(session)
        self.assertEqual(first['state'],'ready')
        self.assertEqual(first['requests'],0)
        self.assertFalse(first['network_enabled'])
        second=await worker.tick(session)
        self.assertEqual(second['positions'][0]['state'],'covered_paper_cashflows')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM paper_events WHERE kind='funding_reconciled'").fetchone()[0],1)

    async def test_closed_queue_rotates_and_replay_or_absent_is_not_written(self):
        for index in range(11): self.duplicate(index)
        first=selection(self.path,0)
        second=selection(self.path,first['cursor'])
        third=selection(self.path,second['cursor'])
        self.assertEqual([len(x['ids']) for x in (first,second,third)],[5,5,2])
        self.assertTrue(set(first['ids']).isdisjoint(second['ids']))
        self.assertEqual(selection(self.path,third['cursor'])['ids'],first['ids'])
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("UPDATE paper_positions SET state='open',payload=json_set(payload,'$.state','open')")
        capped=selection(self.path,0)
        self.assertEqual(len(capped['ids']),10)
        self.assertEqual(capped['omitted_open'],2)
        absent=self.path.parent/'absent.sqlite3'
        self.assertEqual(selection(absent,0)['state'],'absent')
        self.assertFalse(absent.exists())
        with closing(sqlite3.connect(self.path)) as db,db: db.execute("UPDATE paper_meta SET mode='replay'")
        before=self.path.read_bytes()
        self.assertEqual((await self.worker().tick(mark_tests.Session([])))['state'],'idle')
        self.assertEqual(before,self.path.read_bytes())

    async def test_bad_manual_file_and_missing_archive_never_become_zero(self):
        self.prices.write_text('{',encoding='utf-8')
        before=self.path.read_bytes()
        self.assertEqual((await self.worker().tick(mark_tests.Session([])))['state'],'input_error')
        self.assertEqual(before,self.path.read_bytes())
        self.prices.write_text('[]',encoding='utf-8')
        worker=FundingWatch(self.path,self.path.parent/'absent-market.sqlite3',self.fixture.registry)
        result=await worker.tick(mark_tests.Session([]))
        self.assertEqual(result['positions'][0]['state'],'partial_history')
        self.assertIsNone(PaperLedger(self.path).report()['net_pnl_usdt'])
        self.assertFalse(worker.market.exists())

    async def test_network_budget_is_shared_by_all_positions(self):
        for index in range(4): self.duplicate(index)
        budgets=[]
        async def download(position,market,registry,prices,**kwargs):
            budget=kwargs['max_requests'];budgets.append(budget)
            return dict(prices=prices,report=dict(requests=int(budget>0),retry_after_seconds=0,downloads=[]))
        with patch('arbitrage.paper_funding_watch.fetch_position_prices',side_effect=download):
            report=await self.worker(fetch=True).tick(mark_tests.Session([]))
        self.assertEqual(budgets,[3,2,1,0,0])
        self.assertEqual(report['requests'],3)

    async def test_rate_limit_cooldown_survives_recreated_worker(self):
        self.duplicate(0)
        budgets=[]
        async def download(position,market,registry,prices,**kwargs):
            budget=kwargs['max_requests'];budgets.append(budget)
            return dict(prices=prices,report=dict(requests=int(budget>0),retry_after_seconds=300 if budget else 0,
                downloads=[dict(error='settlement_http_429')] if budget else []))
        with patch('arbitrage.paper_funding_watch.fetch_position_prices',side_effect=download):
            first=await self.worker(fetch=True).tick(mark_tests.Session([]))
            again=self.worker(fetch=True,next_network_ms=first['next_network_ms'])
            second=await again.tick(mark_tests.Session([]))
        self.assertEqual(budgets,[3,0,0,0])
        self.assertEqual(second['requests'],0)
        self.assertGreater(first['next_network_ms'],int(time.time()*1000)+290000)

    async def test_one_bad_position_does_not_stop_remaining_positions(self):
        self.duplicate(0)
        calls=0
        async def download(position,market,registry,prices,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1: raise ValueError('injected bad input')
            return dict(prices=prices,report=dict(requests=0,retry_after_seconds=0,downloads=[]))
        with patch('arbitrage.paper_funding_watch.fetch_position_prices',side_effect=download):
            report=await self.worker().tick(mark_tests.Session([]))
        self.assertEqual([row['state'] for row in report['positions']],['blocked','covered_paper_cashflows'])
        self.assertEqual(report['state'],'partial')

    async def test_status_checkpoint_restores_cooldown_on_next_run(self):
        status=self.path.parent/'funding-status.json'
        args=SimpleNamespace(database=self.path,market_database=self.fixture.market,registry=None,status=status,
            funding_prices=self.prices,fetch_prices=False,interval=60,watch=False)
        with patch('arbitrage.paper_funding_watch.Registry.load',return_value=self.fixture.registry),redirect_stdout(StringIO()):
            await run(args)
        checkpoint=json.loads(status.read_text(encoding='utf-8'))
        self.assertGreater(checkpoint['cursor'],0)
        checkpoint['next_network_ms']=int(time.time()*1000)+300000
        status.write_text(encode(checkpoint),encoding='utf-8')
        args.fetch_prices=True
        budgets=[]
        async def download(position,market,registry,prices,**kwargs):
            budgets.append(kwargs['max_requests'])
            return dict(prices=prices,report=dict(requests=0,retry_after_seconds=0,downloads=[]))
        with patch('arbitrage.paper_funding_watch.Registry.load',return_value=self.fixture.registry),patch('arbitrage.paper_funding_watch.fetch_position_prices',side_effect=download),redirect_stdout(StringIO()):
            await run(args)
        self.assertEqual(budgets,[0])
        self.assertEqual(json.loads(status.read_text(encoding='utf-8'))['next_network_ms'],checkpoint['next_network_ms'])


class FundingCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setup=mark_tests.PositionDownloadTests()
        await setup.asyncSetUp()
        self.addCleanup(setup.fixture.doCleanups)
        self.setup=setup
        self.fixture=setup.fixture
        self.path=self.fixture.path
        self.prices=self.path.parent/'other-leg.json'
        self.prices.write_text(encode(self.fixture.prices[1:]),encoding='utf-8')
        position=deepcopy(setup.position)
        position.update(mode='live_snapshot',state='open')
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("UPDATE paper_meta SET mode='live_snapshot'")
            db.execute("UPDATE paper_positions SET state='open',payload=?",(encode(position),))

    def worker(self,**kwargs):
        return FundingWatch(self.path,self.fixture.market,self.setup.registry,prices=self.prices,**kwargs)

    async def test_persistent_cache_survives_mark_clearing_funding(self):
        first=await self.worker(fetch=True).tick(mark_tests.Session([mark_tests.Response([mark_tests.row(NOW+8*HOUR)])]))
        self.assertEqual(first['requests'],1)
        snapshot=later(self.fixture.snapshot,10*HOUR)
        leg=snapshot['economics']['inputs'][0]
        leg.update(exchange='binanceusdm',native_id='BTCUSDT')
        leg['market']['id']='BTCUSDT'
        position=PaperLedger(self.path).value(snapshot,self.fixture.position['id'],self.setup.registry,now_ms=NOW+10*HOUR)
        self.assertNotIn('funding',position)
        loaded=load_position(self.path,position['id'])
        self.assertEqual(len(loaded['_settlement_cache']),1)
        with closing(sqlite3.connect(self.fixture.market)) as db,db:
            db.execute("UPDATE funding_history_imports SET end_ms=?,payload=json_set(payload,'$.end_ms',?)",(NOW+10*HOUR,NOW+10*HOUR))
        second=await self.worker(fetch=True).tick(mark_tests.Session([]))
        self.assertEqual(second['requests'],0)
        self.assertEqual(second['positions'][0]['download']['cached'],1)
        self.assertEqual(second['positions'][0]['state'],'covered_paper_cashflows')

    async def test_cache_write_and_funding_audit_roll_back_together(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("CREATE TRIGGER fail_funding_cache BEFORE INSERT ON paper_events BEGIN SELECT RAISE(ABORT,'injected'); END")
        result=await self.worker(fetch=True).tick(mark_tests.Session([mark_tests.Response([mark_tests.row(NOW+8*HOUR)])]))
        self.assertEqual(result['positions'][0]['state'],'blocked')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM paper_settlement_prices').fetchone()[0],0)
        self.assertIsNone(PaperLedger(self.path).report()['net_pnl_usdt'])
