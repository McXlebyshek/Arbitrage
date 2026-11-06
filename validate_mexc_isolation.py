"""Local five-contract MEXC recovery experiment; all HTTP/WS traffic stays on loopback."""
import argparse
import asyncio
import json
from pathlib import Path
import time
from unittest.mock import patch

import aiohttp
from aiohttp import web

from arbitrage.live import LiveExchange, now_ms
from arbitrage.scanner import Registry, scan
from arbitrage.streams import URLS


async def check():
    registry = Registry.load()
    symbols = sorted(s for ex, s in registry.bindings if ex == "mexc")[:5]
    target = "ATOM_USDT"
    assert target in symbols
    versions = dict.fromkeys(symbols, 100)
    worker = LiveExchange("mexc", None)
    for s in symbols:
        b = registry.bindings["mexc", s]
        worker.markets[s] = dict(id=s, symbol=b["base"]+"/USDT:USDT", base=b["base"],
                                quote="USDT", settle="USDT", active=True, linear=True,
                                swap=True, contractSize=float(b["contract_size"]))
    inject = asyncio.Event()
    failing = asyncio.Event()
    sockets = 0

    async def depth(request):
        symbol = request.match_info["symbol"]
        if symbol == target and inject.is_set() and not failing.is_set():
            failing.set()
            await asyncio.sleep(3.6)
            return web.Response(status=503)
        return web.json_response(dict(success=True, data=dict(version=versions[symbol],
                                      bids=[[100, 10]], asks=[[101, 10]])))

    async def websocket(request):
        nonlocal sockets
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets += 1
        for _ in symbols:
            await ws.receive_json()
        async def listen():
            async for _ in ws:
                pass
        reader = asyncio.create_task(listen())
        try:
            while not ws.closed and not reader.done():
                for s in symbols:
                    if inject.is_set() and s == target and not failing.is_set():
                        versions[s] += 1
                    versions[s] += 1
                    v = versions[s]
                    await ws.send_json(dict(channel="push.depth", symbol=s, data=dict(
                        begin=v, end=v, version=v, cts=now_ms(), bids=[], asks=[])))
                await asyncio.sleep(.08)
        except (ConnectionError, RuntimeError):
            pass
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        return ws

    app = web.Application()
    app.router.add_get("/ws", websocket)
    app.router.add_get("/depth/{symbol}", depth)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    original_get = aiohttp.ClientSession.get

    def local_get(session, remote, **kwargs):
        prefix = "https://api.mexc.com/api/v1/contract/depth/"
        assert remote.startswith(prefix)
        return original_get(session, url + "/depth/" + remote[len(prefix):], **kwargs)

    def eligible():
        stamp = now_ms()
        snap = worker.snapshot(stamp)
        evaluation = scan([snap], registry, stamp, 1000)
        return evaluation["eligible_quotes"], {q["exchange_symbol"] for q in snap["quotes"]
                                               if q["quality"] == "ok"}
    with patch.dict(URLS, {"mexc":url+"/ws"}), patch.object(aiohttp.ClientSession, "get", local_get):
        task = asyncio.create_task(worker.run())
        try:
            async with asyncio.timeout(25):
                while eligible()[0] != 5:
                    await asyncio.sleep(.02)
                inject.set()
                await failing.wait()
                start = time.monotonic()
                counts = []
                first_recovered = None
                while first_recovered is None:
                    admitted, syms = eligible()
                    assert admitted == len(syms)
                    if target in syms or time.monotonic() - start > 17:
                        first_recovered = round(time.monotonic()-start,3)
                    else:
                        counts.append(admitted)
                    await asyncio.sleep(.05)
                return dict(classification="LOCAL_SYNTHETIC_GAP_AND_HTTP_503", target=target,
                            rest_delay_seconds=3.6, samples_while_target_absent=len(counts),
                            minimum_eligible_peers=min(counts), samples_all_four_peers=counts.count(4),
                            recovery_seconds=first_recovered if target in syms else None, websocket_connections=sockets,
                            last_failure=worker.last_failure,
                            reconnects=worker.reconnects, final_eligible=eligible()[0])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = asyncio.run(check())
    with args.output.open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(result)
