from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from arbitrage.funding_comparison import compare
from arbitrage.ui import Dashboard
from test_ui import sample, NOW

HOUR = 3600000
END = 40*86400000
LEGS = dict(long=dict(exchange='alpha', native_id='BTC'), short=dict(exchange='beta', native_id='BTC'))


def events(db, missing=None):
    db.execute('CREATE TABLE funding_history_events(exchange TEXT,native_id TEXT,timestamp_ms INTEGER,rate TEXT)')
    for side, interval, rate in (('long', HOUR, '0.001'), ('short', 4*HOUR, '-0.0002')):
        for stamp in range(END-86400000, END+1, interval):
            if (side, stamp) != missing:
                db.execute('INSERT INTO funding_history_events VALUES (?,?,?,?)', (LEGS[side]['exchange'], 'BTC', stamp, rate))


class FundingComparisonTests(unittest.TestCase):
    def test_different_cadences_shared_window_signed_totals_and_missing_event_is_not_zero(self):
        with closing(sqlite3.connect(':memory:')) as db:
            events(db)
            db.execute("INSERT INTO funding_history_events VALUES ('beta','BTC',?,'99')", (END+HOUR,))
            db.execute("INSERT INTO funding_history_events VALUES ('alpha','OTHER',?,'99')", (END,))
            value = compare(db, LEGS, END, 1)
            self.assertTrue(all(v['covered'] for v in value['coverage'].values()))
            self.assertEqual(value['coverage']['long']['events'], 24)
            self.assertEqual(value['coverage']['short']['events'], 6)
            self.assertEqual(value['totals_pct'], dict(long=2.4, short=-.12))
            self.assertEqual(value['covered_net_pct'], -2.52)
            self.assertIsNone(value['rows'][0]['short_rate_pct'])
            self.assertIsNone(value['rows'][0]['short_sum_pct'])
            self.assertEqual(value['rows'][-1]['observed_net_pct'], -2.52)

    def test_gap_or_import_error_cannot_be_presented_as_complete_window(self):
        with closing(sqlite3.connect(':memory:')) as db:
            events(db, ('long', END-5*HOUR))
            value = compare(db, LEGS, END, 1)
            self.assertIsNone(value['covered_net_pct'])
            self.assertIsNotNone(value['observed_net_pct'])
            self.assertFalse(value['coverage']['long']['covered'])
            db.execute('CREATE TABLE funding_history_imports(exchange TEXT,native_id TEXT,payload TEXT)')
            db.execute('INSERT INTO funding_history_imports VALUES (?,?,?)', ('beta', 'BTC', json.dumps(dict(error='incomplete import'))))
            self.assertFalse(compare(db, LEGS, END, 1)['coverage']['short']['covered'])

    def test_absent_history_is_unknown_while_explicit_zero_remains_zero(self):
        with closing(sqlite3.connect(':memory:')) as db:
            value = compare(db, LEGS, END, 7)
            self.assertEqual(value['rows'], [])
            self.assertEqual(value['totals_pct'], dict(long=None, short=None))
            self.assertIsNone(value['covered_net_pct'])
            events(db)
            db.execute("DELETE FROM funding_history_events WHERE exchange='beta'")
            db.execute("UPDATE funding_history_events SET rate='0'")
            value = compare(db, LEGS, END, 1)
            self.assertEqual(value['totals_pct']['long'], 0)
            self.assertIsNone(value['observed_net_pct'])

    def test_event_limit_is_explicit_and_keeps_latest_events_without_coverage_claim(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE funding_history_events(exchange TEXT,native_id TEXT,timestamp_ms INTEGER,rate TEXT)')
            db.executemany("INSERT INTO funding_history_events VALUES ('alpha','BTC',?,'0.001')", [(END-i*60000,) for i in range(1002)])
            value = compare(db, LEGS, END, 1)
            self.assertTrue(value['truncated'])
            self.assertEqual(len(value['rows']), 1000)
            self.assertEqual(value['rows'][-1]['time'], END)
            self.assertFalse(value['coverage']['long']['covered'])

    def test_leg_change_selects_actual_other_pair_without_reusing_economics(self):
        document, registry = sample()
        with TemporaryDirectory() as directory:
            path = Path(directory)/'latest.json'
            path.write_text(json.dumps(document), encoding='utf-8')
            store = Dashboard(path, Path(directory)/'absent.sqlite3', registry, lambda: NOW)
            first = store.pair(document['scanner']['rows'][0]['id'], 15)
            self.assertEqual(len(first['alternatives']), 2)
            other = next(r for r in first['alternatives'] if r['id'] != first['item']['id'])
            second = store.pair(other['id'], 15)
            self.assertIsNone(second['item']['economics'])
            self.assertFalse(second['item']['confirmed_current'])
            self.assertEqual(second['item']['legs']['long']['exchange'], first['item']['legs']['short']['exchange'])
            with self.assertRaises(ValueError):
                store.pair(other['id'], 15, 8)
