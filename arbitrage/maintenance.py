"""Preview or apply history retention, with backup and minute rollups."""
import argparse
import json
from pathlib import Path
import math
import sqlite3
import time

from .storage import maintain, aggregate_history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/market.sqlite3"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--keep-days", type=float, default=7)
    group.add_argument("--before-ms", type=int, help="Explicit exclusive raw history cutoff, Unix milliseconds")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--aggregate-only", action="store_true", help="Backfill rollups without deleting raw data; requires --apply")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--compact", action="store_true", help="Reclaim free pages with VACUUM after pruning")
    args = parser.parse_args()
    if not math.isfinite(args.keep_days) or args.keep_days <= 0:
        parser.error("--keep-days must be finite and positive")
    cutoff = args.before_ms if args.before_ms is not None else int(time.time()*1000-args.keep_days*86400000)
    if cutoff < 0 or cutoff > int(time.time()*1000):
        parser.error("Cutoff must be a past nonnegative Unix timestamp")
    try:
        if args.aggregate_only:
            if not args.apply or args.compact or args.backup:
                parser.error("--aggregate-only requires --apply and cannot use --compact or --backup")
            result = aggregate_history(args.database)
        else:
            result = maintain(args.database, cutoff, args.apply, args.backup, args.compact)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
