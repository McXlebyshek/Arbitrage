import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from market_monitor.history import History, MAINTENANCE_MS, MINUTE_MS, RECENT_MS, RETENTION_MS, eligible_legs


NOW = 1_800_000_000_000


def leg(key, stamp, **fields):
    return dict(dict(id=key, sourceAt=stamp - 100, receivedAt=stamp, bid=100, ask=101,
                     last=100.5, index=None, oi=None), **fields)


def legacy_file(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('''CREATE TABLE sample (
            market TEXT, slot INTEGER, source REAL, received REAL, bid REAL, ask REAL,
            price REAL, mark_index REAL, oi REAL, PRIMARY KEY (market, slot)) WITHOUT ROWID''')
        db.execute('CREATE INDEX sample_slot ON sample(slot)')
        db.execute('INSERT INTO sample VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   ('legacy', NOW, NOW - 100, NOW, 100, 101, 100.5, 100.1, 500))


class HistoryTests(unittest.TestCase):
    def test_reads_legacy_and_migrates_without_changing_rows(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            legacy_file(path)
            with History(path) as history:
                expected = [(NOW, NOW - 100, NOW, 100, 101, 100.5, 100.1, 500)]
                self.assertEqual(history.read('legacy', NOW), expected)
                with patch.object(history, '_open_writer', wraps=history._open_writer) as open_writer:
                    history.record([leg('new', NOW)], NOW)
                    history.record([leg('new', NOW + 500, bid=250)], NOW + 500)
                open_writer.assert_called_once()
                self.assertEqual(history.read('legacy', NOW + 500), expected)
                self.assertEqual([row[3] for row in history.read('new', NOW + 500)], [100])
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('PRAGMA journal_mode').fetchone(), ('wal',))
                    self.assertEqual(db.execute('PRAGMA auto_vacuum').fetchone(), (2,))
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(), ('ok',))
                    self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                    self.assertEqual(db.execute('SELECT DISTINCT typeof(market_id) FROM history_sample').fetchall(),
                                     [('integer',)])
                    self.assertIsNone(db.execute("SELECT name FROM sqlite_schema WHERE name='sample'").fetchone())

    def test_failed_migration_leaves_legacy_rows_and_schema(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            legacy_file(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('ALTER TABLE sample DROP COLUMN oi')
            with History(path) as history:
                with self.assertRaises(sqlite3.OperationalError):
                    history.record([], NOW)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT market, bid, ask FROM sample').fetchall(), [('legacy', 100, 101)])
                self.assertIsNone(db.execute("SELECT name FROM sqlite_schema WHERE name='history_sample'").fetchone())

    def test_downsamples_old_buckets_without_fabricating_timestamps_and_expires_rows(self):
        with TemporaryDirectory() as directory, History(Path(directory) / 'history.sqlite3') as history:
            expired = NOW - 2 * MINUTE_MS
            history.record([leg('expired', expired)], expired)
            for i in range(5):
                stamp = NOW + i * MINUTE_MS
                legs = [leg('dense', stamp), leg('peer', stamp)]
                if i in (2, 3):
                    legs.append(leg('sparse', stamp, oi=i))
                history.record(legs, stamp)
            recent = NOW + 12 * MINUTE_MS
            history.record([leg('dense', recent)], recent)
            history.record([leg('dense', recent + MINUTE_MS)], recent + MINUTE_MS)
            now = NOW + RECENT_MS + 5 * MINUTE_MS
            history.record([], now)
            self.assertEqual([row[0] for row in history.read('dense', now)], [NOW, recent, recent + MINUTE_MS])
            self.assertEqual([row[0] for row in history.read('peer', now)], [NOW])
            sparse = history.read('sparse', now)
            self.assertEqual((sparse[0][0], sparse[0][1], sparse[0][7]),
                             (NOW + 3 * MINUTE_MS, NOW + 3 * MINUTE_MS - 100, 3))
            now = NOW + RETENTION_MS + 1
            history.record([], now)
            self.assertEqual(history.read('expired', now), [])
            self.assertEqual(history.read('peer', now), [])
            with closing(sqlite3.connect(history.path)) as db:
                self.assertNotIn('expired', {name for (name,) in db.execute('SELECT market FROM history_market')})

    def test_selects_fresh_compatible_pair_legs(self):
        def market(key, exchange, kind='swap', quote='USDT', asset='btc', bid=100, stamp=NOW):
            return leg(key, stamp, exchange=exchange, type=kind, quote=quote, asset=asset, bid=bid)

        markets = {'btc': [market('spot', 'A', 'spot'), market('swap', 'A'), market('another', 'B'),
                           market('wrong-quote', 'C', quote='USDC'), market('wrong-asset', 'C', asset='other'),
                           market('anomaly', 'C', bid=201), market('stale', 'D', stamp=NOW - 100_000)],
                   'only-spot': [market('spot-a', 'A', 'spot'), market('spot-b', 'B', 'spot')]}
        actual = eligible_legs(markets, NOW, lambda left, right: left['asset'] == right['asset'])
        self.assertEqual([item['id'] for item in actual], ['spot', 'swap', 'another'])


class AsyncHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_closes_after_running_write_and_readers_remain_available(self):
        with TemporaryDirectory() as directory:
            history = History(Path(directory) / 'history.sqlite3')
            started, release = Event(), Event()
            stamp = NOW + MAINTENANCE_MS
            original_maintain = history._maintain

            def delayed_maintenance(*args):
                started.set()
                if not release.wait(3):
                    raise TimeoutError('test did not release writer')
                return original_maintain(*args)

            try:
                await history.write([leg('x', NOW)], NOW)
                with patch.object(history, '_maintain', side_effect=delayed_maintenance):
                    task = asyncio.create_task(history.write([leg('x', stamp)], stamp))
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    rows = await asyncio.wait_for(asyncio.to_thread(history.read, 'x', stamp), 1)
                    self.assertEqual([row[0] for row in rows], [NOW])
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    closing_task = asyncio.create_task(history.aclose())
                    await asyncio.sleep(0)
                    self.assertFalse(closing_task.done())
                    release.set()
                    await closing_task
                self.assertEqual([row[0] for row in history.read('x', stamp)], [NOW, stamp])
                with self.assertRaises(RuntimeError):
                    await history.write([], NOW)
            finally:
                release.set()
                await history.aclose()
