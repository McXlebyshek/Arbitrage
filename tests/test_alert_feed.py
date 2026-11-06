from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from arbitrage.alerts import persist,read_feed
from test_alerts import confirmed,NOW


def insert(db,index):
    _,value=confirmed(NOW+index*70000)
    persist(db,value,now_ms=NOW+index*70000+2000)


class AlertFeedTests(unittest.TestCase):
    def test_unread_and_two_way_pagination_are_consistent_and_read_only(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'feed.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                for index in range(6):insert(db,index)
            before=path.read_bytes()
            latest=read_feed(path,2,read_through=2)
            self.assertEqual([x['id'] for x in latest['rows']],[5,6])
            self.assertEqual(latest['unread_count'],4)
            self.assertEqual(latest['latest_id'],6)
            self.assertTrue(latest['has_older'])
            older=read_feed(path,2,before_id=5,read_through=2)
            self.assertEqual([x['id'] for x in older['rows']],[3,4])
            newer=read_feed(path,2,after_id=2)
            self.assertEqual([x['id'] for x in newer['rows']],[3,4])
            self.assertTrue(newer['has_newer'])
            self.assertEqual(read_feed(path,read_through=6)['unread_count'],0)
            self.assertEqual(path.read_bytes(),before)

    def test_suppressed_records_are_not_unread_or_pagination_entries(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'feed.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                insert(db,0)
                _,value=confirmed(NOW+10000)
                persist(db,value,now_ms=NOW+12000)
            feed=read_feed(path)
            self.assertEqual(len(feed['rows']),1)
            self.assertEqual(feed['unread_count'],1)
            self.assertEqual(feed['latest_id'],1)
            self.assertFalse(feed['has_newer'])

    def test_scope_is_stable_after_write_and_changes_when_file_is_replaced(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'feed.sqlite3'
            missing=read_feed(path)['scope']
            with closing(sqlite3.connect(path)) as db,db:insert(db,0)
            first=read_feed(path)['scope']
            self.assertNotEqual(missing,first)
            with closing(sqlite3.connect(path)) as db,db:insert(db,1)
            self.assertEqual(read_feed(path)['scope'],first)
            path.rename(Path(directory)/'previous.sqlite3')
            with closing(sqlite3.connect(path)) as db,db:insert(db,2)
            self.assertNotEqual(read_feed(path)['scope'],first)

    def test_empty_and_legacy_feeds_have_no_false_unread_and_do_not_create_files(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'absent.sqlite3'
            value=read_feed(path)
            self.assertEqual(value['unread_count'],0)
            self.assertEqual(value['rows'],[])
            self.assertFalse(path.exists())
            with closing(sqlite3.connect(path)) as db:db.execute('CREATE TABLE other(value)')
            self.assertEqual(read_feed(path)['rows'],[])

    def test_conflicting_and_negative_cursors_are_rejected(self):
        for values in (dict(after_id=1,before_id=2),dict(before_id=-1),dict(read_through=-1),dict(read_through=True),dict(after_id=2**63)):
            with self.assertRaises(ValueError):read_feed('missing.sqlite3',**values)
