"""Exercise automatic retention on a copy of a completed run, with a simulated date."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from arbitrage.autocare import CarePolicy, backup_root, manifest, prune_cycle, read_archive, rotate_backups
from arbitrage.history import read_buckets
from arbitrage.storage import aggregate_history


def validate(source, target):
    if target.exists():
        raise ValueError("Validation target must be new")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro", uri=True)) as original, closing(sqlite3.connect(target)) as copy:
        original.backup(copy)
    aggregate_history(target)
    baseline = read_buckets(target, limit=1000)
    names = ("snapshots", "scans", "economics", "signal_snapshots", "signal_events")
    originals = {}
    with closing(sqlite3.connect(target)) as db:
        asof = db.execute("SELECT max(generated_at_ms) FROM economics").fetchone()[0]+10*86400000
        for name in names:
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({name})")]
            originals[name] = {row[0]: dict(zip(columns, row[1:])) for row in db.execute(f"SELECT rowid,* FROM {name}")}
    policy = CarePolicy(backup_count=2, backup_max_bytes=64*1024*1024)
    root = backup_root(target)
    root.mkdir(parents=True, exist_ok=True)
    unknown = root/"personal.sqlite3"
    unknown.write_text("not managed", encoding="utf-8")
    results, matched = [], 0
    for _ in range(10):
        result = prune_cycle(target, policy, asof)
        results.append(result)
        if result["state"] == "idle":
            break
        assert result["state"] == "pruned", result
        archived = read_archive(result["backup"], limit=1000)["rows"]
        assert len(archived) == sum(result["deleted_rows"].values())
        for row in archived:
            assert originals[row["table"]][row["source_rowid"]] == row["values"]
        matched += len(archived)
    assert results[-1]["state"] == "idle"
    assert read_buckets(target, limit=1000) == baseline
    before_rotation = len(manifest(root))
    rotation = rotate_backups(root, CarePolicy(backup_count=1, backup_max_bytes=64*1024*1024))
    assert rotation["retained"] == 1
    assert unknown.read_text(encoding="utf-8") == "not managed"
    with closing(sqlite3.connect(target)) as db:
        after_counts = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in names}
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"
        free_bytes = db.execute("PRAGMA freelist_count").fetchone()[0]*db.execute("PRAGMA page_size").fetchone()[0]
    return dict(source=str(source.resolve()), target=str(target.resolve()), simulated_now_ms=asof,
                actual_long_run=False, before_counts={name: len(rows) for name, rows in originals.items()},
                after_counts=after_counts, cycles=results, archived_rows_verified_exactly=matched,
                minute_buckets_retained=len(baseline), observations_retained=sum(row["observations"] for row in baseline),
                aggregates_identical=True, archives_before_rotation=before_rotation, rotation=rotation,
                unmanaged_file_preserved=True, integrity=integrity, reusable_free_bytes=free_bytes,
                database_file_bytes=target.stat().st_size)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/signals-check.sqlite3"))
    parser.add_argument("--target", type=Path, default=Path("data/auto-care-validation.sqlite3"))
    parser.add_argument("--report", type=Path, default=Path("data/auto-care-validation-report.json"))
    args = parser.parse_args()
    result = validate(args.source, args.target)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
