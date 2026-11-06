"""Bounded run diagnostics. Measurements describe this process, not trading uptime."""
from collections import deque
import os
from pathlib import Path
import time
import uuid


def rss_bytes():
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("peak", "working", "peak_paged", "paged",
                "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return counters.working


def file_size(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered)-1, int((len(ordered)-1)*fraction))] if ordered else None


class RunReport:
    def __init__(self, database, warmup_seconds=60, config=None):
        self.started = time.monotonic()
        self.cpu_started = time.process_time()
        self.database = Path(database)
        self.database_start_bytes = file_size(self.database)
        self.warmup_seconds = warmup_seconds
        self.publish_ms, self.lag_ms = deque(maxlen=10000), deque(maxlen=10000)
        self.exchanges = {}
        self.warm_baseline = None
        self.data = {"run_id": str(uuid.uuid4()), "started_at_ms": int(time.time()*1000),
            "finished": False, "config": config or {}, "warmup_seconds": warmup_seconds,
            "observations": 0, "max_spread_rows": 0, "max_eligible_quotes": 0,
            "peak_rss_bytes": None, "last_rss_bytes": None, "peak_publish_ms": 0, "peak_loop_lag_ms": 0}

    def observe(self, snapshots, scanner, publish_ms, lag_ms):
        elapsed = time.monotonic() - self.started
        memory = rss_bytes()
        self.data["last_rss_bytes"] = memory
        if memory is not None:
            self.data["peak_rss_bytes"] = max(self.data["peak_rss_bytes"] or 0, memory)
        self.data["observations"] += 1
        self.data["max_spread_rows"] = max(self.data["max_spread_rows"], scanner.get("displayed_pairs", 0))
        self.data["max_eligible_quotes"] = max(self.data["max_eligible_quotes"], scanner.get("eligible_quotes", 0))
        self.publish_ms.append(publish_ms)
        self.lag_ms.append(lag_ms)
        self.data["peak_publish_ms"] = max(self.data["peak_publish_ms"], publish_ms)
        self.data["peak_loop_lag_ms"] = max(self.data["peak_loop_lag_ms"], lag_ms)
        if elapsed >= self.warmup_seconds and self.warm_baseline is None:
            self.warm_baseline = (elapsed, time.process_time(), memory)
        for snapshot in snapshots:
            status = snapshot["status"]
            exchange = status["exchange"]
            stats = self.exchanges.setdefault(exchange, {"observations": 0, "steady_observations": 0,
                "steady_streaming_observations": 0, "steady_fresh_sum": 0, "steady_selected_sum": 0,
                "max_selected_markets": 0, "max_received_quotes": 0, "max_fresh_quotes": 0,
                "max_connections": 0, "metadata_generation": 0, "reconnects": 0, "book_resyncs": 0,
                "group_restarts": 0, "metadata_failures": 0, "recent_failures": []})
            stats["observations"] += 1
            for target, source in (("max_selected_markets", "markets"), ("max_received_quotes", "quotes"),
                                   ("max_fresh_quotes", "usable_quotes"), ("max_connections", "connections")):
                stats[target] = max(stats[target], status.get(source, 0))
            for key in ("metadata_generation", "reconnects", "book_resyncs", "group_restarts", "metadata_failures"):
                stats[key] = max(stats[key], status.get(key, 0))
            for key in ("received_stale_updates", "out_of_order_updates"):
                stats[key] = max(stats.get(key,0), status.get(key,0))
            groups = status.get('connection_statuses',[])
            stats['source_lag_p95_ms_recent_by_connection'] = [group.get('source_lag_p95_ms') for group in groups]
            stats['event_lag_p95_ms_recent_by_connection'] = [group.get('event_lag_p95_ms') for group in groups]
            stats['source_to_event_p95_ms_recent_by_connection'] = [group.get('source_to_event_p95_ms') for group in groups]
            stats["last_state"] = status["state"]
            stats["last_omitted_markets"] = status.get("omitted_markets", 0)
            failures = [status.get("metadata_error"), status.get("last_metadata_failure")]
            failures.extend(group.get("last_failure") for group in status.get("connection_statuses", []))
            for failure in failures:
                if failure and failure not in stats["recent_failures"]:
                    stats["recent_failures"] = (stats["recent_failures"] + [failure])[-20:]
            if elapsed >= self.warmup_seconds:
                stats["steady_observations"] += 1
                stats["steady_streaming_observations"] += status["state"] == "streaming"
                stats["steady_fresh_sum"] += status.get("usable_quotes", 0)
                stats["steady_selected_sum"] += status.get("markets", 0)

    def document(self):
        elapsed = max(time.monotonic() - self.started, 0.000001)
        cpu = time.process_time() - self.cpu_started
        result = dict(self.data, elapsed_seconds=elapsed, cpu_seconds=cpu,
                      cpu_pct_one_core=100 * cpu / elapsed,
                      database_bytes=file_size(self.database),
                      database_growth_bytes=file_size(self.database)-self.database_start_bytes,
                      publish_p95_ms_recent=percentile(self.publish_ms, 0.95),
                      loop_lag_p95_ms_recent=percentile(self.lag_ms, 0.95), percentile_window_limit=10000,
                      exchanges={})
        if self.warm_baseline:
            seconds, baseline_cpu, memory = self.warm_baseline
            period = elapsed-seconds
            result["steady_cpu_pct_one_core"] = 100*(time.process_time()-baseline_cpu)/period if period > 0 else None
            result["rss_at_warmup_bytes"] = memory
        for exchange, stats in self.exchanges.items():
            denominator = stats["steady_selected_sum"]
            result["exchanges"][exchange] = dict(stats,
                mean_sampled_fresh_fraction=stats["steady_fresh_sum"]/denominator if denominator else None)
        return result

    def write(self, path):
        from .atomic_json import write_json
        write_json(path,self.document(),indent=2)

    def finish(self, reason, exit_code, stopped):
        self.data.update(finished=True, finished_at_ms=int(time.time()*1000),
                         finish_reason=reason, exit_code=exit_code,
                         shutdown_states={s["status"]["exchange"]: s["status"]["state"] for s in stopped})
