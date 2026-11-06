"""Controlled process-crash validation using synthetic scenarios, without APIs."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from arbitrage.__main__ import save
from arbitrage.history import read_events
from arbitrage.recovery import SessionLease
from arbitrage.signals import SignalTracker


def observation(tracker, stamp, stopped=False):
    row = dict(id="synthetic|long|short", status="scenario_estimated", reasons=[],
               convergence_net_scenario_usdt=2, depth_source_times=dict(long=stamp, short=stamp))
    return tracker.update(dict(state="stopped" if stopped else "ready", rows=[] if stopped else [row]),
                          dict(rows=[row]), stamp)


def validate(database):
    if database.exists():
        raise ValueError("Validation database must be new")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", "--database", str(database)],
                           capture_output=True, creationflags=flags)
    if child.returncode != 23:
        raise RuntimeError(f"Unexpected crash child result {child.returncode}: {child.stderr.decode(errors='replace')}")
    with closing(sqlite3.connect(database)) as db:
        old_run = db.execute("SELECT run_id FROM monitor_runs").fetchone()[0]
        old = json.loads(db.execute("SELECT payload FROM signal_snapshots ORDER BY id DESC LIMIT 1").fetchone()[0])
        assert old["rows"][0]["state"] == "confirmed"
    with SessionLease(database):
        competitor = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--try-lock", "--database", str(database)],
                                    capture_output=True, creationflags=flags)
        assert competitor.returncode == 5
        fresh = SignalTracker()
        now = int(time.time()*1000)+5000
        signals = observation(fresh, now)
        save(database, [], signals=signals)
        assert signals["rows"][0]["state"] == "pending" and signals["rows"][0]["confirmations"] == 1
        assert len(signals["recovered_events"]) == 1
        save(database, [], signals=observation(fresh, now+1000, stopped=True))
    with closing(sqlite3.connect(database)) as db:
        states = dict(db.execute("SELECT run_id,state FROM monitor_runs"))
        assert states[old_run] == "interrupted" and states[fresh.run_id] == "stopped"
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"
    return dict(synthetic_controlled_data=True, market_claim=False, child_exit_code=23,
                competitor_exit_code=5, prior_state="confirmed", recovered_state="pending", confirmations_after_restart=1,
                abandoned_run_state=states[old_run], recovery_events=[event for event in read_events(database)
                    if "previous_session_interrupted" in event["reasons"]], integrity=integrity)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/crash-recovery-check.sqlite3"))
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--try-lock", action="store_true")
    args = parser.parse_args()
    if args.try_lock:
        try:
            with SessionLease(args.database):
                pass
        except RuntimeError:
            raise SystemExit(5)
    elif args.child:
        with SessionLease(args.database):
            tracker = SignalTracker()
            now = int(time.time()*1000)
            for offset in (0, 1000, 2000):
                save(args.database, [], signals=observation(tracker, now+offset))
            os._exit(23)
    else:
        result = validate(args.database)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        Path("data/crash-recovery-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
