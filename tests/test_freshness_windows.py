from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from validate_freshness_windows import summarize


class FreshnessWindowsTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'market.sqlite3'
        self.report=dict(started_at_ms=100000,warmup_seconds=60,finished=True,finished_at_ms=300000,
            exchanges=dict(test=dict(max_selected_markets=20),missing=dict(max_selected_markets=0)))
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('CREATE TABLE snapshots(id INTEGER PRIMARY KEY,collected_at_ms INTEGER,exchange TEXT,payload TEXT)')

    def save(self,stamp,state,selected,fresh,exchange='test'):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT INTO snapshots(collected_at_ms,exchange,payload) VALUES (?,?,?)',
                (stamp,exchange,json.dumps(dict(status=dict(state=state,markets=selected,usable_quotes=fresh)))))

    def test_loading_and_disconnection_are_not_hidden_by_selected_denominator(self):
        self.save(161000,'loading_markets',0,0)
        self.save(162000,'reconnecting',20,0)
        self.save(163000,'streaming',20,20)
        self.save(164000,'stopped',20,0)
        row=summarize(self.path,self.report)['exchanges']['test']['windows'][0]
        self.assertEqual(row['samples'],3)
        self.assertEqual(row['fresh_fraction_of_selected'],.5)
        self.assertAlmostEqual(row['fresh_fraction_of_peak_capacity'],1/3)
        self.assertAlmostEqual(row['streaming_fraction'],1/3)

    def test_warmup_window_boundaries_and_no_data_remain_explicit(self):
        self.save(159999,'streaming',20,20)
        self.save(160000,'streaming',20,20)
        self.save(190000,'streaming',20,10)
        self.save(160000,'loading_markets',0,0,'missing')
        result=summarize(self.path,self.report,30)['exchanges']
        self.assertEqual([row['start_seconds'] for row in result['test']['windows']],[60,90])
        self.assertEqual(result['test']['min_window_fresh_fraction_of_peak_capacity'],.5)
        self.assertIsNone(result['missing']['windows'][0]['fresh_fraction_of_peak_capacity'])
        fractional=summarize(self.path,dict(self.report,warmup_seconds=60.5),30)['exchanges']['test']
        self.assertEqual([row['start_seconds'] for row in fractional['windows']],[90])

    def test_missing_source_is_not_created_and_inconsistent_peak_is_rejected(self):
        missing=self.path.with_name('absent.sqlite3')
        with self.assertRaisesRegex(ValueError,'source_database_missing'):summarize(missing,self.report)
        self.assertFalse(missing.exists())
        self.save(161000,'streaming',21,20)
        with self.assertRaisesRegex(ValueError,'report_peak'):summarize(self.path,self.report)

    def test_partial_last_minute_does_not_define_worst_complete_window(self):
        self.report['finished_at_ms']=230000
        self.save(161000,'streaming',20,20)
        self.save(225000,'reconnecting',20,0)
        value=summarize(self.path,self.report)['exchanges']['test']
        self.assertEqual(value['min_window_fresh_fraction_of_peak_capacity'],0)
        self.assertEqual(value['min_complete_window_fresh_fraction_of_peak_capacity'],1)
        self.assertEqual([row['complete_time_window'] for row in value['windows']],[True,False])
