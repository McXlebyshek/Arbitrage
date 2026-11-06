from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from arbitrage.ui import Dashboard
from arbitrage.chart_history import rollup
from test_chart_history import seed
from test_ui import sample, NOW


class ChartNavigationTests(unittest.TestCase):
    def test_past_window_does_not_move_current_card_or_funding_comparison(self):
        document, registry = sample()
        with TemporaryDirectory() as directory:
            path, database = Path(directory)/'latest.json', Path(directory)/'history.sqlite3'
            path.write_text(json.dumps(document), encoding='utf-8')
            stamps = [NOW-i*60000 for i in reversed(range(5))]
            seed(database, document, stamps)
            before = database.read_bytes()
            store = Dashboard(path, database, registry, lambda: NOW)
            value = store.pair(document['scanner']['rows'][0]['id'], 1, 7, NOW-60000)
            self.assertEqual([p['time'] for p in value['points']], [NOW-120000, NOW-60000])
            self.assertEqual(value['chart_range']['end_ms'], NOW-60000)
            self.assertEqual(value['chart_range']['latest_ms'], NOW)
            self.assertEqual(value['funding_comparison']['end_inclusive_ms'], NOW)
            self.assertTrue(value['item']['confirmed_current'])
            self.assertEqual(before, database.read_bytes())
            future = store.pair(document['scanner']['rows'][0]['id'], 1, 7, NOW+100000)
            self.assertEqual(future['chart_range']['end_ms'], NOW)

    def test_minute_history_and_funding_events_respect_historical_end(self):
        document, registry = sample()
        with TemporaryDirectory() as directory:
            path, database = Path(directory)/'latest.json', Path(directory)/'history.sqlite3'
            path.write_text(json.dumps(document), encoding='utf-8')
            seed(database, document, [NOW-3600000, NOW-60000, NOW])
            leg = document['scanner']['rows'][0]
            with closing(sqlite3.connect(database)) as db, db:
                rollup(db, None)
                db.execute('CREATE TABLE funding_history_events(exchange TEXT,native_id TEXT,timestamp_ms INTEGER,rate TEXT)')
                for stamp in (NOW-86400000, NOW-60000, NOW):
                    db.execute('INSERT INTO funding_history_events VALUES (?,?,?,?)', (leg['long_exchange'],leg['long_symbol'],stamp,'0.001'))
            store = Dashboard(path, database, registry, lambda: NOW)
            value = store.pair(leg['id'], 60, 7, NOW-60000)
            self.assertEqual(value['history_source'], 'minute')
            self.assertTrue(all(p['time']<=NOW-60000 for p in value['points']))
            self.assertTrue(all(p['time']<=NOW-60000 for s in value['funding_series'] for p in s['rows']))
            self.assertEqual(value['funding_range']['earliest_ms'], NOW-86400000)

    def test_invalid_navigation_time_is_rejected_without_database_creation(self):
        document, registry = sample()
        with TemporaryDirectory() as directory:
            path, database = Path(directory)/'latest.json', Path(directory)/'absent.sqlite3'
            path.write_text(json.dumps(document), encoding='utf-8')
            store = Dashboard(path, database, registry, lambda: NOW)
            for stamp in (-1, True, 1.5, 8640000000000001):
                with self.assertRaises(ValueError):
                    store.pair(document['scanner']['rows'][0]['id'], 1, 7, stamp)
            self.assertFalse(database.exists())

    def test_funding_graph_limit_keeps_latest_events_in_time_order_and_marks_truncation(self):
        document, registry = sample()
        with TemporaryDirectory() as directory:
            path, database = Path(directory)/'latest.json', Path(directory)/'history.sqlite3'
            path.write_text(json.dumps(document), encoding='utf-8')
            seed(database, document, [NOW])
            leg = document['scanner']['rows'][0]
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE funding_history_events(exchange TEXT,native_id TEXT,timestamp_ms INTEGER,rate TEXT)')
                db.executemany('INSERT INTO funding_history_events VALUES (?,?,?,?)',
                    [(leg['long_exchange'],leg['long_symbol'],NOW-i*60000,'0.001') for i in range(1002)])
            value = Dashboard(path, database, registry, lambda: NOW).pair(leg['id'],15)
            points = value['funding_series'][0]
            self.assertTrue(points['truncated'])
            self.assertEqual(len(points['rows']),1000)
            self.assertEqual(points['rows'][0]['time'],NOW-999*60000)
            self.assertEqual(points['rows'][-1]['time'],NOW)
