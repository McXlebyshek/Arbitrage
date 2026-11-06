"""Attribute sampled freshness losses on a completed, fixed-registry MEXC run."""
import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def classify(quote, evaluated):
    if quote is None:
        return "absent"
    if quote["connection_state"] != "streaming":
        return "disconnected"
    if quote["quality"] != "stale":
        return quote["quality"]
    receipt_age = evaluated - quote["received_at_ms"]
    transit = quote["received_at_ms"] - quote["source_timestamp_ms"]
    if receipt_age > 3000:
        return "old_receipt_and_late_on_arrival" if transit > 3000 else "old_receipt"
    return "recent_receipt_late_on_arrival" if transit > 3000 else "recent_receipt_source_expired"


class WireComparison:
    """Match by local millisecond clock. Equal-ms arrivals have ambiguous order."""
    def __init__(self, capture):
        self.start = capture["start_ms"] + 20000
        self.finish = capture["finish_ms"]
        self.frames = capture["frames"]
        self.arrivals = {s: [r[0] for r in rows] for s, rows in self.frames.items()}
        if any(times != sorted(times) for times in self.arrivals.values()):
            raise ValueError("wire receipt clock moved backwards")
        self.counts = {s: Counter() for s in self.frames}

    def observe(self, symbol, quote, evaluated, quality):
        if symbol not in self.frames or not self.start <= evaluated <= self.finish:
            return
        times = self.arrivals[symbol]
        left = bisect_left(times, evaluated - 3000)
        right = bisect_right(times, evaluated)
        rows = self.frames[symbol][left:right]
        counts = self.counts[symbol]
        counts["samples"] += 1
        if quality == "ok":
            counts["monitor_ok"] += 1
        elif not rows:
            counts["monitor_gap_no_wire_frame"] += 1
        elif quote and any(isinstance(stamp, int) and quote["source_timestamp_ms"] is not None
                           and quote["source_timestamp_ms"] < stamp
                           and -1000 <= evaluated - stamp <= 3000 for _, stamp, _ in rows):
            counts["monitor_gap_newer_fresh_wire_time"] += 1
        else:
            counts["monitor_gap_other_wire_frame"] += 1


def summarize(database, report, capture=None):
    cfg = report["config"]
    if not report.get("finished") or cfg["symbols"] != "REGISTERED":
        raise ValueError("requires a finished REGISTERED run")
    start = report["started_at_ms"]
    stats = report["exchanges"]["mexc"]
    width = min(cfg["group_size"], 5)
    counters, groups, group_symbols = {}, {}, {}
    history = {}
    events = []
    total = Counter()
    steady = 0
    wire = WireComparison(capture) if capture is not None else None
    sql = ("SELECT collected_at_ms,payload FROM snapshots WHERE exchange='mexc' "
           "AND collected_at_ms BETWEEN ? AND ? ORDER BY collected_at_ms,id")
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        for stamp, payload in db.execute(sql, (start, report["finished_at_ms"])):
            snap = json.loads(payload)
            status = snap["status"]
            if status["state"] == "stopped":
                continue
            elapsed = (stamp - start) / 1000
            warm = elapsed >= report["warmup_seconds"]
            members = sorted(m["id"] for m in snap["instruments"])
            if len(members) != stats["max_selected_markets"]:
                if warm:
                    raise ValueError("incomplete membership after warmup")
                continue
            quotes = {q["exchange_symbol"]: q for q in snap["quotes"]}
            connections = {g["connection_id"]: g for g in status["connection_statuses"]}
            row_counts = Counter()
            for i in range(0, len(members), width):
                gid = i // width
                group = connections[gid]
                symbols = members[i:i+width]
                if group_symbols.setdefault(gid, symbols) != symbols or group["markets"] != len(symbols):
                    raise ValueError("group membership changed")
                state = {k: group[k] for k in ("reconnects", "book_resyncs", "last_failure", "last_book_resync_reason")}
                if history.get(gid) != state:
                    history[gid] = state
                    events.append(dict(seconds=round(elapsed, 2), group=gid, **state))
                if not warm:
                    continue
                tally = Counter()
                for symbol in symbols:
                    key = classify(quotes.get(symbol), status["quality_evaluated_at_ms"])
                    if wire is not None:
                        wire.observe(symbol, quotes.get(symbol), status["quality_evaluated_at_ms"], key)
                    counters.setdefault(symbol, Counter())[key] += 1
                    tally[key] += 1
                groups.setdefault(gid, Counter()).update(tally)
                row_counts.update(tally)
            if warm:
                if row_counts["ok"] != status["usable_quotes"] or sum(row_counts.values()) != status["markets"]:
                    raise ValueError("published coverage disagrees with quote rows")
                steady += 1
                total.update(row_counts)
    if not steady:
        raise ValueError("no post-warmup samples")
    result = dict(run_id=report["run_id"], samples=steady, quality=total, groups=groups,
                  group_members=group_symbols, symbols=counters, sampled_group_transitions=events)
    if wire is not None:
        if not all(row["samples"] for row in wire.counts.values()):
            raise ValueError("wire capture does not overlap sampled run")
        result["wire"] = dict(start_ms=capture["start_ms"], finish_ms=wire.finish,
                              frames={s: len(rows) for s, rows in wire.frames.items()}, overlap=wire.counts)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--wire", type=Path, help="optional concurrent validate_mexc_cadence capture")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output file already exists")
    report = json.loads((args.directory / "monitor-report.json").read_text(encoding="utf-8"))
    wire = json.loads(args.wire.read_text(encoding="utf-8")) if args.wire else None
    result = summarize(args.directory / "market.sqlite3", report, wire)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2)
    print(json.dumps(result["quality"], indent=2))
