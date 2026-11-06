"""Replay captured MEXC depth at observed receipt times; never feeds the scanner."""
import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
from statistics import median

from arbitrage.mexc_book import MexcBook, amount
from arbitrage.models import normalize
from validate_mexc_full_capture import SYMBOLS


def full_top(data):
    sides = []
    for key in ("bids", "asks"):
        rows = data[key]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 5:
            raise ValueError("unexpected full depth length")
        levels = [(amount(row[0]), amount(row[1])) for row in rows]
        if any(p <= 0 or size <= 0 for p, size in levels):
            raise ValueError("invalid full depth level")
        prices = [p for p, _ in levels]
        if prices != sorted(set(prices), reverse=key == "bids"):
            raise ValueError("unordered full depth")
        sides.append(levels[0])
    (bid, bs), (ask, ass) = sides
    if bid >= ask:
        raise ValueError("crossed full book")
    return bid, ask, bs, ass


class Comparison:
    def __init__(self, symbol):
        self.market = dict(id=symbol, symbol=symbol, base=symbol.split("_")[0], quote="USDT")
        self.book = MexcBook()
        self.quotes = {}
        self.versions = {"full": {}, "incremental": {}}
        self.counts = Counter()
        self.time_keys = Counter()
        self.examples = {}
        self.only_examples = []
        self.last_full = None
        self.before_seed = []

    def update(self, mode, msg, received):
        data = msg["data"]
        if mode == "seed":
            self.book.initialize(data)
            for pending, arrival in self.before_seed:
                self.update("incremental", pending, arrival)
            self.before_seed.clear()
            return
        if mode == "incremental" and self.book.version is None:
            self.before_seed.append((msg, received))
            return
        self.counts[mode + "_frames"] += 1
        source = next((k for k in ("cts", "ts") if data.get(k)), None)
        stamp = data[source] if source else msg.get("ts")
        self.time_keys[f"{mode}.{('data.' + source) if source else 'envelope.ts'}"] += 1
        if mode == "incremental":
            quote = self.book.apply(msg, self.market, received)
            if quote is None:
                return
            version = self.book.version
            bid, ask = max(self.book.bids), min(self.book.asks)
            top = (bid, ask, self.book.bids[bid], self.book.asks[ask])
        else:
            if source != "cts" or type(stamp) is not int or stamp <= 0:
                raise ValueError("full snapshot lacks its own valid cts")
            top = full_top(data)
            version = data.get("version")
            if type(version) is not int or version <= 0:
                raise ValueError("invalid full version")
            if self.last_full is not None and version <= self.last_full:
                if version < self.last_full or self.versions[mode][version][0] != top:
                    raise ValueError("full version reversed or conflicting")
                self.counts["full_duplicate_versions"] += 1
                return
            self.last_full = version
            quote = normalize("mexc", self.market, dict(bid=top[0], ask=top[1], timestamp=stamp),
                              received, "diagnostic_full")
        self.versions[mode][version] = (top, stamp)
        self.quotes[mode] = quote
        if mode not in self.examples:
            self.examples[mode] = dict(version=version, stamp=stamp, received=received,
                                       fields=sorted(data), top=list(map(str, top)))

    def sample(self, now):
        self.counts["samples"] += 1
        fresh = {k: self.quotes.get(k) is not None and self.quotes[k].quality(now) == "ok"
                 for k in self.versions}
        for mode, ok in fresh.items():
            self.counts[mode + "_fresh"] += ok
        self.counts["full_only"] += fresh["full"] and not fresh["incremental"]
        if fresh["full"] and not fresh["incremental"]:
            if "incremental" not in self.quotes:
                relation = "without_incremental"
            elif self.last_full > self.book.version:
                relation = "newer_version"
            elif self.last_full == self.book.version:
                relation = "same_version"
            else:
                relation = "older_version"
            self.counts["full_only_" + relation] += 1
            if len(self.only_examples) < 5 and "incremental" in self.quotes:
                self.only_examples.append(dict(sample_ms=now, full_version=self.last_full,
                    incremental_version=self.book.version, full_source_ms=self.quotes["full"].source_timestamp_ms,
                    incremental_source_ms=self.quotes["incremental"].source_timestamp_ms))
        self.counts["incremental_only"] += fresh["incremental"] and not fresh["full"]
        self.counts["neither"] += not any(fresh.values())

    def result(self):
        full, inc = self.versions["full"], self.versions["incremental"]
        common = sorted(full.keys() & inc.keys())
        match = Counter()
        mismatches = []
        deltas = []
        for version in common:
            a, ta = full[version]
            b, tb = inc[version]
            match["equal_prices"] += a[:2] == b[:2]
            match["equal_prices_sizes"] += a == b
            match["equal_source_time"] += ta == tb
            if type(ta) is int and type(tb) is int:
                deltas.append(ta - tb)
            if a != b and len(mismatches) < 5:
                mismatches.append(dict(version=version, full=list(map(str,a)), incremental=list(map(str,b))))
        return dict(counts=self.counts, unique_versions={k:len(v) for k,v in self.versions.items()},
                    matched_versions=len(common), matches=match, mismatches=mismatches,
                    time_keys=self.time_keys, examples=self.examples, full_only_examples=self.only_examples,
                    full_minus_incremental_ms=dict(min=min(deltas), median=median(deltas), max=max(deltas),
                        positive=sum(d > 0 for d in deltas), pairs=len(deltas)) if deltas else None)


def summarize(capture, symbols=SYMBOLS):
    states = {s:Comparison(s) for s in symbols}
    rows = capture["records"]
    if any(left[1] > right[1] for left, right in zip(rows, rows[1:])):
        raise ValueError("receipt clock moved backwards")
    index = 0
    channels = Counter()

    def consume(until):
        nonlocal index
        while index < len(rows) and rows[index][1] <= until:
            mode, received, msg = rows[index]
            index += 1
            channel = msg.get("channel")
            channels[f"{mode}:{channel}"] += 1
            if mode != "seed" and channel != {"full":"push.depth.full","incremental":"push.depth"}[mode]:
                continue
            if msg["symbol"] in states:
                states[msg["symbol"]].update(mode, msg, received)

    start = capture["start_ms"]
    for second in range(20, capture["duration"]):
        now = start + 1000 * second
        consume(now)
        for state in states.values():
            state.sample(now)
    consume(start + capture["duration"] * 1000)
    for symbol, state in states.items():
        if not (state.versions["full"] and state.versions["incremental"]):
            raise ValueError(f"missing verified full or incremental data for {symbol}")
    return dict(start_ms=start, duration=capture["duration"], warmup=20, channels=channels,
                symbols={s:state.result() for s,state in states.items()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", choices=SYMBOLS, default=SYMBOLS)
    args = parser.parse_args()
    opener = gzip.open if args.input.suffix == ".gz" else open
    with opener(args.input, "rt", encoding="utf-8") as source:
        result = summarize(json.load(source), args.symbols)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2)
    for symbol, row in result["symbols"].items():
        print(symbol, dict(row["counts"]), row["matched_versions"], dict(row["matches"]))
