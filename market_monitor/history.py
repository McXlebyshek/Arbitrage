"""Local market observations with a single SQLite writer and short readers."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from threading import Lock
from time import monotonic

from .funding_history import DAY
from .market import fresh
from .thresholds import MAX_PRICE_RATIO


MINUTE_MS = 60_000
RETENTION_MS = 2 * DAY
RECENT_MS = 6 * 60 * MINUTE_MS
DOWNSAMPLE_MS = 5 * MINUTE_MS
MAINTENANCE_MS = 10 * MINUTE_MS
COMPACTION_BATCH_MS = 60 * MINUTE_MS
VACUUM_PAGES = 256


def eligible_legs(markets, now, compatible_assets):
    """Keep fresh legs that can participate in a monitor pair."""
    for group in markets.values():
        current = [leg for leg in group if fresh(leg['receivedAt'], now)]
        selected = set()
        for left in current:
            if left['id'] in selected:
                continue
            for right in current:
                if (left['quote'] != right['quote'] or
                        (left['exchange'], left['type']) == (right['exchange'], right['type']) or
                        'swap' not in (left['type'], right['type'])):
                    continue
                if not compatible_assets(left, right):
                    continue
                prices = [leg.get('bid') if leg.get('bid') is not None else leg.get('last') for leg in (left, right)]
                if all(price is not None and price > 0 for price in prices) and max(prices) / min(prices) > MAX_PRICE_RATIO:
                    continue
                selected.update((left['id'], right['id']))
                break
        yield from (leg for leg in current if leg['id'] in selected)


@contextmanager
def transaction(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)).fetchone() is not None


class History:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='market-history')
        self._submission_lock = Lock()
        self._close_future = None
        self._write_future = None
        self._last_write_ms = None
        self._writer = None
        self._next_maintenance = 0
        self._compacted_until = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _open_writer(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5, autocommit=True)
        try:
            db.execute('PRAGMA auto_vacuum=INCREMENTAL')
            if db.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                raise sqlite3.OperationalError('unable to enable history WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            db.execute('PRAGMA foreign_keys=ON')
            with transaction(db):
                db.execute('''CREATE TABLE IF NOT EXISTS history_market (
                    id INTEGER PRIMARY KEY, market TEXT NOT NULL UNIQUE)''')
                db.execute('''CREATE TABLE IF NOT EXISTS history_sample (
                    market_id INTEGER NOT NULL REFERENCES history_market(id), slot INTEGER NOT NULL,
                    source REAL, received REAL, bid REAL, ask REAL, price REAL, mark_index REAL, oi REAL,
                    PRIMARY KEY (market_id, slot)) WITHOUT ROWID''')
                db.execute('CREATE INDEX IF NOT EXISTS history_sample_slot ON history_sample(slot)')
                if has_table(db, 'sample'):
                    db.execute('INSERT INTO history_market(market) SELECT DISTINCT market FROM sample')
                    db.execute('''INSERT INTO history_sample
                        SELECT id, slot, source, received, bid, ask, price, mark_index, oi
                        FROM sample JOIN history_market USING (market)''')
                    db.execute('DROP TABLE sample')
            if db.execute('PRAGMA auto_vacuum').fetchone()[0] != 2:
                # Existing databases need a one-time rebuild to add the pointer map.
                db.execute('PRAGMA auto_vacuum=INCREMENTAL')
                db.execute('VACUUM')
        except BaseException:
            db.close()
            raise
        return db

    def _submit(self, legs, now):
        with self._submission_lock:
            if self._close_future is not None:
                raise RuntimeError('history is closed')
            return self._enqueue_locked(legs, now)

    def try_write(self, legs, now):
        """Queue one background write only when the serial writer has room."""
        with self._submission_lock:
            if self._close_future is not None:
                raise RuntimeError('history is closed')
            if self._write_future is not None and not self._write_future.done():
                return None
            return self._enqueue_locked(legs, now)

    def _enqueue_locked(self, legs, now):
        future = self._worker.submit(self._record_timed, legs, now)
        self._write_future = future
        return future

    def record(self, legs, now):
        self._submit(legs, now).result()

    async def write(self, legs, now):
        await asyncio.wrap_future(self._submit(legs, now))

    @property
    def last_write_ms(self):
        return self._last_write_ms

    def _record_timed(self, legs, now):
        started = monotonic()
        try:
            return self._record(legs, now)
        finally:
            self._last_write_ms = round((monotonic() - started) * 1000)

    def _record(self, legs, now):
        if self._writer is None:
            self._writer = self._open_writer()
        db = self._writer
        slot = now // MINUTE_MS * MINUTE_MS
        rows = [(slot, leg['sourceAt'], leg['receivedAt'], leg['bid'], leg['ask'],
                 leg['last'], leg['index'], leg['oi'], leg['id'])
                for leg in legs if fresh(leg['receivedAt'], now)]
        maintain = now >= self._next_maintenance or slot < self._compacted_until
        compact_until = (now - RECENT_MS) // DOWNSAMPLE_MS * DOWNSAMPLE_MS
        with transaction(db):
            db.executemany('INSERT INTO history_market(market) VALUES (?) ON CONFLICT DO NOTHING',
                           ((row[-1],) for row in rows))
            db.executemany('''INSERT INTO history_sample
                SELECT id, ?, ?, ?, ?, ?, ?, ?, ? FROM history_market WHERE market=?
                ON CONFLICT (market_id, slot) DO NOTHING''', rows)
            compacted_until = self._maintain(db, slot, now, compact_until) if maintain else None
        if maintain:
            self._next_maintenance = now + MAINTENANCE_MS
            self._compacted_until = compacted_until
            db.execute(f'PRAGMA incremental_vacuum({VACUUM_PAGES})').fetchall()

    def _maintain(self, db, slot, now, compact_until):
        cutoff = now - RETENTION_MS
        db.execute('DELETE FROM history_sample WHERE slot < ?', (cutoff,))
        if slot < self._compacted_until:
            start = max(cutoff, min(self._compacted_until, slot // DOWNSAMPLE_MS * DOWNSAMPLE_MS))
        else:
            start = max(cutoff, self._compacted_until)
        batch_until = min(compact_until, start + COMPACTION_BATCH_MS)
        # Keep actual times. Prefer the shared aligned minute for the spread
        # chart and retain the latest real observation when that minute is absent.
        if start < batch_until:
            db.execute('''DELETE FROM history_sample WHERE (market_id, slot) IN (
                SELECT market_id, slot FROM (
                    SELECT market_id, slot, ROW_NUMBER() OVER (
                        PARTITION BY market_id, slot / ? ORDER BY (slot % ? = 0) DESC, slot DESC
                    ) AS position FROM history_sample WHERE slot >= ? AND slot < ?
                ) WHERE position > 1)''', (DOWNSAMPLE_MS, DOWNSAMPLE_MS, start, batch_until))
        db.execute('''DELETE FROM history_market WHERE NOT EXISTS (
            SELECT 1 FROM history_sample WHERE market_id=history_market.id)''')
        return max(self._compacted_until, batch_until)

    def read(self, key, now):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, autocommit=False)) as db:
            if has_table(db, 'history_sample'):
                return db.execute('''SELECT slot, source, received, bid, ask, price, mark_index, oi
                    FROM history_market JOIN history_sample ON market_id=id
                    WHERE market=? AND slot>=? ORDER BY slot''', (key, now - RETENTION_MS)).fetchall()
            if has_table(db, 'sample'):
                return db.execute('''SELECT slot, source, received, bid, ask, price, mark_index, oi
                    FROM sample WHERE market=? AND slot>=? ORDER BY slot''', (key, now - RETENTION_MS)).fetchall()
            return []

    def _stop(self):
        with self._submission_lock:
            if self._close_future is None:
                self._close_future = self._worker.submit(self._close_writer)
                self._worker.shutdown(wait=False)
            return self._close_future

    def _close_writer(self):
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def close(self):
        self._stop().result()

    async def aclose(self):
        await asyncio.shield(asyncio.wrap_future(self._stop()))
