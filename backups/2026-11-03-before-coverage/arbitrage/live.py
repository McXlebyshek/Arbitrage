"""Independent exchange workers with bounded reconnects and periodic snapshots."""
import asyncio
from dataclasses import asdict
import random
import time

import aiohttp

from .models import is_target
from .streams import URLS, decode, heartbeat, parse, subscriptions


def now_ms():
    return int(time.time() * 1000)


class LiveExchange:
    def __init__(self, exchange, bases):
        self.exchange, self.bases = exchange, bases
        self.markets, self.quotes = {}, {}
        self.state = "starting"
        self.last_error = None
        self.reconnects = 0
        self.updates = 0
        self.last_message_ms = None

    async def load_markets(self):
        import ccxt.async_support as ccxt
        client = getattr(ccxt, self.exchange)({"enableRateLimit": True, "timeout": 15000,
                    "options": {"defaultType": "swap", "defaultSubType": "linear"}})
        try:
            async with asyncio.timeout(60):
                markets = await client.load_markets()
                self.markets = {m["id"]: m for m in markets.values()
                                if is_target(m) and m["base"] in self.bases}
            if not self.markets:
                raise ValueError("No active contracts for requested bases")
        finally:
            await client.close()

    def snapshot(self, timestamp):
        rows = [dict(asdict(q), quality=q.quality(timestamp)) for q in self.quotes.values()]
        usable = sum(q["quality"] == "ok" for q in rows) if self.state == "streaming" else 0
        # Disconnected quotes are retained for diagnosis but are never eligible.
        if self.state != "streaming":
            for row in rows:
                row["quality"] = "disconnected"
        return {"status": {"exchange": self.exchange, "state": self.state,
                "transport": "WebSocket", "checked_at_ms": timestamp,
                "quality_evaluated_at_ms": timestamp, "quotes": len(rows),
                "usable_quotes": usable, "markets": len(self.markets),
                "missing_bases": sorted(self.bases - {m["base"] for m in self.markets.values()}),
                "updates": self.updates, "reconnects": self.reconnects,
                "last_message_ms": self.last_message_ms, "last_error": self.last_error,
                "trading_availability": "not_checked"},
                "instruments": [{k: m.get(k) for k in ("id", "symbol", "base", "quote", "settle", "contractSize", "active", "swap", "linear")}
                                for m in self.markets.values()], "quotes": rows}

    async def send(self, ws, payload):
        if isinstance(payload, str):
            await ws.send_str(payload)
        elif payload is not None:
            await ws.send_json(payload)

    async def connection(self, session):
        self.state = "connecting"
        async with session.ws_connect(URLS[self.exchange], heartbeat=20, autoping=True,
                                      timeout=aiohttp.ClientWSTimeout(ws_close=5),
                                      max_msg_size=2**20) as ws:
            self.quotes.clear()
            self.state = "subscribing"
            for subscription in subscriptions(self.exchange, self.markets):
                await self.send(ws, subscription)
                await asyncio.sleep(0.15)
            loop = asyncio.get_running_loop()
            last_data = last_ping = loop.time()
            while True:
                if loop.time() - last_ping >= 15:
                    await self.send(ws, heartbeat(self.exchange))
                    last_ping = loop.time()
                if loop.time() - last_data > 30:
                    raise TimeoutError("No market-data updates for 30 seconds")
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=5)
                except TimeoutError:
                    continue
                if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise ConnectionError(f"WS closed: {ws.close_code}; {ws.exception()}")
                if message.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    continue
                received = now_ms()
                self.last_message_ms = received
                if message.data == "ping":
                    await ws.send_str("pong")
                    continue
                payload = decode(message.data)
                if payload is None:
                    continue
                quotes = parse(self.exchange, payload, self.markets, received)
                for quote in quotes:
                    previous = self.quotes.get(quote.symbol)
                    if (previous and quote.source_timestamp_ms is not None
                            and previous.source_timestamp_ms is not None
                            and quote.source_timestamp_ms < previous.source_timestamp_ms):
                        continue
                    self.quotes[quote.symbol] = quote
                    self.updates += 1
                if quotes:
                    last_data = loop.time()
                    self.state = "streaming"
                    self.last_error = None

    async def run(self):
        delay = 1
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=15)) as session:
            while True:
                started = time.monotonic()
                try:
                    if not self.markets:
                        self.state = "loading_markets"
                        await self.load_markets()
                    await self.connection(session)
                except asyncio.CancelledError:
                    self.state = "stopped"
                    raise
                except Exception as exc:
                    self.state = "reconnecting"
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.reconnects += 1
                    if time.monotonic() - started > 60:
                        delay = 1
                    await asyncio.sleep(delay + random.uniform(0, 0.25))
                    delay = min(delay * 2, 30)


async def run_live(args, publish):
    bases = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    workers = [LiveExchange(exchange, bases) for exchange in args.exchanges]
    tasks = [asyncio.create_task(worker.run(), name=worker.exchange) for worker in workers]
    start = time.monotonic()
    try:
        while True:
            timestamp = now_ms()
            snapshots = [worker.snapshot(timestamp) for worker in workers]
            # Surface an unexpected worker crash instead of silently losing an exchange.
            for task in tasks:
                if task.done():
                    task.result()
            await asyncio.to_thread(publish, args, snapshots, "WebSocket")
            if args.duration and time.monotonic() - start >= args.duration:
                return 0 if all(s["status"]["usable_quotes"] == len(bases) for s in snapshots) else 1
            await asyncio.sleep(args.interval)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for worker in workers:
            worker.state = "stopped"
        await asyncio.to_thread(publish, args, [worker.snapshot(now_ms()) for worker in workers], "WebSocket")
