"""Transactional history rollups and retention of complete lifecycle episodes."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def ensure_rollups(db):
    db.execute("CREATE TABLE IF NOT EXISTS history_rollups (bucket_ms INTEGER NOT NULL, start_ms INTEGER NOT NULL, pair_id TEXT NOT NULL, policy_key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(bucket_ms,start_ms,pair_id,policy_key))")
    db.execute("CREATE TABLE IF NOT EXISTS history_checkpoints (bucket_ms INTEGER PRIMARY KEY, last_id INTEGER NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS history_rollups_pair_time ON history_rollups(pair_id,start_ms)")


def rollup(db, bucket_ms=60000, max_snapshots=None):
    if not 1000 <= bucket_ms <= 86400000:
        raise ValueError("Bucket must be 1000–86400000 milliseconds")
    ensure_rollups(db)
    if "economics" not in tables(db):
        return 0
    checkpoint = db.execute("SELECT last_id FROM history_checkpoints WHERE bucket_ms=?", (bucket_ms,)).fetchone()
    last = checkpoint[0] if checkpoint else 0
    processed = 0
    # Fetch bounded batches before writing to the same connection.
    while True:
        limit = 200 if max_snapshots is None else min(200, max_snapshots-processed)
        if limit <= 0:
            break
        batch = db.execute("SELECT id,generated_at_ms,payload FROM economics WHERE id>? ORDER BY id LIMIT ?", (last, limit)).fetchall()
        if not batch:
            break
        for identity, timestamp, payload in batch:
            snapshot = json.loads(payload)
            policy = json.dumps(snapshot.get("policy", {}), sort_keys=True, separators=(",", ":"), allow_nan=False)
            key = sha256(policy.encode()).hexdigest()
            start = timestamp//bucket_ms*bucket_ms
            for item in snapshot.get("rows", []):
                address = (bucket_ms, start, item["id"], key)
                old = db.execute("SELECT payload FROM history_rollups WHERE bucket_ms=? AND start_ms=? AND pair_id=? AND policy_key=?", address).fetchone()
                value = json.loads(old[0]) if old else dict(pair_id=item["id"], asset=item.get("asset"),
                    bucket_ms=bucket_ms, start_ms=start, policy=json.loads(policy), policy_key=key,
                    observations=0, statuses={}, reasons={}, net_samples=0, net_sum_usdt=0,
                    net_min_usdt=None, net_max_usdt=None, positive_samples=0,
                    classification="conditional_scenario_observations_not_trades", execution_verified=False)
                value["observations"] += 1
                value["statuses"][item["status"]] = value["statuses"].get(item["status"], 0)+1
                for reason in item.get("reasons", []):
                    value["reasons"][reason] = value["reasons"].get(reason, 0)+1
                net = item.get("convergence_net_scenario_usdt")
                if item["status"] == "scenario_estimated" and net is not None:
                    if not isinstance(net, (float, int)):
                        raise ValueError("Invalid scenario number")
                    value["net_samples"] += 1
                    value["net_sum_usdt"] += net
                    value["net_min_usdt"] = net if value["net_min_usdt"] is None else min(value["net_min_usdt"], net)
                    value["net_max_usdt"] = net if value["net_max_usdt"] is None else max(value["net_max_usdt"], net)
                    value["positive_samples"] += net > 0
                value["net_mean_usdt"] = value["net_sum_usdt"]/value["net_samples"] if value["net_samples"] else None
                db.execute("INSERT INTO history_rollups VALUES (?,?,?,?,?) ON CONFLICT(bucket_ms,start_ms,pair_id,policy_key) DO UPDATE SET payload=excluded.payload",
                           (*address, json.dumps(value, ensure_ascii=False, allow_nan=False)))
            last, processed = identity, processed+1
        db.execute("INSERT INTO history_checkpoints VALUES (?,?) ON CONFLICT(bucket_ms) DO UPDATE SET last_id=excluded.last_id", (bucket_ms, last))
    return processed


def aggregate_history(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+"?mode=rw", uri=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        processed = rollup(db)
        return dict(processed_economic_snapshots=processed,
                    buckets=db.execute("SELECT count(*) FROM history_rollups").fetchone()[0])


def retention_plan(db, requested_cutoff):
    existing = tables(db)
    cutoff, protected = requested_cutoff, []
    if "signal_events" in existing:
        for episode, first, last, ended in db.execute("SELECT episode_id,min(timestamp_ms),max(timestamp_ms),sum(kind='ended') FROM signal_events GROUP BY episode_id"):
            if not ended or last >= requested_cutoff:
                cutoff = min(cutoff, max(0, first-3000))
                protected.append(episode)
    clauses = {}
    for name, stamp in (("snapshots", "collected_at_ms"), ("scans", "generated_at_ms"),
                        ("economics", "generated_at_ms"), ("signal_snapshots", "generated_at_ms")):
        if name in existing:
            # Keep high-water IDs so SQLite cannot reuse IDs after a full purge.
            anchor = "id NOT IN (SELECT max(id) FROM snapshots GROUP BY exchange)" if name == "snapshots" else f"id != (SELECT max(id) FROM {name})"
            clauses[name] = (f"{stamp} < ? AND {anchor}", (cutoff,))
    if "signal_events" in existing:
        clauses["signal_events"] = ("episode_id IN (SELECT episode_id FROM signal_events GROUP BY episode_id HAVING max(timestamp_ms) < ? AND sum(kind='ended') > 0)", (cutoff,))
    counts = {name: db.execute(f"SELECT count(*) FROM {name} WHERE {clause}", values).fetchone()[0]
              for name, (clause, values) in clauses.items()}
    return dict(requested_cutoff_ms=requested_cutoff, effective_cutoff_ms=cutoff,
                protected_episodes=len(protected), deletable_rows=counts), clauses


def maintain(database, cutoff_ms, apply=False, backup=None, compact=False):
    database = Path(database).resolve()
    if not database.is_file():
        raise ValueError("Database does not exist")
    if compact and not apply:
        raise ValueError("Compaction requires --apply")
    before_bytes = database.stat().st_size
    uri = database.as_uri()+("?mode=rw" if apply else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        if not apply:
            plan, _ = retention_plan(db, cutoff_ms)
            return dict(plan, applied=False, database_bytes=before_bytes)
        # A reserved write lock allows a separate reader to back up the committed
        # database, while preventing any writer from changing the deletion set.
        db.execute("BEGIN IMMEDIATE")
        try:
            plan, clauses = retention_plan(db, cutoff_ms)
            backup_path = Path(backup).resolve() if backup else database.with_name(database.stem+f".before-prune-{uuid4().hex}.sqlite3")
            if backup_path == database or backup_path.exists():
                raise ValueError("Backup must be a new path different from the database")
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation also guards against accidental overwrite.
            with backup_path.open("xb"):
                pass
            with closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True)) as source, closing(sqlite3.connect(backup_path)) as destination:
                source.backup(destination)
            processed = rollup(db)
            deleted = {}
            for name, (clause, values) in clauses.items():
                deleted[name] = db.execute(f"DELETE FROM {name} WHERE {clause}", values).rowcount
            db.commit()
        except BaseException:
            db.rollback()
            raise
        if compact:
            db.execute("VACUUM")
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
    return dict(plan, applied=True, deleted_rows=deleted, processed_economic_snapshots=processed,
                backup=str(backup_path), before_bytes=before_bytes, after_bytes=database.stat().st_size,
                compacted=compact, integrity=integrity)
