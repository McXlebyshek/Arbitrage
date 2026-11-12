from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from market_monitor.exchange_clock import ExchangeClock
from market_monitor.history import DAY, History, MINUTE
from market_monitor.history_selection import history_legs


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_history_migrates_and_prunes_on_the_dedicated_worker(self):
        now = 1_800_000_000_000
        slots = (now - 2 * DAY - MINUTE, now - 7 * 60 * MINUTE, now - 7 * 60 * MINUTE + MINUTE,
                 now - 2 * MINUTE, now - MINUTE)
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'history.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE sample (market, slot, source, received, bid, ask, price, mark_index, oi)')
                db.executemany('INSERT INTO sample VALUES (?,?,?,?,?,?,?,?,?)',
                               [('btc', slot, slot, slot, 100, 101, 100, None, 42) for slot in slots])
            history = History(path)
            try:
                samples = await history.run(history.read, 'btc', now)
                self.assertEqual([row[0] for row in samples], list(slots[1:]))
                await history.run(history.record, [], now)
                samples = await history.run(history.read, 'btc', now)
                self.assertEqual([row[0] for row in samples], [slots[1], *slots[-2:]])
                self.assertEqual([row[7] for row in samples], [42, 42, 42])

                async def pragma(sql):
                    return await history.run(lambda: history.connection.execute(sql).fetchone()[0])

                self.assertEqual(await pragma('PRAGMA journal_mode'), 'wal')
                self.assertEqual(await pragma('PRAGMA auto_vacuum'), 2)
            finally:
                await history.shutdown()


class ExchangeClockTests(unittest.TestCase):
    def test_applies_trusted_offset_and_expires_it(self):
        now = 1_800_000_000_000
        clock = ExchangeClock()
        self.assertEqual(clock.correct(now + 800, now), now + 800)
        clock.offset, clock.checked_at = 800, now
        self.assertEqual(clock.correct(now + 800, now), now)
        self.assertEqual(clock.correct(now + 900_801, now + 900_001), now + 900_801)


class HistorySelectionTests(unittest.TestCase):
    def test_keeps_comparable_legs_and_retains_watched_skewed_markets(self):
        now = 1_800_000_000_000
        a = dict(id='a', exchange='A', type='swap', quote='USDT', asset='btc',
                 bid=100, ask=101, receivedAt=now, sourceAt=now)
        b = dict(a, id='b', exchange='B', sourceAt=now - 1500)
        skew = dict(a, id='skew', exchange='C', sourceAt=now - 3100)
        isolated = dict(a, id='alone', exchange='D', quote='USDC')
        market = {'BTC': [a, b, skew], 'OTHER': [isolated]}
        self.assertEqual(history_legs(market, now, set(), set()), [a, b])
        self.assertEqual(history_legs(market, now, {'skew'}, {'OTHER'}), [a, b, skew, isolated])
