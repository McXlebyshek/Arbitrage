"""Local, bounded chart history. The monitor runs database work on one worker."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

from .funding_history import DAY
from .market import fresh


MINUTE = 60_000
HISTORY_WINDOW = 2 * DAY
HISTORY_DENSE_WINDOW = 6 * 60 * MINUTE
HISTORY_COARSE_SLOT = 5 * MINUTE
MAINTENANCE_PERIOD = 10 * MINUTE


class History:
    def __init__(self, path: Path):
        self.path = path
        self.connection = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='market-history')
        self.next_cleanup = 0
        self.market_ids = {}

    async def run(self, operation, *args):
        return await asyncio.get_running_loop().run_in_executor(self.executor, operation, *args)

    def open(self):
        if self.connection is not None:
            return self.connection
        new_database = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        try:
            if new_database:
                db.execute('PRAGMA auto_vacuum=INCREMENTAL')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            migrated = False
            with db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS history_market (id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL)')
                db.execute('''CREATE TABLE IF NOT EXISTS history_sample (
                    market_id INTEGER NOT NULL, slot INTEGER NOT NULL, source REAL, received REAL,
                    bid REAL, ask REAL, price REAL, mark_index REAL, oi REAL,
                    PRIMARY KEY (market_id, slot)) WITHOUT ROWID''')
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sample'").fetchone():
                    db.execute('INSERT OR IGNORE INTO history_market(key) SELECT DISTINCT market FROM sample')
                    db.execute('''INSERT OR IGNORE INTO history_sample
                        SELECT m.id, s.slot, s.source, s.received, s.bid, s.ask, s.price, s.mark_index, s.oi
                        FROM sample AS s JOIN history_market AS m ON m.key = s.market''')
                    db.execute('DROP TABLE sample')
                    migrated = True
                db.execute('CREATE INDEX IF NOT EXISTS history_sample_slot ON history_sample(slot)')
            if migrated and db.execute('PRAGMA auto_vacuum').fetchone()[0] == 0:
                db.execute('PRAGMA auto_vacuum=INCREMENTAL')
                db.execute('VACUUM')
            self.market_ids = dict(db.execute('SELECT key, id FROM history_market'))
        except BaseException:
            db.close()
            raise
        self.connection = db
        return db

    def record(self, legs, now: int):
        db = self.open()
        slot = now // MINUTE * MINUTE
        usable = [leg for leg in legs if fresh(leg['receivedAt'], now) and
                  any(leg[field] is not None for field in ('bid', 'ask', 'last', 'index', 'oi'))]
        new_keys = {leg['id'] for leg in usable if leg['id'] not in self.market_ids}
        ids = {}
        cleanup = slot >= self.next_cleanup
        with db:
            for key in sorted(new_keys):
                db.execute('INSERT OR IGNORE INTO history_market(key) VALUES (?)', (key,))
                ids[key], = db.execute('SELECT id FROM history_market WHERE key=?', (key,)).fetchone()
            db.executemany('INSERT OR IGNORE INTO history_sample VALUES (?,?,?,?,?,?,?,?,?)', (
                (self.market_ids.get(leg['id']) or ids[leg['id']], slot, leg['sourceAt'], leg['receivedAt'],
                 leg['bid'], leg['ask'], leg['last'], leg['index'], leg['oi'])
                for leg in usable
            ))
            if cleanup:
                db.execute('DELETE FROM history_sample WHERE slot < ?', (slot - HISTORY_WINDOW,))
                db.execute('''DELETE FROM history_sample WHERE slot >= ? AND slot < ? AND slot % ? != 0''',
                           (self.next_cleanup - MAINTENANCE_PERIOD - HISTORY_DENSE_WINDOW
                            if self.next_cleanup else 0, slot - HISTORY_DENSE_WINDOW, HISTORY_COARSE_SLOT))
        self.market_ids.update(ids)
        if cleanup:
            self.next_cleanup = slot + MAINTENANCE_PERIOD
            db.execute('PRAGMA incremental_vacuum(128)')

    def read(self, key: str, now: int):
        if self.connection is None and not self.path.exists():
            return []
        db = self.open()
        market_id = self.market_ids.get(key)
        if market_id is None:
            return []
        return db.execute(
            'SELECT slot,source,received,bid,ask,price,mark_index,oi FROM history_sample '
            'WHERE market_id=? AND slot>=? ORDER BY slot', (market_id, now - HISTORY_WINDOW),
        ).fetchall()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    async def shutdown(self):
        await self.run(self.close)
        self.executor.shutdown(wait=False)
