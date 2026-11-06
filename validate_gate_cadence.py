"""Measure wire activity only. Never produces quotes or modifies monitor state."""
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import time

import aiohttp


SYMBOLS = ("TRX_USDT", "XRP_USDT")
TICKER = "futures.book_ticker"
DEPTH = "futures.obu"
URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"


async def compare(duration):
    frames = {symbol: {channel: [] for channel in (TICKER, DEPTH)} for symbol in SYMBOLS}
    topics = {f"ob.{symbol}.50": symbol for symbol in SYMBOLS}
    acknowledgements = Counter()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=15)) as session:
        async with session.ws_connect(URL, headers={"X-Gate-Size-Decimal": "1"}, heartbeat=15) as ws:
            for channel, payload in [(TICKER, list(SYMBOLS)), (DEPTH, list(topics))]:
                await ws.send_json(dict(time=int(time.time()), event="subscribe", channel=channel, payload=payload))
            started = time.monotonic()
            start_epoch_ms = int(time.time() * 1000)
            while (remaining := duration - (time.monotonic() - started)) > 0:
                try:
                    msg = await asyncio.wait_for(ws.receive(), remaining)
                except TimeoutError:
                    break
                elapsed = time.monotonic() - started
                if msg.type != aiohttp.WSMsgType.TEXT:
                    raise RuntimeError(f"unexpected websocket message {msg.type}")
                data = msg.json()
                if data.get("error"):
                    raise RuntimeError(str(data["error"]))
                channel = data.get("channel")
                row = data.get("result", {})
                if data.get("event") == "subscribe" and row.get("status") == "success":
                    acknowledgements[channel] += 1
                symbol = row.get("s") if channel == TICKER else topics.get(row.get("s"))
                if channel in (TICKER, DEPTH) and symbol in frames and isinstance(row.get("t"), int):
                    frames[symbol][channel].append((elapsed, row["t"], row.get("u")))
    results = {}
    for symbol, channels in frames.items():
        stats = {}
        recent = {}
        for channel, events in channels.items():
            arrivals = [event[0] for event in events]
            boundaries = [0, *arrivals, duration]
            stats[channel] = dict(frames=len(events), distinct_updates=len({e[2] for e in events}),
                                  longest_silent_seconds=round(max(b-a for a,b in zip(boundaries, boundaries[1:])), 3))
            recent[channel] = {second for second in range(20, duration)
                               if any(second-3 < arrival <= second for arrival in arrivals)}
        results[symbol] = dict(channels=stats, samples=duration-20,
                               ticker_recent=len(recent[TICKER]), depth_recent=len(recent[DEPTH]),
                               depth_recent_ticker_silent=len(recent[DEPTH] - recent[TICKER]))
    return dict(start_epoch_ms=start_epoch_ms, duration_seconds=duration, acknowledgements=dict(acknowledgements),
                results=results, interpretation="Local frame arrival in prior 3s, sampled each second after 20s. No depth reconstruction or price freshness inferred.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not 30 <= args.duration <= 300 or args.output.exists():
        parser.error("use 30–300 seconds and a new output file")
    result = asyncio.run(compare(args.duration))
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2)
    print(json.dumps(result, indent=2))
