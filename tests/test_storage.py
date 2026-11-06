from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from arbitrage.__main__ import save
from arbitrage.history import read_buckets, rollup_status
from arbitrage.storage import aggregate_history, maintain, retention_plan


def economic(stamp, net=2, capital=1000, status="scenario_estimated"):
    return dict(generated_at_ms=stamp, policy=dict(capital_usdt=capital, hold_hours=8),
                rows=[dict(id="pair", asset="bitcoin", status=status,
                           reasons=["depth_stale"] if status == "blocked" else [],
                           convergence_net_scenario_usdt=net if status == "scenario_estimated" else None)])


class StorageTests(unittest.TestCase):
    def test_pruning_backfills_legacy_before_removing_sources(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"legacy.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE economics(id INTEGER PRIMARY KEY,generated_at_ms INTEGER,payload TEXT)")
                for stamp in (100000, 200000, 300000):
                    db.execute("INSERT INTO economics(generated_at_ms,payload) VALUES (?,?)", (stamp, json.dumps(economic(stamp))))
            self.assertEqual(rollup_status(path)["pending_raw_snapshots"], 3)
            result = maintain(path, 500000, True)
            self.assertEqual(result["processed_economic_snapshots"], 3)
            self.assertTrue(rollup_status(path)["caught_up"])
            self.assertEqual(sum(x["observations"] for x in read_buckets(path)), 3)

    def test_rollup_is_incremental_and_separates_policy_and_missing_values(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"test.sqlite3"
            for args in ((100000, 2, 1000), (101000, -1, 1000), (102000, 8, 500)):
                save(path, [], economics=economic(*args))
            save(path, [], economics=economic(103000, status="blocked"))
            buckets = read_buckets(path)
            self.assertEqual(len(buckets), 2)
            full = next(x for x in buckets if x["policy"]["capital_usdt"] == 1000)
            self.assertEqual(full["observations"], 3)
            self.assertEqual(full["net_samples"], 2)
            self.assertEqual(full["net_mean_usdt"], 0.5)
            self.assertEqual(full["positive_samples"], 1)
            self.assertEqual(full["reasons"], {"depth_stale": 1})
            self.assertEqual(aggregate_history(path)["processed_economic_snapshots"], 0)
            self.assertEqual(read_buckets(path), buckets)
            self.assertEqual(read_buckets(path, pair_id="missing"), [])
            self.assertEqual(read_buckets(path, since_ms=120000), [])

    def test_preview_does_not_modify_database_and_apply_preserves_rollups_and_backup(self):
        with TemporaryDirectory() as directory:
            path, backup = Path(directory)/"test.sqlite3", Path(directory)/"backup.sqlite3"
            for stamp in (100000, 200000, 300000):
                save(path, [], economics=economic(stamp))
            before = path.read_bytes()
            preview = maintain(path, 250000)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(preview["deletable_rows"]["economics"], 2)
            buckets = read_buckets(path)
            result = maintain(path, 250000, True, backup, True)
            self.assertEqual(result["integrity"], "ok")
            self.assertEqual(read_buckets(path), buckets)
            with closing(sqlite3.connect(path)) as db, closing(sqlite3.connect(backup)) as original:
                self.assertEqual(db.execute("SELECT count(*) FROM economics").fetchone()[0], 1)
                self.assertEqual(original.execute("SELECT count(*) FROM economics").fetchone()[0], 3)
            save(path, [], economics=economic(400000))
            self.assertEqual(sum(x["observations"] for x in read_buckets(path)), 4)

    def test_open_and_boundary_episodes_pin_raw_history(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"test.sqlite3"
            for stamp in (100000, 200000, 300000):
                save(path, [], economics=economic(stamp))
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE signal_events(event_id TEXT PRIMARY KEY,episode_id TEXT,timestamp_ms INTEGER,kind TEXT)")
                db.execute("INSERT INTO signal_events VALUES ('a','open',200000,'appeared')")
                plan, _ = retention_plan(db, 400000)
                self.assertEqual(plan["effective_cutoff_ms"], 197000)
                self.assertEqual(plan["deletable_rows"]["economics"], 1)
                db.execute("INSERT INTO signal_events VALUES ('b','open',500000,'ended')")
                plan, _ = retention_plan(db, 400000)
                self.assertEqual(plan["effective_cutoff_ms"], 197000)
                self.assertEqual(plan["deletable_rows"]["signal_events"], 0)
            maintain(path, 600000, True)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM signal_events").fetchone()[0], 0)

    def test_backup_overwrite_refused_and_failed_rollup_rolls_back_deletion(self):
        with TemporaryDirectory() as directory:
            path, backup = Path(directory)/"test.sqlite3", Path(directory)/"existing.sqlite3"
            save(path, [], economics=economic(100000))
            backup.write_bytes(b"keep")
            with self.assertRaises(ValueError):
                maintain(path, 200000, True, backup)
            self.assertEqual(backup.read_bytes(), b"keep")
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO economics(generated_at_ms,payload) VALUES (?,?)", (150000, "malformed json"))
            with self.assertRaises(ValueError):
                maintain(path, 200000, True)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM economics").fetchone()[0], 2)

    def test_legacy_backfill_and_latest_id_anchor_prevent_checkpoint_loss(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"legacy.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE economics(id INTEGER PRIMARY KEY, generated_at_ms INTEGER,payload TEXT)")
                for stamp in (100000, 110000):
                    db.execute("INSERT INTO economics(generated_at_ms,payload) VALUES (?,?)", (stamp, json.dumps(economic(stamp))))
            self.assertEqual(aggregate_history(path)["processed_economic_snapshots"], 2)
            maintain(path, 500000, True)
            save(path, [], economics=economic(600000))
            self.assertEqual(sum(x["observations"] for x in read_buckets(path)), 3)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT max(id) FROM economics").fetchone()[0], 3)
