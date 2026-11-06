from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from arbitrage.__main__ import save
from arbitrage.history import read_events
from arbitrage.recovery import SessionLease
from arbitrage.signals import SignalTracker
from test_signals import row, update
from test_economics import NOW


class RecoveryTests(unittest.TestCase):
    def test_legacy_runs_are_closed_and_failed_new_save_does_not_half_recover(self):
        with TemporaryDirectory() as directory:
            database = Path(directory)/"legacy.sqlite3"
            old = SignalTracker()
            save(database, [], signals=update(old, NOW, row(NOW)))
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("DROP TABLE monitor_runs")
            fresh = SignalTracker()
            signals = update(fresh, NOW+10000, row(NOW+10000))
            bad = json.loads(json.dumps(signals))
            del bad["events"][0]["kind"]
            with self.assertRaises(KeyError):
                save(database, [], signals=bad)
            self.assertEqual([event for event in read_events(database) if event["kind"] == "ended"], [])
            save(database, [], signals=signals)
            self.assertEqual(len(signals["recovered_events"]), 1)
            with closing(sqlite3.connect(database)) as db:
                last_old = json.loads(db.execute("SELECT payload FROM signal_snapshots WHERE run_id=? ORDER BY id DESC LIMIT 1", (old.run_id,)).fetchone()[0])
                self.assertEqual(last_old["state"], "interrupted")
                self.assertEqual(last_old["rows"], [])

    def test_lease_prevents_second_writer_and_releases_on_close(self):
        with TemporaryDirectory() as directory:
            database = Path(directory)/"db.sqlite3"
            with SessionLease(database):
                with self.assertRaises(RuntimeError):
                    with SessionLease(database):
                        pass
            with SessionLease(database):
                pass

    def test_restart_closes_abandoned_confirmed_episode_and_starts_new_streak(self):
        with TemporaryDirectory() as directory:
            database = Path(directory)/"db.sqlite3"
            tracker = SignalTracker()
            for delta in (0, 1000, 2000):
                signals = update(tracker, NOW+delta, row(NOW+delta))
                save(database, [], signals=signals)
            self.assertEqual(signals["rows"][0]["state"], "confirmed")
            # No stopped publication: simulate the durable state left by a crash.
            fresh = SignalTracker()
            signals = update(fresh, NOW+30000, row(NOW+30000))
            save(database, [], signals=signals)
            self.assertEqual(signals["rows"][0]["state"], "pending")
            self.assertEqual(signals["rows"][0]["confirmations"], 1)
            endings = [event for event in read_events(database) if event["kind"] == "ended"]
            self.assertEqual(len(endings), 1)
            self.assertEqual(endings[0]["reasons"], ["previous_session_interrupted"])
            self.assertFalse(endings[0]["termination_time_known"])
            save(database, [], signals=signals)
            self.assertEqual(len([event for event in read_events(database) if event["kind"] == "ended"]), 1)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT state FROM monitor_runs WHERE run_id=?", (tracker.run_id,)).fetchone()[0], "interrupted")

    def test_clean_shutdown_does_not_generate_recovery(self):
        with TemporaryDirectory() as directory:
            database = Path(directory)/"db.sqlite3"
            tracker = SignalTracker()
            save(database, [], signals=update(tracker, NOW, row(NOW)))
            save(database, [], signals=update(tracker, NOW+1000, state="stopped"))
            signals = update(SignalTracker(), NOW+2000, row(NOW+2000))
            save(database, [], signals=signals)
            self.assertEqual(signals["recovered_events"], [])
