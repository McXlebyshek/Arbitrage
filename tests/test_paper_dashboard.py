from contextlib import closing
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from aiohttp.test_utils import TestClient, TestServer
from arbitrage.paper import PaperLedger, encode
from arbitrage.paper_dashboard import read_dashboard
from arbitrage.ui import create_app
from test_paper import document, later
from test_economics import NOW


class PaperDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'paper.sqlite3'
        self.snapshot, self.registry = document()
        self.ledger = PaperLedger(self.path)
        self.position = self.ledger.open(self.snapshot, 'test', self.registry, now_ms=NOW)

    def duplicate(self, index, state='closed', net=None):
        row = deepcopy(self.position)
        row.update(id=f'position-{index}',state=state)
        row['valuation'].update(net_pnl_usdt=net,pnl_before_funding_usdt='2')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO paper_positions VALUES (?,?,?,?,?,?,?)',
                (row['id'], f'pair-{index}', state, '1', f'episode-{index}', f'hash-{index}', encode(row)))

    def test_projection_freshness_and_read_only_source(self):
        before = self.path.read_bytes()
        result = read_dashboard(self.path, now_ms=NOW)
        row = result['rows'][0]
        self.assertTrue(row['valuation_fresh'])
        self.assertEqual(row['fresh_until_ms'], NOW+3000)
        self.assertNotIn('entry_evidence',row)
        self.assertNotIn('inputs',encode(row))
        self.assertEqual(result['summary']['open_count'],1)
        self.assertFalse(read_dashboard(self.path,now_ms=NOW+4000)['rows'][0]['valuation_fresh'])
        self.assertEqual(before,self.path.read_bytes())

    def test_filtered_pages_and_total_summary_are_consistent(self):
        for index in range(8): self.duplicate(index)
        result = read_dashboard(self.path,'closed',3,0,NOW)
        self.assertEqual(len(result['rows']),3)
        self.assertEqual(result['total_filtered'],8)
        self.assertEqual(result['summary']['closed_count'],8)
        self.assertEqual(result['summary']['open_count'],1)
        self.assertTrue(result['has_next'])
        second = read_dashboard(self.path,'closed',3,3,NOW)
        self.assertTrue(set(row['id'] for row in result['rows']).isdisjoint(row['id'] for row in second['rows']))
        self.assertEqual(read_dashboard(self.path,'closed',3,999,NOW)['offset'],0)

    def test_unknown_net_does_not_become_zero_or_partial_total(self):
        self.duplicate(0,net='3')
        self.duplicate(1,net='-1')
        self.duplicate(2,net='0')
        self.duplicate(3)
        result = read_dashboard(self.path,'closed',now_ms=NOW)['summary']
        self.assertIsNone(result['closed_net_usdt'])
        self.assertEqual(result['closed_before_funding_usdt'],'8')
        self.assertEqual(result['covered_closed_count'],3)
        self.assertEqual([result[x] for x in ('covered_wins','covered_losses','covered_flat')],[1,1,1])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM paper_positions WHERE id='position-3'")
        self.assertEqual(read_dashboard(self.path,'closed',now_ms=NOW)['summary']['closed_net_usdt'],'2')

    def test_missing_and_legacy_sources_do_not_create_or_modify_files(self):
        missing = Path(self.temp.name)/'absent.sqlite3'
        self.assertEqual(read_dashboard(missing)['source'],'absent')
        self.assertFalse(missing.exists())
        with closing(sqlite3.connect(missing)) as db: db.execute('CREATE TABLE unrelated(value)')
        before = missing.read_bytes()
        self.assertEqual(read_dashboard(missing)['source'],'unsupported')
        self.assertEqual(before,missing.read_bytes())

    def test_replay_never_looks_fresh_and_closed_is_history(self):
        replay = Path(self.temp.name)/'replay.sqlite3'
        PaperLedger(replay,True).open(self.snapshot,'test',self.registry)
        result = read_dashboard(replay,now_ms=NOW)
        self.assertEqual(result['mode'],'replay')
        self.assertFalse(result['rows'][0]['valuation_fresh'])
        self.ledger.value(later(self.snapshot),self.position['id'],self.registry,close=True,now_ms=NOW+1000)
        self.assertFalse(read_dashboard(self.path,'closed',now_ms=NOW+1000)['rows'][0]['valuation_fresh'])

    def test_invalid_pages_rejected(self):
        for args in (dict(state='unknown'),dict(limit=0),dict(limit=True),dict(limit=101),dict(offset=-1),dict(offset=1000001)):
            with self.assertRaises(ValueError): read_dashboard(self.path,**args)


class PaperDashboardHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_read_only_default_custom_path_and_invalid_parameters(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            snapshot,registry=document()
            path=root/'latest.json'
            path.write_text(encode(snapshot),encoding='utf-8')
            custom=root/'custom.sqlite3'
            PaperLedger(custom).open(snapshot,'test',registry,now_ms=NOW)
            before=custom.read_bytes()
            async with TestClient(TestServer(create_app(path,root/'market.sqlite3',registry,custom))) as client:
                response=await client.get('/api/paper')
                self.assertEqual(response.status,200)
                self.assertEqual((await response.json())['summary']['open_count'],1)
                self.assertEqual((await client.post('/api/paper')).status,405)
                for query in ('state=bad','limit=0','limit=101','offset=-1','offset=1000001','limit=x'):
                    self.assertEqual((await client.get('/api/paper?'+query)).status,400)
                self.assertEqual((await client.get('/assets/paper-panel.js')).status,200)
                self.assertEqual((await client.get('/assets/paper-panel.css')).status,200)
            self.assertEqual(before,custom.read_bytes())
            async with TestClient(TestServer(create_app(path,root/'market.sqlite3',registry))) as client:
                self.assertEqual((await (await client.get('/api/paper')).json())['source'],'absent')
            self.assertFalse((root/'paper.sqlite3').exists())

    async def test_corrupt_ledger_returns_unavailable(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'paper.sqlite3').write_text('not sqlite',encoding='utf-8')
            async with TestClient(TestServer(create_app(root/'latest.json',root/'market.sqlite3'))) as client:
                self.assertEqual((await client.get('/api/paper')).status,503)
