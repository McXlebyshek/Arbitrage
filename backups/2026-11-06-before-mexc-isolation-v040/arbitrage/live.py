"""Independent exchange workers with bounded reconnects and periodic snapshots."""
import asyncio
from collections import deque
from dataclasses import asdict
import random
import time

import aiohttp

from .models import is_target, normalize, number
from .gate_book import Book as GateBook
from .collector import public_client_config
from .streams import URLS, BINANCE_STREAMS, BINANCE_COMBINED_URL, decode, heartbeat, parse, subscriptions
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
    def __init__(self, exchange, bases, binance_stream='depth5@500ms'):
        if binance_stream not in BINANCE_STREAMS: raise ValueError('unsupported_binance_stream')
        self.binance_stream=binance_stream
        self.binance_update_ids={}
        self.gate_stream = "book_ticker"
        self.gate_decimal_sizes=True
        self.fractional_size_updates=0
        self.exchange, self.bases = exchange, bases
        self.markets, self.quotes = {}, {}
        self.state = "starting"
        self.last_error = None
        self.last_failure = None
        self.reconnects = 0
        self.fresh_data_timeout = 30
        self.stale_stream_reconnects = 0
        self.updates = 0
        self.last_message_ms = None
        self.source_lags = deque(maxlen=2048)
        self.event_lags = deque(maxlen=2048)
        self.dispatch_lags = deque(maxlen=2048)
        self.received_stale_updates = 0
        self.out_of_order_updates = 0
        self.books = {}
        self.book_resyncs = 0
        self.last_book_resync_reason = None
        self.snapshot_limiter = RequestLimiter()
        self.connection_limiter = RequestLimiter(interval=1.1)

    async def load_markets(self):
        import ccxt.async_support as ccxt
        client = getattr(ccxt, self.exchange)(public_client_config(self.exchange))
        try:
            async with asyncio.timeout(60):
                for attempt in range(2 if self.exchange=='gate' else 1):
                    try:
                        markets = await client.load_markets(reload=True)
                        break
                    except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
                        raise
                    except ccxt.NetworkError:
                        if self.exchange!='gate' or attempt: raise
                        await asyncio.sleep(.5)
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
                "binance_stream": self.binance_stream if self.exchange=='binanceusdm' else None,
                "gate_stream": self.gate_stream if self.exchange == "gate" else None,
                "gate_decimal_sizes": self.gate_decimal_sizes if self.exchange=='gate' else None,
                "quality_evaluated_at_ms": timestamp, "quotes": len(rows),
                "usable_quotes": usable, "markets": len(self.markets),
                "missing_bases": sorted((self.bases or set()) - {m["base"] for m in self.markets.values()}),
                "book_resyncs": self.book_resyncs,
                "last_book_resync_reason": self.last_book_resync_reason,
                "updates": self.updates, "reconnects": self.reconnects,
                "stale_stream_reconnects": self.stale_stream_reconnects,
                "fractional_size_updates": self.fractional_size_updates,
                "last_message_ms": self.last_message_ms, "last_error": self.last_error,
                "source_lag_samples": len(self.source_lags),
                "source_lag_p50_ms": sorted(self.source_lags)[(len(self.source_lags)-1)//2] if self.source_lags else None,
                "source_lag_p95_ms": sorted(self.source_lags)[int((len(self.source_lags)-1)*.95)] if self.source_lags else None,
                "event_lag_p95_ms": sorted(self.event_lags)[int((len(self.event_lags)-1)*.95)] if self.event_lags else None,
                "source_to_event_p95_ms": sorted(self.dispatch_lags)[int((len(self.dispatch_lags)-1)*.95)] if self.dispatch_lags else None,
                "received_stale_updates": self.received_stale_updates,
                "out_of_order_updates": self.out_of_order_updates,
                "last_failure": self.last_failure,
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

    def parse_gate_obu(self, payload, received):
        if payload.get("channel") != "futures.obu" or payload.get("event") not in (None, "update"):
            return []
        row = payload.get("result") or {}
        topic = row.get("s")
        if not isinstance(topic, str) or not topic.startswith("ob.") or not topic.endswith(".50"):
            return []
        symbol = topic[3:-3]
        if symbol not in self.markets:
            return []
        if symbol not in self.books:
            self.books[symbol] = GateBook()
        market = self.markets[symbol]
        try:
            top = self.books[symbol].apply(row)
            previous = self.quotes.get(market["symbol"])
            if previous and top.stamp < previous.source_timestamp_ms:
                self.out_of_order_updates += 1
                raise ValueError("OBU source time regressed")
        except (ValueError, TypeError) as exc:
            # Invalidate the entire socket before its asynchronous close handshake.
            self.state = "reconnecting"
            self.quotes.clear()
            self.books.clear()
            self.last_book_resync_reason = f"{symbol}: {exc}"
            raise
        self.book_resyncs += row.get("full") is True
        self.fractional_size_updates += 0 < top.bid_size < 1 or 0 < top.ask_size < 1
        return [normalize("gate", market, dict(bid=top.bid, ask=top.ask, timestamp=top.stamp),
                          received, "ws_depth_verified")]

    async def parse_quotes(self, session, payload, received):
        if self.exchange == "gate" and self.gate_stream == "obu":
            return self.parse_gate_obu(payload, received)
        if self.exchange=='gate' and payload.get('channel')=='futures.book_ticker' and payload.get('event')=='update':
            row=payload.get('result') or {}
            sizes=[number(row.get(key)) for key in ('B','A')]
            self.fractional_size_updates+=any(size is not None and 0<size<1 for size in sizes)
        if self.exchange != "mexc" or payload.get("channel") != "push.depth":
            quotes=parse(self.exchange, payload, self.markets, received, self.binance_stream)
            if quotes and self.exchange=='binanceusdm' and self.binance_stream!='bookTicker':
                row=payload['data'];symbol=row['s'];uid=row['u']
                if uid<=self.binance_update_ids.get(symbol,-1):
                    self.out_of_order_updates+=1
                    return []
                self.binance_update_ids[symbol]=uid
            return quotes
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
        url=URLS[self.exchange]
        if self.exchange=='binanceusdm' and self.binance_stream!='bookTicker':
            url=BINANCE_COMBINED_URL
        headers={'X-Gate-Size-Decimal':'1'} if self.exchange=='gate' and self.gate_decimal_sizes else None
        async with session.ws_connect(url, heartbeat=20, autoping=True,headers=headers,
                                      timeout=aiohttp.ClientWSTimeout(ws_close=5),
                                      max_msg_size=2**20) as ws:
            self.quotes.clear()
            self.books.clear()
            self.binance_update_ids.clear()
            self.source_lags.clear()
            self.event_lags.clear()
            self.dispatch_lags.clear()
            self.state = "subscribing"
            for subscription in subscriptions(self.exchange, self.markets, self.binance_stream, self.gate_stream):
                await self.send(ws, subscription)
                await asyncio.sleep(0.15)
            loop = asyncio.get_running_loop()
            last_data = last_ping = last_fresh = loop.time()
            frames = 0
            while True:
                # Buffered receive can finish synchronously; share the event loop
                # with other venues and publication at bounded frame intervals.
                if frames and frames % 256 == 0:
                    await asyncio.sleep(0)
                if loop.time() - last_ping >= 15:
                    await self.send(ws, heartbeat(self.exchange))
                    last_ping = loop.time()
                if loop.time() - last_data > 30:
                    raise TimeoutError("No market-data updates for 30 seconds")
                if loop.time() - last_fresh > self.fresh_data_timeout:
                    self.stale_stream_reconnects += 1
                    # Invalidate before ws_close can wait for its peer/deadline.
                    self.state = 'reconnecting'
                    self.last_error = 'TimeoutError: No fresh market-data updates within freshness watchdog window'
                    self.last_failure = self.last_error
                    raise TimeoutError('No fresh market-data updates within freshness watchdog window')
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=5)
                except TimeoutError:
                    continue
                if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise ConnectionError(f"WS closed: {ws.close_code}; {ws.exception()}")
                if message.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    continue
                frames += 1
                received = now_ms()
                self.last_message_ms = received
                if message.data == "ping":
                    await ws.send_str("pong")
                    continue
                payload = decode(message.data)
                if payload is None:
                    continue
                quotes = await self.parse_quotes(session, payload, received)
                event_payload=payload.get('data',payload) if self.exchange=='binanceusdm' and self.binance_stream!='bookTicker' else payload
                event_time = number(event_payload.get('E') if self.exchange=='binanceusdm' else payload.get('time_ms') if self.exchange=='gate' else None)
                if quotes and event_time is not None and event_time > 0:
                    self.event_lags.append(received-event_time)
                for quote in quotes:
                    if quote.source_timestamp_ms is not None:
                        lag = received-quote.source_timestamp_ms
                        self.source_lags.append(lag)
                        if event_time is not None and event_time > 0:
                            self.dispatch_lags.append(event_time-quote.source_timestamp_ms)
                        self.received_stale_updates += lag > 3000
                    previous = self.quotes.get(quote.symbol)
                    if (previous and quote.source_timestamp_ms is not None
                            and previous.source_timestamp_ms is not None
                            and quote.source_timestamp_ms < previous.source_timestamp_ms):
                        self.out_of_order_updates += 1
                        continue
                    self.quotes[quote.symbol] = quote
                    self.updates += 1
                    if quote.quality(received)=='ok':
                        last_fresh=loop.time()
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
                    self.last_failure = self.last_error
                    self.reconnects += 1
                    if time.monotonic() - started > 60:
                        delay = 1
                    await asyncio.sleep(delay + random.uniform(0, 0.25))
                    delay = min(delay * 2, 30)


