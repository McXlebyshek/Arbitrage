import asyncio
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from arbitrage.economic_data import EconomicData
from arbitrage.paper import PaperLedger, encode
from arbitrage.paper_collection import read_positions
from arbitrage.paper_watch import check_status_path, tick
from test_economic_data import arguments
from test_economics import NOW
from test_paper import document, later


class PaperWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'paper.sqlite3'
        self.snapshot_path = Path(self.temp.name)/'latest.json'
        self.snapshot, self.registry = document()
        self.ledger = PaperLedger(self.path)
        self.position = self.ledger.open(self.snapshot, 'test', self.registry, now_ms=NOW)

    def write(self, snapshot):
        self.snapshot_path.write_text(encode(snapshot), encoding='utf-8')

    def test_reader_is_read_only_and_excludes_replay_missing_legacy_closed(self):
        before = self.path.read_bytes()
        result = read_positions(self.path, self.registry)
        self.assertEqual(result['positions'][0]['id'], self.position['id'])
        self.assertEqual(before, self.path.read_bytes())
        missing = Path(self.temp.name)/'absent.sqlite3'
        self.assertEqual(read_positions(missing, self.registry)['state'], 'absent')
        self.assertFalse(missing.exists())
        with closing(sqlite3.connect(missing)) as db: db.execute('CREATE TABLE other(value)')
        self.assertEqual(read_positions(missing, self.registry)['state'], 'unsupported_ledger')
        replay = Path(self.temp.name)/'replay.sqlite3'
        PaperLedger(replay, replay=True).open(self.snapshot, 'test', self.registry)
        self.assertEqual(read_positions(replay, self.registry)['positions'], [])
        self.ledger.value(later(self.snapshot), self.position['id'], self.registry, close=True, now_ms=NOW+1000)
        self.assertEqual(read_positions(self.path, self.registry)['open_count'], 0)

    def test_reader_rejects_disabled_exchange_and_changed_registry_identity(self):
        result = read_positions(self.path, self.registry, {'alpha'})
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['positions'], [])
        self.assertEqual(result['skipped'][0]['reason'], 'paper_exchange_not_enabled')
        registry = deepcopy(self.registry)
        registry.bindings[('alpha','BTC_USDT')]['price_units'] *= 1000
        self.assertEqual(read_positions(self.path, registry)['positions'], [])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE paper_positions SET payload='[]'")
        result = read_positions(self.path, self.registry)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['skipped'][0]['reason'], 'paper_position_payload_invalid')

    def test_reader_caps_positions_and_reports_omitted(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            for index in range(12):
                row = deepcopy(self.position)
                row['id'] = row['pair']['id'] = f'position-{index}'
                db.execute('INSERT INTO paper_positions VALUES (?,?,?,?,?,?,?)',
                    (row['id'], row['pair']['id'], 'open', '1', 'episode-'+str(index), 'hash-'+str(index), encode(row)))
        result = read_positions(self.path, self.registry)
        self.assertEqual(len(result['positions']), 10)
        self.assertEqual(result['open_count'], 13)
        self.assertEqual(result['omitted_by_limit'], 3)
        self.assertEqual(result['state'], 'partial')

    def test_tick_marks_fixed_position_once_and_restart_does_not_duplicate(self):
        self.write(later(self.snapshot))
        before = self.snapshot_path.read_bytes()
        result = tick(self.path, self.snapshot_path, self.registry, NOW+1000)
        self.assertEqual(result['positions'][0]['state'], 'marked')
        again = tick(self.path, self.snapshot_path, self.registry, NOW+2000)
        self.assertEqual(again['positions'][0]['state'], 'already_current')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM paper_events WHERE kind='marked'").fetchone()[0], 1)
        self.assertEqual(self.snapshot_path.read_bytes(), before)
        self.assertEqual(self.ledger.report()['open_count'], 1)

    def test_stale_duplicate_stopped_and_missing_inputs_do_not_change_position(self):
        self.write(self.snapshot)
        result = tick(self.path, self.snapshot_path, self.registry, NOW+4000)
        self.assertEqual(result['positions'][0]['state'], 'blocked')
        self.assertIn('depth_stale', result['positions'][0]['error'])
        for kind in ('stale','stopped','missing'):
            value = later(self.snapshot)
            now = NOW+1000
            if kind == 'stale': now += 10000
            if kind == 'stopped': value['economics']['state'] = 'stopped'
            if kind == 'missing': value['economics']['inputs'] = []
            self.write(value)
            result = tick(self.path, self.snapshot_path, self.registry, now)
            self.assertIn(result['state'], ('partial','snapshot_unavailable'))
        self.assertEqual(self.ledger.report()['positions'][0]['valuation']['timestamp_ms'], NOW)
        self.assertEqual(self.ledger.report()['open_count'], 1)

    def test_invalid_json_missing_snapshot_and_bad_ledger_are_reported(self):
        self.assertEqual(tick(self.path, self.snapshot_path, self.registry, NOW)['state'], 'snapshot_unavailable')
        self.snapshot_path.write_text('{', encoding='utf-8')
        self.assertEqual(tick(self.path, self.snapshot_path, self.registry, NOW)['state'], 'snapshot_unavailable')
        bad = Path(self.temp.name)/'bad.sqlite3'
        bad.write_text('not sqlite', encoding='utf-8')
        self.assertEqual(tick(bad, self.snapshot_path, self.registry, NOW)['state'], 'ledger_error')

    def test_idle_and_replay_watcher_never_create_or_modify_positions(self):
        missing = Path(self.temp.name)/'absent.sqlite3'
        self.assertEqual(tick(missing, self.snapshot_path, self.registry, NOW)['state'], 'idle')
        self.assertFalse(missing.exists())
        replay = Path(self.temp.name)/'replay.sqlite3'
        PaperLedger(replay, replay=True).open(self.snapshot, 'test', self.registry)
        before = replay.read_bytes()
        self.assertEqual(tick(replay, self.snapshot_path, self.registry, NOW)['state'], 'idle')
        self.assertEqual(replay.read_bytes(), before)

    def test_status_path_cannot_overwrite_source_or_unrelated_file(self):
        self.write(self.snapshot)
        with self.assertRaises(ValueError): check_status_path(self.snapshot_path, [self.path,self.snapshot_path])
        with self.assertRaises(ValueError): check_status_path(self.path, [])
        unrelated = Path(self.temp.name)/'unrelated.json'
        unrelated.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError): check_status_path(unrelated, [])
        unrelated.write_text('{"classification":"paper_watch_status"}', encoding='utf-8')
        check_status_path(unrelated, [self.path,self.snapshot_path])


class PaperCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_legs_remain_in_inputs_after_scanner_selection_disappears(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'paper.sqlite3'
            snapshot, registry = document()
            position = PaperLedger(path).open(snapshot, 'test', registry, now_ms=NOW)
            args = arguments(registry)
            args.paper_database = path
            engine = EconomicData(args, [])
            await engine.refresh_paper_pins()
            self.assertEqual(engine.selection(), {'alpha':{'BTC_USDT'}, 'beta':{'BTC_USDT'}})
            engine.state = 'ready'
            engine.legs = {(row['exchange'],row['native_id']):row for row in snapshot['economics']['inputs']}
            result = engine.snapshot({'rows':[]}, NOW)
            self.assertEqual(result['rows'], [])
            self.assertEqual(len(result['inputs']), 2)
            self.assertEqual(result['paper_collection']['open_count'], 1)
            PaperLedger(path).value(later(snapshot), position['id'], registry, close=True, now_ms=NOW+1000)
            await engine.refresh_paper_pins()
            self.assertEqual(engine.selection(), {})
            self.assertEqual(engine.snapshot({'rows':[]}, NOW)['inputs'], [])

    async def test_pool_funding_selection_includes_pins_outside_ws_subset(self):
        _, registry = document()
        engine = EconomicData(arguments(registry), [SimpleNamespace(exchange='alpha',selected={}), SimpleNamespace(exchange='beta',selected={})])
        snapshot, _ = document()
        engine.paper_pins = [snapshot['economics']['rows'][0]]
        self.assertEqual(engine.funding_selection(), {'alpha':{'BTC_USDT'}, 'beta':{'BTC_USDT'}})

    async def test_pin_error_grace_expires_and_cancellation_releases_loop(self):
        _, registry = document()
        engine = EconomicData(arguments(registry), [])
        calls = 0
        async def refresh():
            nonlocal calls
            calls += 1
            if calls == 1:
                engine.paper_pins = [dict(id='test')]
            else:
                raise sqlite3.OperationalError('locked')
        engine.refresh_paper_pins = refresh
        clock = iter([NOW, NOW+1000, NOW+1000, NOW+11000, NOW+11000])
        states = []
        async def pause(seconds):
            states.append((list(engine.paper_pins), dict(engine.paper_collection)))
            if len(states) == 3: raise asyncio.CancelledError()
        with patch('arbitrage.economic_data.now_ms', side_effect=lambda:next(clock)), patch('arbitrage.economic_data.asyncio.sleep', side_effect=pause):
            with self.assertRaises(asyncio.CancelledError): await engine.paper_pin_loop()
        self.assertTrue(states[1][1]['retained_during_grace'])
        self.assertEqual(states[2][0], [])

    async def test_run_with_only_pins_keeps_workers_and_shutdown_cancels_them(self):
        _, registry = document()
        engine = EconomicData(arguments(registry), [])
        snapshot, _ = document()
        engine.paper_pins = [snapshot['economics']['rows'][0]]
        started = asyncio.Event()
        stopped = []
        async def pin_loop(): await asyncio.Event().wait()
        async def books(exchange):
            started.set()
            try: await asyncio.Event().wait()
            finally: stopped.append(exchange)
        async def funding(exchange): await asyncio.Event().wait()
        engine.paper_pin_loop, engine.book_loop, engine.funding_loop = pin_loop, books, funding
        task = asyncio.create_task(engine.run())
        await asyncio.wait_for(started.wait(), 1)
        self.assertNotEqual(engine.state, 'awaiting_candidates')
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(engine.state, 'stopped')
        self.assertCountEqual(stopped, ['alpha','beta'])
