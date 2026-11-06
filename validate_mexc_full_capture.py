"""Capture concurrent public MEXC depth streams and initial REST books for offline replay."""
import argparse
import asyncio
import json
from pathlib import Path
import time

import aiohttp

from arbitrage.streams import URLS, decode, heartbeat, subscriptions

SYMBOLS = ("ATOM_USDT", "TRX_USDT", "XRP_USDT")


async def capture(duration):
    records = []
    started = time.monotonic()
    epoch = time.time_ns() // 1000000

    def record(kind, data):
        records.append([kind, time.time_ns() // 1000000, data])

    async def seed(session):
        for symbol in SYMBOLS:
            async with session.get(f"https://api.mexc.com/api/v1/contract/depth/{symbol}",
                                   params={"limit": 1000}, timeout=aiohttp.ClientTimeout(total=15)) as response:
                response.raise_for_status()
                payload = await response.json()
                if payload.get("success") is not True:
                    raise ValueError(f"REST snapshot rejected for {symbol}")
                record("seed", dict(symbol=symbol, data=payload["data"]))
            await asyncio.sleep(0.3)

    observed = set()
    ready = asyncio.Event()

    async def stream(session, mode):
        async with session.ws_connect(URLS["mexc"], heartbeat=20) as ws:
            for sub in subscriptions("mexc", SYMBOLS):
                if mode == "full":
                    sub["method"] = "sub.depth.full"
                    sub["param"]["limit"] = 5
                await ws.send_json(sub)
            ping = time.monotonic()
            while (remaining := duration - (time.monotonic() - started)) > 0:
                if time.monotonic() - ping > 15:
                    await ws.send_json(heartbeat("mexc"))
                    ping = time.monotonic()
                try:
                    msg = await asyncio.wait_for(ws.receive(), min(2, remaining))
                except TimeoutError:
                    continue
                if msg.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    raise RuntimeError(f"{mode} socket closed {msg.type}")
                payload = decode(msg.data)
                if payload:
                    record(mode, payload)
                    if mode == "incremental" and payload.get("channel") == "push.depth":
                        observed.add(payload.get("symbol"))
                        if observed.issuperset(SYMBOLS):
                            ready.set()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=15)) as session:
        async with asyncio.TaskGroup() as group:
            group.create_task(stream(session, "incremental"))
            group.create_task(stream(session, "full"))
            await asyncio.wait_for(ready.wait(), timeout=20)
            # Accumulate diffs first: a returned REST version can predate its request.
            await asyncio.sleep(3)
            await seed(session)
    return dict(start_ms=epoch, duration=duration, records=records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=180)
    args = parser.parse_args()
    if args.output.exists() or not 60 <= args.duration <= 300:
        parser.error("select a new file and 60–300 seconds")
    result = asyncio.run(capture(args.duration))
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, separators=(",", ":"))
    print("Captured", len(result["records"]), "records")
