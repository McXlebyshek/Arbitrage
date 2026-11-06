"""Bounded background retention with verified row archives in a managed directory."""
import asyncio
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from .storage import tables, retention_plan

DAY = 86400000


@dataclass(frozen=True)
class CarePolicy:
    raw_days: float = 7
    summary_days: float = 30
    funding_days: float = 90
    backup_count: int = 3
    backup_max_bytes: int = 512*1024*1024
    batch_rows: int = 200

    def __post_init__(self):
        import math
        if (not all(math.isfinite(x) for x in (self.raw_days, self.summary_days, self.funding_days))
                or not 0 < self.raw_days <= self.summary_days or self.funding_days < 31
                or not 1 <= self.backup_count <= 100 or self.backup_max_bytes < 1 or not 1 <= self.batch_rows <= 10000):
            raise ValueError("Invalid automatic history policy")


def backup_root(database):
    database = Path(database).resolve()
    identity = sha256(os.path.normcase(str(database)).encode()).hexdigest()[:12]
    root = database.parent/"managed-backups"/(database.stem+"-"+identity)
    if not root.resolve().is_relative_to(database.parent) or root.is_symlink() or root.parent.is_symlink():
        raise ValueError("Managed backup directory escaped the database directory")
    return root


def owned_file(root, name):
    if not re.fullmatch(r"backup-[0-9a-f]{32}\.sqlite3", name):
        raise ValueError("Invalid managed backup name")
    path = root/name
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError("Backup path escaped its managed directory")
    return path


def manifest(root):
    path = root/"manifest.json"
    if path.is_symlink():
        raise ValueError("Managed manifest cannot be a symlink")
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not isinstance(entries, list):
        raise ValueError("Invalid backup manifest")
    for entry in entries:
        owned_file(root, entry["name"])
        if entry.get("verified") is not True:
            raise ValueError("Unverified managed backup")
    if len({entry["name"] for entry in entries}) != len(entries):
        raise ValueError("Duplicate managed backup entries")
    return entries


def write_manifest(root, entries):
    root.mkdir(parents=True, exist_ok=True)
    temporary = root/("manifest-"+uuid4().hex+".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(entries, indent=2))
    temporary.replace(root/"manifest.json")


def rotate_backups(root, policy):
    entries = [entry for entry in manifest(root) if owned_file(root, entry["name"]).is_file()]
    # Manifest insertion order is authoritative even if the wall clock moves back.
    deleted = []
    def size():
        return sum(owned_file(root, entry["name"]).stat().st_size for entry in entries)
    # Always retain the latest verified backup, even if it exceeds the budget.
    while len(entries) > 1 and (len(entries) > policy.backup_count or size() > policy.backup_max_bytes):
        entry = entries.pop(0)
        path = owned_file(root, entry["name"])
        path.unlink()
        deleted.append(entry["name"])
    if root.exists():
        write_manifest(root, entries)
    return dict(retained=len(entries), retained_bytes=size(), deleted=deleted,
                budget_exceeded=size() > policy.backup_max_bytes,
                unmanaged_files=sum(path.name not in {entry["name"] for entry in entries}|{"manifest.json", "manifest.tmp"}
                                    for path in root.iterdir()) if root.exists() else 0)


def write_archive(database, root, now, selected, policy):
    entries = manifest(root)
    root.mkdir(parents=True, exist_ok=True)
    name = "backup-"+uuid4().hex+".sqlite3"
    target = owned_file(root, name)
    with target.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(target)) as destination, destination:
            destination.execute("CREATE TABLE archive_info(source TEXT,created_at_ms INTEGER,format TEXT)")
            destination.execute("INSERT INTO archive_info VALUES (?,?,?)", (str(database), now, "retention_rows_v1"))
            destination.execute("CREATE TABLE archived_rows(table_name TEXT,source_rowid INTEGER,columns_json TEXT,row_json TEXT,PRIMARY KEY(table_name,source_rowid))")
            for table_name, data in selected.items():
                for rowid, values in data["rows"]:
                    destination.execute("INSERT INTO archived_rows VALUES (?,?,?,?)", (table_name, rowid,
                        json.dumps(data["columns"]), json.dumps(values, ensure_ascii=False, allow_nan=False)))
        with closing(sqlite3.connect(target.as_uri()+"?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Backup verification failed")
            if check.execute("SELECT count(*) FROM archived_rows").fetchone()[0] != sum(len(data["rows"]) for data in selected.values()):
                raise ValueError("Archive row count mismatch")
        if target.stat().st_size > policy.backup_max_bytes:
            raise ValueError("Archive batch exceeds backup byte budget; increase budget or reduce batch rows")
        entries.append(dict(name=name, created_at_ms=now, verified=True, size_bytes=target.stat().st_size))
        write_manifest(root, entries)
        return target
    except BaseException:
        owned_file(root, name).unlink(missing_ok=True)
        raise


def prune_cycle(database, policy, now=None):
    now = int(time.time()*1000) if now is None else now
    database = Path(database).resolve()
    if not database.is_file():
        return dict(state="awaiting_database")
    root = backup_root(database)
    with closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True, timeout=0.05)) as db:
        began = time.monotonic()
        db.set_progress_handler(lambda: int(time.monotonic()-began > 0.1), 1000)
        db.execute("BEGIN")
        existing = tables(db)
        plan, clauses = retention_plan(db, int(now-policy.raw_days*DAY))
        summary_cutoff = min(int(now-policy.summary_days*DAY), plan["effective_cutoff_ms"])
        funding_cutoff = int(now-policy.funding_days*DAY)
        checkpoint = db.execute("SELECT last_id FROM history_checkpoints WHERE bucket_ms=60000").fetchone() if "history_checkpoints" in existing else None
        pending = db.execute("SELECT count(*) FROM economics WHERE id>?", (checkpoint[0] if checkpoint else 0,)).fetchone()[0] if "economics" in existing else 0
        if pending:
            return dict(state="aggregation_pending", pending_snapshots=pending)
        selected = {}
        def capture(name, where, values):
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({name})")]
            rows = db.execute(f"SELECT rowid,* FROM {name} WHERE {where} ORDER BY rowid LIMIT ?", (*values, policy.batch_rows)).fetchall()
            selected[name] = dict(columns=columns, rows=[(row[0], list(row[1:])) for row in rows])
        for name, (clause, values) in clauses.items():
            if name == "signal_events":
                eligible = db.execute("SELECT episode_id,count(*) FROM signal_events GROUP BY episode_id HAVING max(timestamp_ms)<? AND sum(kind='ended')>0 ORDER BY min(timestamp_ms)", (plan["effective_cutoff_ms"],)).fetchall()
                chosen, count = [], 0
                for episode, size in eligible:
                    if count+size <= policy.batch_rows:
                        chosen.append(episode)
                        count += size
                capture(name, "episode_id IN ("+",".join("?" for _ in chosen)+")", chosen)
            else:
                capture(name, clause, values)
        if "history_rollups" in existing:
            capture("history_rollups", "start_ms+bucket_ms<=?", (summary_cutoff,))
        if "funding_history_events" in existing:
            capture("funding_history_events", "timestamp_ms<?", (funding_cutoff,))
    if not any(data["rows"] for data in selected.values()):
        return dict(state="batch_limit_blocked" if sum(plan["deletable_rows"].values()) else "idle", plan=plan, backups=rotate_backups(root, policy),
                    unprocessed_raw_rows=sum(plan["deletable_rows"].values()))
    # Only the selected rows are archived, outside the main write transaction.
    target = write_archive(database, root, now, selected, policy)
    rotate_backups(root, policy)
    with closing(sqlite3.connect(database.as_uri()+"?mode=rw", uri=True, timeout=0.05)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            began = time.monotonic()
            db.set_progress_handler(lambda: int(time.monotonic()-began > 0.1), 1000)
            current, _ = retention_plan(db, int(now-policy.raw_days*DAY))
            if current["effective_cutoff_ms"] < plan["effective_cutoff_ms"]:
                db.rollback()
                return dict(state="protection_changed", backup=str(target), backups=rotate_backups(root, policy))
            deleted = {}
            for name, data in selected.items():
                columns, rows = data["columns"], data["rows"]
                skip_episodes = set()
                if name == "signal_events":
                    index = columns.index("episode_id")
                    for episode in {values[index] for _, values in rows}:
                        ids = {identity for identity, values in rows if values[index] == episode}
                        actual = {row[0] for row in db.execute("SELECT rowid FROM signal_events WHERE episode_id=?", (episode,))}
                        if actual != ids:
                            skip_episodes.add(episode)
                deleted[name] = 0
                for identity, values in rows:
                    if name == "signal_events" and values[columns.index("episode_id")] in skip_episodes:
                        continue
                    match = " AND ".join('"'+column.replace('"','""')+'" IS ?' for column in columns)
                    deleted[name] += db.execute(f"DELETE FROM {name} WHERE rowid=? AND {match}", (identity, *values)).rowcount
            db.commit()
        except BaseException:
            db.set_progress_handler(None, 0)
            db.rollback()
            raise
    return dict(state="pruned", plan=plan, deleted_rows=deleted, backup=str(target),
                backups=rotate_backups(root, policy), file_bytes=database.stat().st_size,
                note="Archive contains only expired rows; freed pages are reused; no online VACUUM")


def read_archive(path, table=None, limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError("Archive row limit must be 1–1000")
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)) as db:
        source, created, version = db.execute("SELECT source,created_at_ms,format FROM archive_info").fetchone()
        if version != "retention_rows_v1":
            raise ValueError("Unknown row archive format")
        query, params = "SELECT table_name,source_rowid,columns_json,row_json FROM archived_rows", []
        if table:
            query += " WHERE table_name=?"
            params.append(table)
        rows = db.execute(query+" ORDER BY table_name,source_rowid LIMIT ?", params+[limit]).fetchall()
    return dict(source=source, created_at_ms=created, format=version,
                rows=[dict(table=name, source_rowid=identity, values=dict(zip(json.loads(columns), json.loads(values))))
                      for name, identity, columns, values in rows])


class AutoCare:
    def __init__(self, database, policy=None, interval=3600, funding_hours=0, exchanges=(), registry=None, assets=None, funding_delay=30):
        self.database, self.policy = Path(database), policy or CarePolicy()
        self.interval, self.funding_hours = interval, funding_hours
        self.exchanges, self.registry, self.assets = exchanges, registry, assets
        self.funding_delay = funding_delay
        self.status = dict(state="starting", cycles=0, last_result=None, last_error=None,
                           funding=dict(state="disabled" if not funding_hours else "warming_up"))
        self.report_path = Path(str(self.database)+".maintenance.json")

    def snapshot(self):
        return deepcopy(self.status)

    def report(self):
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(str(self.report_path)+".tmp")
        temporary.write_text(json.dumps(dict(self.status, updated_at_ms=int(time.time()*1000)), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.report_path)

    async def funding_loop(self):
        from .funding_history import collect, read_reports
        from .scanner import Registry
        await asyncio.sleep(self.funding_delay)
        while True:
            try:
                required = {key for key, binding in (self.registry or Registry.load()).bindings.items()
                            if key[0] in self.exchanges and (not self.assets or binding["base"] in self.assets)}
                cached = read_reports(self.database) if self.database.is_file() else []
                fresh = {(row["exchange"], row["native_id"]) for row in cached if not row["error"]
                         and 0 <= int(time.time()*1000)-row["end_ms"] < self.funding_hours*3600000}
                if required and required <= fresh:
                    self.status["funding"] = dict(state="cached_fresh", contracts=len(required))
                    self.report()
                    await asyncio.sleep(min(self.funding_hours*3600, 3600))
                    continue
                # A failed import is retried at the next configured boundary, avoiding
                # repeated catalog/history bursts while an API is unavailable.
                self.status["funding"] = dict(state="refreshing")
                reports = await collect(self.database, self.exchanges, self.assets, self.registry)
                errors = sum(bool(row.get("error")) for row in reports)
                self.status["funding"] = dict(state="partial" if errors else "updated", contracts=len(reports),
                    errors=errors, completed_at_ms=int(time.time()*1000))
            except Exception as exc:
                self.status["funding"] = dict(state="error", error=f"{type(exc).__name__}: {exc}")
            self.report()
            await asyncio.sleep(self.funding_hours*3600)

    async def run(self):
        funding = asyncio.create_task(self.funding_loop()) if self.funding_hours else None
        inflight = None
        try:
            while True:
                self.status["state"] = "running"
                try:
                    inflight = asyncio.create_task(asyncio.to_thread(prune_cycle, self.database, self.policy))
                    result = await asyncio.shield(inflight)
                    self.status.update(state="waiting", last_result=result, last_error=None,
                                       cycles=self.status["cycles"]+1, last_completed_at_ms=int(time.time()*1000))
                except Exception as exc:
                    self.status.update(state="waiting", last_error=f"{type(exc).__name__}: {exc}", cycles=self.status["cycles"]+1)
                self.report()
                followup = self.status.get("last_result") or {}
                delay = min(self.interval, 5) if sum(followup.get("deleted_rows", {}).values()) else self.interval
                await asyncio.sleep(delay)
        finally:
            if funding:
                funding.cancel()
                await asyncio.gather(funding, return_exceptions=True)
                if self.status["funding"]["state"] in ("warming_up", "refreshing"):
                    self.status["funding"]["state"] = "stopped"
            if inflight and not inflight.done():
                await asyncio.gather(inflight, return_exceptions=True)
            self.status["state"] = "stopped"
            self.report()
