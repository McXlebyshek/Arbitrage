"""Validate retention on a separate copy of a completed public run. No API calls."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from arbitrage.history import read_buckets
from arbitrage.storage import aggregate_history, maintain


def validate(source, target):
    if target.exists():
        raise ValueError("Validation target must not exist")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro", uri=True)) as original, closing(sqlite3.connect(target)) as copy:
        original.backup(copy)
    first = aggregate_history(target)
    baseline = read_buckets(target, limit=1000)
    with closing(sqlite3.connect(target)) as db:
        stamps = [row[0] for row in db.execute("SELECT generated_at_ms FROM economics ORDER BY generated_at_ms")]
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("economics", "signal_events", "signal_snapshots", "snapshots", "scans")}
    cutoff = stamps[len(stamps)//2]
    before_preview = sha256(target.read_bytes()).hexdigest()
    preview = maintain(target, cutoff)
    assert before_preview == sha256(target.read_bytes()).hexdigest(), "Preview changed database"
    applied = maintain(target, cutoff, apply=True, compact=True)
    after = read_buckets(target, limit=1000)
    assert baseline == after, "Retained aggregates changed"
    assert aggregate_history(target)["processed_economic_snapshots"] == 0, "Rollup was counted twice"
    with closing(sqlite3.connect(applied["backup"])) as backup:
        backup_counts = {table: backup.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in counts}
    assert counts == backup_counts, "Backup lost raw data"
    with closing(sqlite3.connect(target)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        after_counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in counts}
    return dict(source=str(source.resolve()), target=str(target.resolve()), backfill=first,
                before_counts=counts, after_counts=after_counts, preview=preview, applied=applied,
                aggregate_bucket_count=len(baseline), aggregate_observations=sum(row["observations"] for row in baseline),
                preview_unchanged=True, aggregates_identical=True, backup_counts_identical=True,
                second_backfill_processed=0, integrity_check="ok")


if __name__ == "__main__":
    result = validate(Path("data/signals-check.sqlite3"), Path("data/history-retention-check.sqlite3"))
    Path("docs/reports/history-storage-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
