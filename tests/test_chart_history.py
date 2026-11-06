from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from arbitrage.chart_history import MINUTE, pending, read, rollup
from arbitrage.storage import maintain
from arbitrage.autocare import CarePolicy, prune_cycle, read_archive
from arbitrage.ui import Dashboard
from test_ui import NOW, sample


def seed(database, document, stamps):
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('CREATE TABLE scans(id INTEGER PRIMARY KEY,generated_at_ms INTEGER,payload TEXT)')
        db.execute('CREATE INDEX scans_time ON scans(generated_at_ms)')
        for stamp in stamps:
            value = dict(document['scanner'], generated_at_ms=stamp)
            db.execute('INSERT INTO scans(generated_at_ms,payload) VALUES (?,?)', (stamp, json.dumps(value)))


class ChartHistoryTests(unittest.TestCase):
    def test_minute_extremes_last_missing_reverse_and_idempotent_checkpoint(self):
        document, _ = sample()
        row = document['scanner']['rows'][0]
        with closing(sqlite3.connect(':memory:')) as db, db:
            db.execute('CREATE TABLE scans(id INTEGER PRIMARY KEY,generated_at_ms INTEGER,payload TEXT)')
            for stamp, entry, reverse in ((120100, 2, True), (121000, 7, True), (123000, -1, False)):
                scan = deepcopy(document['scanner'])
                scan['rows'][0]['open_spread_pct'] = entry
                if not reverse:
                    scan['rows'] = scan['rows'][:1]
                db.execute('INSERT INTO scans(generated_at_ms,payload) VALUES (?,?)', (stamp, json.dumps(scan)))
            self.assertEqual(rollup(db, 2)['scans'], 2)
            self.assertEqual(pending(db)['scans'], 1)
            rollup(db)
            point = read(db, 'pair:'+row['id'], 120000, 180000)[0]
            self.assertEqual(point['samples'], 3)
            self.assertEqual(point['entry'], -1)
            self.assertEqual(point['ranges']['entry'], dict(min=-1, max=7))
            self.assertIsNone(point['exit'])
            self.assertEqual(point['long_price'], row['long_ask_usdt_per_unit'])
            self.assertEqual(rollup(db), dict(scans=0, economics=0))
            self.assertEqual(read(db, 'pair:'+row['id'], 120000, 180000), [point])
            self.assertEqual(read(db, 'pair:'+row['id'], 120000, 122000), [])

    def test_checkpoint_and_buckets_rollback_together(self):
        document, _ = sample()
        with closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE scans(id INTEGER PRIMARY KEY,generated_at_ms INTEGER,payload TEXT)')
            db.execute('INSERT INTO scans VALUES (1,?,?)', (NOW, json.dumps(document['scanner'])))
            db.execute("INSERT INTO scans VALUES (2,?,'broken')", (NOW+1000,))
            db.commit()
            with self.assertRaises(ValueError), db:
                rollup(db)
            self.assertEqual(db.execute('SELECT count(*) FROM chart_rollups').fetchone()[0], 0)
            self.assertEqual(pending(db)['scans'], 2)

    def test_day_history_exceeds_300_and_uses_index_without_modifying_database(self):
        document, registry = sample()
        end = NOW//MINUTE*MINUTE
        document['generated_at_ms'] = end
        with TemporaryDirectory() as directory:
            database, path = Path(directory)/'day.sqlite3', Path(directory)/'latest.json'
            # Controlled 24h fixture with an intentional missing minute.
            stamps = [end-i*MINUTE for i in range(1440) if i != 600]
            seed(database, document, reversed(stamps))
            with closing(sqlite3.connect(database)) as db, db:
                rollup(db, None)
                plan = str(db.execute('EXPLAIN QUERY PLAN SELECT payload FROM chart_rollups WHERE series=? AND start_ms>=? AND start_ms<=?', ('pair:'+document['scanner']['rows'][0]['id'], end-86400000, end)).fetchall())
                self.assertIn('INDEX', plan)
            path.write_text(json.dumps(document), encoding='utf-8')
            before = database.read_bytes()
            detail = Dashboard(path, database, registry, lambda: end).pair(document['scanner']['rows'][0]['id'], 1440)
            self.assertEqual(detail['history_source'], 'minute')
            self.assertEqual(len(detail['points']), 1439)
            self.assertFalse(detail['truncated'])
            self.assertNotIn(end-600*MINUTE, [p['time'] for p in detail['points']])
            self.assertEqual(detail['points'][0]['time'], stamps[-1])
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(len(Dashboard(path, database, registry, lambda: end).pair(document['scanner']['rows'][0]['id'], 1)['points']), 2)

    def test_legacy_raw_fallback_is_explicit_and_backfill_survives_pruning(self):
        document, registry = sample()
        with TemporaryDirectory() as directory:
            database, path = Path(directory)/'legacy.sqlite3', Path(directory)/'latest.json'
            seed(database, document, range(NOW-400000, NOW+1, 1000))
            path.write_text(json.dumps(document), encoding='utf-8')
            store = Dashboard(path, database, registry, lambda: NOW)
            old = store.pair(document['scanner']['rows'][0]['id'], 15)
            self.assertTrue(old['truncated'])
            self.assertEqual(old['history_source'], 'raw')
            maintain(database, NOW+1000, apply=True)
            detail = store.pair(document['scanner']['rows'][0]['id'], 15)
            self.assertEqual(detail['history_source'], 'minute')
            self.assertGreater(len(detail['points']), 5)
            self.assertEqual(sum(p['samples'] for p in detail['points']), 401)

    def test_funding_own_intervals_and_stale_unknown_are_not_zero(self):
        with closing(sqlite3.connect(':memory:')) as db, db:
            db.execute('CREATE TABLE economics(id INTEGER PRIMARY KEY,generated_at_ms INTEGER,payload TEXT)')
            for stamp, received, rate, interval in ((120000, 120000, .001, 3600000), (121000, 121000, .002, 14400000), (300000, 1000, .5, 3600000), (360000, 360000, None, 3600000)):
                doc = dict(funding_observations=[dict(exchange='alpha', native_id='BTC', received_at_ms=received, rate=rate, interval_ms=interval)])
                db.execute('INSERT INTO economics(generated_at_ms,payload) VALUES (?,?)', (stamp, json.dumps(doc)))
            rollup(db, None)
            points = read(db, 'fund:alpha:BTC', 0, 500000)
            self.assertEqual(len(points), 1)
            self.assertEqual(points[0]['value'], .2)
            self.assertEqual(points[0]['interval_ms'], 14400000)

    def test_autocare_waits_for_chart_backfill_and_archives_expired_buckets(self):
        document, _ = sample()
        with TemporaryDirectory() as directory:
            database = Path(directory)/'history.sqlite3'
            seed(database, document, [1000, 62000, 86400000*40])
            policy = CarePolicy()
            self.assertEqual(prune_cycle(database, policy, now=86400000*41)['state'], 'aggregation_pending')
            with closing(sqlite3.connect(database)) as db, db:
                rollup(db, None)
            result = prune_cycle(database, policy, now=86400000*41)
            self.assertEqual(result['state'], 'pruned')
            self.assertEqual(result['deleted_rows']['chart_rollups'], 4)
            archived = read_archive(result['backup'], 'chart_rollups')
            self.assertEqual(len(archived['rows']), 4)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM chart_rollups').fetchone()[0], 2)

