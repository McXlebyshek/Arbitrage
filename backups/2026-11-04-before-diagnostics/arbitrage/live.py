"""Independent exchange workers with bounded reconnects and periodic snapshots."""
import asyncio
from dataclasses import asdict
import random
import time

import aiohttp

from .models import is_target
from .streams import URLS, decode, heartbeat, parse, subscriptions
from .mexc_book import BookGap, MexcBook


def now_ms():
    return int(time.time() * 1000)


class RequestLimiter:
    """Shared REST snapshot budget across all MEXC connection groups."""
    def __init__(self, interval=0.3):
        self.lock = asyncio.Lock()
        self.interval = interval
        self.next_request = 0

    async def wait(self):
        async with self.lock:
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, self.next_request - loop.time()))
            self.next_request = loop.time() + self.interval


class LiveExchange:
    def __init__(self, exchange, bases):
        self.exchange, self.bases = exchange, bases
        self.markets, self.quotes = {}, {}
        self.state = "starting"
        self.last_error = None
        self.reconnects = 0
        self.updates = 0
        self.last_message_ms = None
        self.books = {}
        self.book_resyncs = 0
        self.last_book_resync_reason = None
        self.snapshot_limiter = RequestLimiter()
        self.connection_limiter = RequestLimiter(interval=1.1)

    async def load_markets(self):
        import ccxt.async_support as ccxt
        client = getattr(ccxt, self.exchange)({"enableRateLimit": True, "timeout": 15000,
                    "options": {"defaultType": "swap", "defaultSubType": "linear"}})
        try:
            async with asyncio.timeout(60):
                markets = await client.load_markets()
                self.markets = {m["id"]: m for m in markets.values()
                                if is_target(m) and (self.bases is None or m["base"] in self.bases)}
            if not self.markets:
                raise ValueError("No active contracts for requested bases")
        finally:
            await client.close()

    def snapshot(self, timestamp):
        rows = [dict(asdict(q), quality=q.quality(timestamp), connection_state=self.state) for q in self.quotes.values()]
        usable = sum(q["quality"] == "ok" for q in rows) if self.state == "streaming" else 0
        # Disconnected quotes are retained for diagnosis but are never eligible.
        if self.state != "streaming":
            for row in rows:
                row["quality"] = "disconnected"
        return {"status": {"exchange": self.exchange, "state": self.state,
                "transport": "WebSocket", "checked_at_ms": timestamp,
                "quality_evaluated_at_ms": timestamp, "quotes": len(rows),
                "usable_quotes": usable, "markets": len(self.markets),
                "missing_bases": sorted((self.bases or set()) - {m["base"] for m in self.markets.values()}),
                "book_resyncs": self.book_resyncs,
                "last_book_resync_reason": self.last_book_resync_reason,
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

    async def resnapshot(self, session, symbol, reason="initial_snapshot"):
        self.quotes.pop(self.markets[symbol]["symbol"], None)
        self.books.pop(symbol, None)
        self.last_book_resync_reason = f"{symbol}: {reason}"
        await self.snapshot_limiter.wait()
        async with session.get(f"https://api.mexc.com/api/v1/contract/depth/{symbol}",
                               params={"limit": 1000}, timeout=aiohttp.ClientTimeout(total=15)) as response:
            response.raise_for_status()
            payload = await response.json()
        if payload.get("success") is not True:
            raise BookGap(f"REST depth rejected: {payload.get('code')}")
        book = MexcBook()
        book.initialize(payload["data"])
        self.books[symbol] = book
        self.book_resyncs += 1

    async def parse_quotes(self, session, payload, received):
        if self.exchange != "mexc" or payload.get("channel") != "push.depth":
            return parse(self.exchange, payload, self.markets, received)
        symbol = payload.get("symbol")
        if symbol not in self.markets:
            return []
        if symbol not in self.books:
            await self.resnapshot(session, symbol)
            return []  # REST alone cannot mark the WS depth as synchronized.
        try:
            quote = self.books[symbol].apply(payload, self.markets[symbol], received)
            return [quote] if quote is not None else []
        except BookGap as exc:
            # Remove the last quote before awaiting REST; unrelated groups keep working.
            await self.resnapshot(session, symbol, str(exc))
            return []

    async def connection(self, session):
        self.state = "connecting"
        async with session.ws_connect(URLS[self.exchange], heartbeat=20, autoping=True,
                                      timeout=aiohttp.ClientWSTimeout(ws_close=5),
                                      max_msg_size=2**20) as ws:
            self.quotes.clear()
            self.books.clear()
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
                quotes = await self.parse_quotes(session, payload, received)
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
                    await self.connection_limiter.wait()
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


