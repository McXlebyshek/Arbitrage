from copy import deepcopy
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from arbitrage.__main__ import save, publish
from arbitrage.history import read_events
from arbitrage.signals import SignalTracker
from test_economics import NOW


def row(stamp, value=2):
    return dict(id="btc|long|short", status="scenario_estimated", reasons=[],
                convergence_net_scenario_usdt=value, depth_source_times=dict(long=stamp, short=stamp))


def update(tracker, stamp, item=None, state="ready", available=True):
    return tracker.update(dict(state=state, rows=[item] if item is not None else []),
                          dict(rows=[dict(id="btc|long|short")] if available else []), stamp)


class SignalTests(unittest.TestCase):
    def test_publication_filters_do_not_hide_signal_and_shutdown_persists_ending(self):
        raw = dict(id="btc|long|short", open_spread_pct=-1)
        scan = dict(rows=[raw], displayed_pairs=1, exclusions=[], skipped_pairs=[], generated_at_ms=NOW)
        class Engine:
            stopped = False
            def submit(self, scanner):
                pass
            def snapshot(self, scanner, timestamp):
                return dict(state="stopped" if self.stopped else "ready", generated_at_ms=timestamp,
                    rows=[] if self.stopped else [dict(row(timestamp), long_exchange="alpha", short_exchange="beta")])
        with TemporaryDirectory() as directory:
            engine = Engine()
            args = SimpleNamespace(database=Path(directory)/"run.sqlite3", output=Path(directory)/"latest.json",
                registry=None, capital_usdt=1000, max_skew_ms=1000, min_spread_pct=0, economic_engine=engine, top=0)
            with patch("arbitrage.__main__.scan", return_value=scan), redirect_stdout(StringIO()):
                for delta in (0, 1000, 2000):
                    with patch("arbitrage.__main__.time.time", return_value=(NOW+delta)/1000):
                        publish(args, [], "WebSocket")
                document = json.loads(args.output.read_text(encoding="utf-8"))
                self.assertEqual(document["scanner"]["rows"], [])
                self.assertEqual(document["signals"]["rows"][0]["state"], "confirmed")
                engine.stopped = True
                with patch("arbitrage.__main__.time.time", return_value=(NOW+3000)/1000):
                    publish(args, [], "WebSocket")
            events = read_events(args.database)
            self.assertEqual([event["kind"] for event in events], ["appeared", "confirmed", "ended"])
            self.assertEqual(events[-1]["reasons"], ["monitor_stopped"])
            self.assertEqual(json.loads(args.output.read_text(encoding="utf-8"))["signals"]["rows"], [])

    def test_confirmation_requires_three_independent_samples_and_elapsed_span(self):
        tracker = SignalTracker()
        for delta in (0, 1000, 2000):
            result = update(tracker, NOW+delta, row(NOW+delta))
        self.assertEqual(result["rows"][0]["state"], "confirmed")
        self.assertEqual(result["events"][0]["kind"], "confirmed")
        self.assertFalse(result["execution_verified"])
        # Snapshots returned to callers cannot change the live tracker.
        result["rows"][0]["confirmations"] = 999
        self.assertEqual(tracker.active["btc|long|short"]["confirmations"], 3)

    def test_republished_or_one_leg_only_book_cannot_confirm(self):
        tracker = SignalTracker(max_skew_ms=3000)
        update(tracker, NOW, row(NOW))
        for delta in (500, 1000, 1500, 2000):
            item = row(NOW+delta)
            item["depth_source_times"]["short"] = NOW
            result = update(tracker, NOW+delta, item)
        self.assertEqual(result["rows"][0]["confirmations"], 1)
        self.assertEqual(result["rows"][0]["state"], "pending")
        tracker = SignalTracker(min_seconds=2)
        for delta in (0, 100, 200):
            update(tracker, NOW+delta, row(NOW+delta))
        result = update(tracker, NOW+2000, row(NOW+200))
        self.assertEqual(result["rows"][0]["state"], "pending")

    def test_loss_resets_streak_and_recovery_requires_new_samples(self):
        tracker = SignalTracker()
        for delta in (0, 1000, 2000):
            update(tracker, NOW+delta, row(NOW+delta))
        blocked = dict(row(NOW+3000), status="blocked", reasons=["depth_stale"])
        result = update(tracker, NOW+3000, blocked)
        self.assertEqual(result["events"][0]["kind"], "lost")
        self.assertEqual(result["rows"][0]["confirmations"], 0)
        result = update(tracker, NOW+4000, row(NOW+4000))
        self.assertEqual(result["rows"][0]["state"], "pending")
        self.assertEqual(result["rows"][0]["confirmations"], 1)

    def test_negative_unknown_and_threshold_equality_never_confirm(self):
        for value in (-2, 0, None, "nan"):
            tracker = SignalTracker()
            for delta in (0, 1000, 2000):
                result = update(tracker, NOW+delta, row(NOW+delta, value))
            self.assertEqual(result["rows"][0]["state"], "rejected")
            self.assertEqual(result["rows"][0]["confirmations"], 0)
        tracker = SignalTracker(min_usdt=2)
        self.assertEqual(update(tracker, NOW, row(NOW))["rows"][0]["state"], "rejected")

    def test_gap_and_clock_rollback_cannot_reuse_confirmations(self):
        tracker = SignalTracker()
        for delta in (0, 1000, 2000):
            update(tracker, NOW+delta, row(NOW+delta))
        result = update(tracker, NOW+15000, row(NOW+15000))
        self.assertEqual(result["rows"][0]["state"], "pending")
        self.assertEqual(result["events"][0]["kind"], "continuity_reset")
        result = update(tracker, NOW+14000, row(NOW+14000))
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["ended"][0]["reasons"], ["publication_clock_not_advancing"])

    def test_source_ages_skew_and_regression_are_rejected(self):
        for stamps in ({"long": NOW-4000, "short": NOW}, {"long": NOW, "short": NOW-2000}, {}):
            result = update(SignalTracker(), NOW, dict(row(NOW), depth_source_times=stamps))
            self.assertEqual(result["rows"][0]["state"], "rejected")
        tracker = SignalTracker()
        update(tracker, NOW, row(NOW))
        result = update(tracker, NOW+1000, row(NOW-100))
        self.assertEqual(result["rows"][0]["reasons"], ["depth_source_time_regressed"])

    def test_ending_reason_episode_identity_restart_and_memory_bound(self):
        for reason, state, available in (("monitor_stopped", "stopped", True),
                                         ("selection_changed", "ready", True),
                                         ("quote_unavailable_or_ineligible", "ready", False)):
            tracker = SignalTracker()
            first = update(tracker, NOW, row(NOW))
            result = update(tracker, NOW+1000, state=state, available=available)
            self.assertEqual(result["ended"][0]["reasons"], [reason])
            self.assertEqual(result["rows"], [])
            self.assertEqual(len(tracker.active), 0)
            second = update(tracker, NOW+2000, row(NOW+2000))
            self.assertNotEqual(first["rows"][0]["episode_id"], second["rows"][0]["episode_id"])
        other = SignalTracker()
        self.assertNotEqual(other.run_id, tracker.run_id)
        self.assertEqual(other.active, {})

    def test_sqlite_events_are_idempotent_filterable_and_atomic_with_economics(self):
        tracker = SignalTracker()
        result = update(tracker, NOW, row(NOW))
        with TemporaryDirectory() as directory:
            path = Path(directory)/"history.sqlite3"
            economic = dict(generated_at_ms=NOW, rows=[row(NOW)])
            save(path, [], economics=economic, signals=result)
            save(path, [], economics=economic, signals=result)
            self.assertEqual(len(read_events(path)), 1)
            self.assertEqual(read_events(path, "missing"), [])
            self.assertEqual(read_events(path, "btc|long|short", 1)[0]["kind"], "appeared")
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM signal_snapshots").fetchone()[0], 2)
            bad = deepcopy(result)
            bad["events"][0].pop("kind")
            with self.assertRaises(KeyError):
                save(path, [], economics=economic, signals=bad)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM economics").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT count(*) FROM signal_snapshots").fetchone()[0], 2)
