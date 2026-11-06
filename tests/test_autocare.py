import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from arbitrage.__main__ import save
from arbitrage.autocare import (AutoCare, CarePolicy, backup_root, manifest, prune_cycle,
                               read_archive, rotate_backups, write_archive)
from arbitrage.funding_history import persist
from arbitrage.history import read_buckets
from test_economics import fixture
from test_storage import economic

NOW = 1793780000000
DAY = 86400000


def seed(path):
    for stamp in (NOW-40*DAY, NOW-10*DAY, NOW):
        save(path, [], economics=economic(stamp))
    persist(path, "mexc", dict(id="BTC_USDT", asset="bitcoin"),
            dict(events=[dict(timestamp=NOW-120*DAY, rate="0.001"), dict(timestamp=NOW-DAY, rate="0.002")],
                 requests=1, error=None), NOW)


class AutoCareStorageTests(unittest.TestCase):
    def test_expired_rows_have_verified_archive_before_deletion(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"market.sqlite3"
            seed(path)
            result = prune_cycle(path, CarePolicy(), NOW)
            self.assertEqual(result["state"], "pruned")
            self.assertEqual(result["deleted_rows"]["economics"], 2)
            self.assertEqual(result["deleted_rows"]["history_rollups"], 1)
            self.assertEqual(result["deleted_rows"]["funding_history_events"], 1)
            archive = read_archive(result["backup"], limit=1000)
            self.assertEqual(len(archive["rows"]), sum(result["deleted_rows"].values()))
            self.assertEqual(len(read_buckets(path)), 2)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM economics").fetchone()[0], 1)
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_archive_failure_cannot_remove_source_data(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"market.sqlite3"
            seed(path)
            before = path.read_bytes()
            with patch("arbitrage.autocare.write_archive", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    prune_cycle(path, CarePolicy(), NOW)
            self.assertEqual(path.read_bytes(), before)

    def test_concurrent_funding_correction_is_not_deleted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"market.sqlite3"
            seed(path)
            def update_after_archive(*args):
                archive = write_archive(*args)
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("UPDATE funding_history_events SET rate='0.123',observed_at_ms=? WHERE timestamp_ms<?", (NOW+1, NOW-90*DAY))
                return archive
            with patch("arbitrage.autocare.write_archive", side_effect=update_after_archive):
                result = prune_cycle(path, CarePolicy(), NOW)
            self.assertEqual(result["deleted_rows"]["funding_history_events"], 0)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT rate FROM funding_history_events ORDER BY timestamp_ms LIMIT 1").fetchone()[0], "0.123")

    def test_new_open_episode_between_archive_and_prune_changes_protection(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"market.sqlite3"
            seed(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE signal_events(event_id TEXT PRIMARY KEY,episode_id TEXT,timestamp_ms INTEGER,kind TEXT)")
            def update_after_archive(*args):
                archive = write_archive(*args)
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("INSERT INTO signal_events VALUES ('new','open',?,'appeared')", (NOW-20*DAY,))
                return archive
            with patch("arbitrage.autocare.write_archive", side_effect=update_after_archive):
                result = prune_cycle(path, CarePolicy(), NOW)
            self.assertEqual(result["state"], "protection_changed")
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM economics").fetchone()[0], 3)

    def test_rotation_uses_owned_manifest_only_and_keeps_latest_at_same_time(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"market.sqlite3"
            root, policy = backup_root(path), CarePolicy(backup_count=2)
            selected = {"economics": dict(columns=["id", "payload"], rows=[(1, [1, "original"])])}
            archives = [write_archive(path, root, NOW, selected, policy) for _ in range(3)]
            unknown = root/"my-personal-copy.sqlite3"
            unknown.write_text("leave me", encoding="utf-8")
            report = rotate_backups(root, policy)
            self.assertFalse(archives[0].exists())
            self.assertTrue(archives[-1].exists())
            self.assertEqual(report["retained"], 2)
            self.assertEqual(unknown.read_text(encoding="utf-8"), "leave me")
            report = rotate_backups(root, CarePolicy(backup_count=2, backup_max_bytes=1))
            self.assertEqual(report["retained"], 1)
            self.assertTrue(report["budget_exceeded"])
            self.assertTrue(archives[-1].exists())

    def test_manifest_cannot_escape_directory_and_small_budget_prevents_deletion(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"market.sqlite3"
            seed(path)
            root = backup_root(path)
            with self.assertRaises(ValueError):
                prune_cycle(path, CarePolicy(backup_max_bytes=1), NOW)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM economics").fetchone()[0], 3)
            root.mkdir(parents=True, exist_ok=True)
            (root/"manifest.json").write_text(json.dumps([dict(name="../outside.sqlite3", verified=True)]), encoding="utf-8")
            with self.assertRaises(ValueError):
                rotate_backups(root, CarePolicy())

    def test_batch_bound_and_backfill_gate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"market.sqlite3"
            for index in range(10):
                save(path, [], economics=economic(NOW-10*DAY+index))
            result = prune_cycle(path, CarePolicy(batch_rows=2), NOW)
            self.assertEqual(result["deleted_rows"]["economics"], 2)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("DELETE FROM history_checkpoints")
            result = prune_cycle(path, CarePolicy(), NOW)
            self.assertEqual(result["state"], "aggregation_pending")

    def test_busy_database_skips_without_creating_archive(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"market.sqlite3"
            seed(path)
            with closing(sqlite3.connect(path)) as writer:
                writer.execute("BEGIN EXCLUSIVE")
                with self.assertRaises(sqlite3.OperationalError):
                    prune_cycle(path, CarePolicy(), NOW)
                writer.rollback()
            self.assertFalse(backup_root(path).exists())

    def test_failed_prune_transaction_keeps_originals_and_verified_archive(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"market.sqlite3"
            seed(path)
            def deny_after_archive(*args):
                archive = write_archive(*args)
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("CREATE TRIGGER deny_delete BEFORE DELETE ON economics BEGIN SELECT RAISE(ABORT,'simulated'); END")
                return archive
            with patch("arbitrage.autocare.write_archive", side_effect=deny_after_archive):
                with self.assertRaises(sqlite3.IntegrityError):
                    prune_cycle(path, CarePolicy(), NOW)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM economics").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT count(*) FROM funding_history_events").fetchone()[0], 2)
            self.assertEqual(len(manifest(backup_root(path))), 1)

    def test_episode_larger_than_batch_is_never_partially_deleted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"market.sqlite3"
            save(path, [], economics=economic(NOW-10*DAY))
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE signal_events(event_id TEXT PRIMARY KEY,episode_id TEXT,timestamp_ms INTEGER,kind TEXT)")
                for index, kind in enumerate(("appeared", "reason_changed", "ended")):
                    db.execute("INSERT INTO signal_events VALUES (?,?,?,?)", (str(index), "closed", NOW-10*DAY+index, kind))
            result = prune_cycle(path, CarePolicy(batch_rows=2), NOW)
            self.assertEqual(result["state"], "batch_limit_blocked")
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM signal_events").fetchone()[0], 3)


class AutoCareWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_worker_does_not_block_loop_and_shutdown_waits_for_worker(self):
        with TemporaryDirectory() as directory:
            care = AutoCare(Path(directory)/"market.sqlite3", interval=0.01)
            started, release = threading.Event(), threading.Event()
            def slow(*args):
                started.set()
                release.wait(3)
                return dict(state="idle")
            with patch("arbitrage.autocare.prune_cycle", side_effect=slow):
                task = asyncio.create_task(care.run())
                try:
                    for _ in range(100):
                        if started.is_set():
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(started.is_set())
                    task.cancel()
                    await asyncio.sleep(0.02)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(care.snapshot()["state"], "stopped")

    async def test_worker_error_is_reported_and_does_not_end_scheduler(self):
        with TemporaryDirectory() as directory:
            care = AutoCare(Path(directory)/"market.sqlite3", interval=0.01)
            with patch("arbitrage.autocare.prune_cycle", side_effect=[RuntimeError("busy"), dict(state="idle")]):
                task = asyncio.create_task(care.run())
                try:
                    for _ in range(100):
                        if care.status["cycles"] >= 2:
                            break
                        await asyncio.sleep(0.005)
                    self.assertGreaterEqual(care.status["cycles"], 2)
                    self.assertIsNone(care.status["last_error"])
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_funding_refresh_receives_registry_and_warmup_is_independent(self):
        with TemporaryDirectory() as directory:
            _, _, registry = fixture()
            care = AutoCare(Path(directory)/"market.sqlite3", interval=10, funding_hours=24,
                            exchanges=["alpha"], registry=registry, funding_delay=0)
            with patch("arbitrage.funding_history.collect", return_value=[dict(error=None)]) as collect:
                task = asyncio.create_task(care.run())
                try:
                    for _ in range(100):
                        if care.status["funding"]["state"] == "updated":
                            break
                        await asyncio.sleep(0.005)
                    self.assertEqual(care.status["funding"]["state"], "updated")
                    self.assertIs(collect.call_args.args[3], registry)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_fresh_archive_avoids_repeat_import_after_restart(self):
        with TemporaryDirectory() as directory:
            _, _, registry = fixture()
            path = Path(directory)/"market.sqlite3"
            with closing(sqlite3.connect(path)):
                pass
            care = AutoCare(path, interval=10, funding_hours=24, exchanges=["alpha"], registry=registry, funding_delay=0)
            cached = [dict(exchange="alpha", native_id="BTC_USDT", error=None, end_ms=int(time.time()*1000))]
            with patch("arbitrage.funding_history.read_reports", return_value=cached), patch("arbitrage.funding_history.collect") as collect:
                task = asyncio.create_task(care.run())
                try:
                    for _ in range(100):
                        if care.status["funding"]["state"] == "cached_fresh":
                            break
                        await asyncio.sleep(0.005)
                    self.assertEqual(care.status["funding"]["state"], "cached_fresh")
                    collect.assert_not_called()
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
