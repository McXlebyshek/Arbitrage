"""Record independent public MEXC depth arrival times for offline comparison."""
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import time

import aiohttp

from arbitrage.streams import URLS, decode, heartbeat, subscriptions


async def capture(duration):
    symbols = ("ATOM_USDT", "TRX_USDT", "XRP_USDT")
    frames = {symbol: [] for symbol in symbols}
    control = Counter()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=15)) as client:
        async with client.ws_connect(URLS["mexc"], heartbeat=20) as ws:
            for sub in subscriptions("mexc", symbols):
                await ws.send_json(sub)
            started = time.monotonic()
            start_ms = time.time_ns() // 1000000
            ping = started
            while (remaining := duration - (time.monotonic() - started)) > 0:
                if time.monotonic() - ping >= 15:
                    await ws.send_json(heartbeat("mexc"))
                    ping = time.monotonic()
                try:
                    msg = await asyncio.wait_for(ws.receive(), min(2, remaining))
                except TimeoutError:
                    continue
                received = time.time_ns() // 1000000
                if msg.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    raise RuntimeError(f"public socket closed: {msg.type}")
                payload = decode(msg.data)
                if payload is None:
                    continue
                channel = payload.get("channel")
                symbol = payload.get("symbol")
                if channel == "push.depth" and symbol in frames:
                    book = payload["data"]
                    stamp = book.get("cts") or book.get("ts") or payload.get("ts")
                    frames[symbol].append([received, stamp, book.get("end", book.get("version"))])
                else:
                    control[channel] += 1
            finish_ms = time.time_ns() // 1000000
    if not all(frames.values()):
        raise RuntimeError("missing depth traffic for requested symbols")
    return dict(start_ms=start_ms, finish_ms=finish_ms, control=control,
                columns=["received_at_ms", "source_timestamp_ms", "end_version"], frames=frames)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=180, choices=range(30, 301), metavar="30..300")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new output file")
    result = asyncio.run(capture(args.duration))
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output)
    print({sym: len(rows) for sym, rows in result["frames"].items()})
