"""Single-writer session lease and explicit interruption of abandoned signal episodes."""
from pathlib import Path
import json


class SessionInUse(RuntimeError):
    pass


class SessionLease:
    def __init__(self, database):
        self.path = Path(str(Path(database).resolve())+".monitor.lock")
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise SessionInUse("Another monitor is using this database") from None
        return self

    def __exit__(self, *unused):
        if self.handle is not None:
            self.handle.close()  # The OS also releases the lease on process death.
            self.handle = None


def recover_sessions(db, signals):
    """Called inside the same transaction as the new run's first publication."""
    db.execute("CREATE TABLE IF NOT EXISTS monitor_runs (run_id TEXT PRIMARY KEY, state TEXT NOT NULL, last_seen_at_ms INTEGER NOT NULL)")
    run, now = signals["run_id"], signals["generated_at_ms"]
    known = db.execute("SELECT 1 FROM monitor_runs WHERE run_id=?", (run,)).fetchone()
    recovered = []
    if not known:
        db.execute("CREATE INDEX IF NOT EXISTS signal_events_episode ON signal_events(episode_id)")
        previous = db.execute("SELECT payload FROM signal_events WHERE rowid IN (SELECT max(rowid) FROM signal_events GROUP BY episode_id) AND kind!='ended' AND run_id!=?", (run,)).fetchall()
        for (payload,) in previous:
            old = json.loads(payload)
            event = dict(event_id=f"recovery:{old['episode_id']}", run_id=old["run_id"],
                episode_id=old["episode_id"], pair_id=old["pair_id"], timestamp_ms=now,
                kind="ended", from_state=old["to_state"], to_state="ended",
                reasons=["previous_session_interrupted"], confirmations=0, conditional_net_usdt=None,
                recovered_by_run_id=run, last_event_at_ms=old["timestamp_ms"],
                termination_time_known=False)
            db.execute("INSERT OR IGNORE INTO signal_events VALUES (?,?,?,?,?,?,?)", (event["event_id"], now,
                event["run_id"], event["pair_id"], event["episode_id"], event["kind"], json.dumps(event)))
            recovered.append(event)
        db.execute("UPDATE monitor_runs SET state='interrupted' WHERE state='running' AND run_id!=?", (run,))
        # Legacy runs may not have monitor_runs rows. Their terminal snapshot is
        # still recorded so the most recent saved status no longer says confirmed.
        old_runs = {event["run_id"] for event in recovered}
        for old_run in old_runs:
            old_snapshot = dict(run_id=old_run, generated_at_ms=now, state="interrupted", rows=[],
                events=[event for event in recovered if event["run_id"] == old_run], ended=[],
                recovered_by_run_id=run, execution_verified=False)
            db.execute("INSERT INTO signal_snapshots(generated_at_ms,run_id,payload) VALUES (?,?,?)",
                       (now, old_run, json.dumps(old_snapshot)))
    db.execute("INSERT INTO monitor_runs VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET state=excluded.state,last_seen_at_ms=excluded.last_seen_at_ms",
               (run, "stopped" if signals["state"] == "stopped" else "running", now))
    signals["recovered_events"] = recovered
