"""Read-only comparison of saved economic runs, without exchange requests."""
import json
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
import sqlite3
from statistics import median


def summarize(name):
    with closing(sqlite3.connect(f"file:data/{name}.sqlite3?mode=ro", uri=True)) as db:
        items = [json.loads(row[0]) for row in db.execute("SELECT payload FROM economics ORDER BY id")]
    active = [item for item in items if item.get("selection")]
    rows = [row for item in active for row in item["rows"]]
    reasons = Counter(reason for row in rows for reason in row.get("reasons", []))
    ages = defaultdict(list)
    for item in active:
        for leg in item.get("inputs", []):
            stamp = leg["book"].get("timestamp")
            if stamp is not None:
                ages[leg["exchange"]].append(item["generated_at_ms"]-stamp)
    report = json.loads(Path(f"data/{name}-report.json").read_text(encoding="utf-8"))
    return {"report": report, "active_snapshots": len(active), "evaluated_rows": len(rows),
            "statuses": dict(Counter(row["status"] for row in rows)), "reasons": dict(reasons),
            "time_block_fraction": (reasons["depth_stale"]+reasons["depth_time_skew"])/max(1, len(rows)),
            "assets": sorted({row["asset"] for row in rows}),
            "depth_age_ms_by_venue": {venue: {"samples": len(values), "median": median(values),
                "p90": sorted(values)[int(0.9*(len(values)-1))]} for venue, values in ages.items()},
            "last_funding_coverage": active[-1].get("funding_coverage"),
            "observed_errors": sorted({item["last_error"] for item in active if item.get("last_error")})}


if __name__ == "__main__":
    result = {"baseline_0_7": summarize("funding-selection"),
              "independent_workers_0_8": summarize("depth-scheduler"),
              "limitations": "Sequential live runs with different markets/candidates; not a controlled benchmark. "
                             "The main run precedes addition of error backoff; it observed no errors. "
                             "The final deployed version is checked separately."}
    target = Path("docs/reports/depth-scheduler-summary.json")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, value in result.items():
        if isinstance(value, dict):
            print(name, value["statuses"], value["reasons"], round(value["time_block_fraction"]*100, 1))
