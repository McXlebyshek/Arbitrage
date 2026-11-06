"""Isolated Gate OBU/BBO comparison. No data is published to the monitor."""
import argparse
import asyncio
from collections import Counter, OrderedDict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import time

import aiohttp

from arbitrage.models import normalize
from validate_gate_cadence import DEPTH, TICKER, URL


def positive_id(row, key):
    value = row.get(key)
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {key}")
    return value


def amount(raw):
    if not isinstance(raw, (str, int)) or isinstance(raw, bool):
        raise ValueError("invalid decimal type")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("invalid decimal")
    return value


@dataclass(frozen=True)
class Top:
    uid: int
    stamp: int
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal


class Book:
    def __init__(self):
        self.uid = None
        self.bids = {}
        self.asks = {}

    def apply(self, row):
        try:
            return self._apply(row)
        except (ValueError, TypeError):
            self.__init__()
            raise

    def _apply(self, row):
        uid = positive_id(row, "u")
        stamp = positive_id(row, "t")
        if type(row.get("full", False)) is not bool:
            raise ValueError("invalid full flag")
        if row.get("full"):
            self.bids.clear()
            self.asks.clear()
        else:
            first = positive_id(row, "U")
            if self.uid is None or first != self.uid + 1 or uid < first:
                raise ValueError("sequence gap")
        for key, side in (("b", self.bids), ("a", self.asks)):
            levels = row.get(key, [])
            if not isinstance(levels, list):
                raise ValueError("invalid side")
            for level in levels:
                if not isinstance(level, list) or len(level) != 2:
                    raise ValueError("invalid level")
                price, size = map(amount, level)
                if not price:
                    raise ValueError("invalid price")
                if size:
                    side[price] = size
                else:
                    side.pop(price, None)
        if not self.bids or not self.asks or max(self.bids) >= min(self.asks):
            raise ValueError("empty or crossed book")
        self.uid = uid
        bid, ask = max(self.bids), min(self.asks)
        return Top(uid, stamp, bid, ask, self.bids[bid], self.asks[ask])


class Comparison:
    def __init__(self, symbol):
        self.symbol = symbol
        self.market = dict(symbol=symbol, id=symbol, base=symbol.split("_")[0], quote="USDT")
        self.counts = Counter()
        self.errors = Counter()
        self.examples = []
        self.reset()

    def reset(self):
        self.book = Book()
        self.latest = {}
        self.pending = {DEPTH: OrderedDict(), TICKER: OrderedDict()}

    def record(self, channel, top, received):
        quote = normalize("gate", self.market, dict(bid=top.bid, ask=top.ask, timestamp=top.stamp), received, channel)
        self.latest[channel] = (quote, top)
        self.counts[channel] += 1
        peer_channel = DEPTH if channel == TICKER else TICKER
        peer = self.pending[peer_channel].pop(top.uid, None)
        if peer is None:
            pending = self.pending[channel]
            pending[top.uid] = top
            if len(pending) > 4096:
                pending.popitem(last=False)
            return
        self.counts["paired_id"] += 1
        prices = (top.bid, top.ask) == (peer.bid, peer.ask)
        sizes = (top.bid_size, top.ask_size) == (peer.bid_size, peer.ask_size)
        self.counts["paired_price_equal"] += prices
        self.counts["paired_price_and_size_equal"] += prices and sizes
        if not (prices and sizes) and len(self.examples) < 5:
            self.examples.append({channel: vars(top), peer_channel: vars(peer)})

    def sample(self, now):
        self.counts["samples"] += 1
        fresh = {}
        for channel in (DEPTH, TICKER):
            entry = self.latest.get(channel)
            fresh[channel] = entry is not None and entry[0].quality(now) == "ok"
            self.counts[channel + "_fresh"] += fresh[channel]
        self.counts["obu_only_fresh"] += fresh[DEPTH] and not fresh[TICKER]
        if all(fresh.values()):
            self.counts["both_fresh"] += 1
            a, b = (self.latest[c][1] for c in (DEPTH, TICKER))
            self.counts["sample_price_equal"] += (a.bid, a.ask) == (b.bid, b.ask)

    async def collect(self, session):
        topic = f"ob.{self.symbol}.50"
        while True:
            try:
                async with session.ws_connect(URL, headers={"X-Gate-Size-Decimal": "1"}, heartbeat=15) as ws:
                    self.counts["connections"] += 1
                    for channel, item in [(TICKER, self.symbol), (DEPTH, topic)]:
                        await ws.send_json(dict(time=int(time.time()), channel=channel, event="subscribe", payload=[item]))
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            raise ValueError("unexpected websocket message")
                        data = msg.json()
                        if data.get("error"):
                            raise ValueError(str(data["error"]))
                        row = data.get("result") or {}
                        channel = data.get("channel")
                        received = time.time_ns() // 1000000
                        if channel == DEPTH and row.get("s") == topic:
                            # Drop the previous top before validation: closing a failed
                            # socket can yield control to the independent sampler.
                            self.latest.pop(DEPTH, None)
                            top = self.book.apply(row)
                            self.counts["snapshots"] += row.get("full") is True
                        elif channel == TICKER and row.get("s") == self.symbol:
                            top = Top(positive_id(row, "u"), positive_id(row, "t"),
                                      *(amount(row.get(k)) for k in ("b", "a", "B", "A")))
                            if min(top.bid, top.ask, top.bid_size, top.ask_size) <= 0 or top.bid > top.ask:
                                self.latest.pop(TICKER, None)
                                self.counts["invalid_ticker"] += 1
                                continue
                        else:
                            continue
                        self.record(channel, top, received)
                    raise ValueError("websocket closed")
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
                self.errors[str(exc) or type(exc).__name__] += 1
            finally:
                self.reset()
            await asyncio.sleep(1)


async def compare(duration):
    states = [Comparison(s + "_USDT") for s in ("TRX", "ATOM", "XRP")]
    started = time.monotonic()
    epoch = time.time_ns() // 1000000
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=10)) as session:
        async with asyncio.TaskGroup() as group:
            workers = [group.create_task(state.collect(session)) for state in states]
            for second in range(20, duration):
                await asyncio.sleep(max(0, started + second - time.monotonic()))
                for state in states:
                    state.sample(time.time_ns() // 1000000)
            await asyncio.sleep(max(0, started + duration - time.monotonic()))
            for worker in workers:
                worker.cancel()
    return dict(start_ms=epoch, duration_seconds=duration, warmup_seconds=20,
                symbols={s.symbol: dict(counts=s.counts, errors=s.errors, mismatches=s.examples) for s in states})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 30 <= args.duration <= 300 or args.output.exists():
        parser.error("use 30–300 seconds and a new file")
    result = asyncio.run(compare(args.duration))
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, default=str)
    print(json.dumps(result, indent=2, default=str))
