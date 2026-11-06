"""Public REST enrichment of a bounded set of WS candidates. Never submits orders."""
import asyncio
import json
from pathlib import Path
import time

from .economics import evaluate_pair, normalize_funding
from .scanner import decimal, output_number
from .collector import EXCHANGES


def now_ms():
    return int(time.time()*1000)


def load_fee_overrides(path):
    if path is None:
        return {}
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Fee profile must map exchange ids to fractional taker rates")
    rates = {}
    for exchange, raw in document.items():
        rate = decimal(raw)
        if exchange not in EXCHANGES or not 0 <= rate <= decimal("0.01"):
            raise ValueError("Taker rates must be fractional values from 0 to 0.01")
        rates[exchange] = str(rate)
    return rates


def okx_volume_lower_bound(candles, observed_at_ms):
    """Quote turnover of non-overlapping 1H bars wholly starting inside the last 24h.

    The oldest partial hour is omitted. This is a lower bound, never baseVolume*last.
    """
    values = {}
    for row in candles:
        if len(row) < 8:
            raise ValueError("Incomplete OKX quote-volume candle")
        stamp, volume = int(row[0]), decimal(row[7])
        if volume < 0 or stamp % 3600000 != 0:
            raise ValueError("Invalid OKX quote-volume candle")
        if observed_at_ms-86400000 <= stamp <= observed_at_ms:
            if stamp in values:
                raise ValueError("Duplicate OKX quote-volume candle")
            values[stamp] = volume
    if not values:
        raise ValueError("No in-window OKX quote-volume candles")
    return output_number(sum(values.values(), decimal(0)))


class EconomicData:
    def __init__(self, args, pools):
        self.args, self.pools = args, {pool.exchange: pool for pool in pools}
        self.candidates = []
        self.legs, self.funding, self.histories, self.liquidity = {}, {}, {}, {}
        self.clients, self.loaded = {}, {}
        self.client_locks = {}
        self.state, self.last_error, self.cycles = "starting", None, 0
        self.active = set()

    def submit(self, scanner):
        self.candidates = list(scanner.get("rows", [])[:self.args.economics_pairs])

    async def client(self, exchange):
        import ccxt.async_support as ccxt
        if exchange not in self.clients:
            self.clients[exchange] = getattr(ccxt, exchange)({"enableRateLimit": True, "timeout": 15000,
                "options": {"defaultType": "swap", "defaultSubType": "linear"}})
        client = self.clients[exchange]
        lock = self.client_locks.setdefault(exchange, asyncio.Lock())
        async with lock:
            if exchange not in self.loaded or now_ms()-self.loaded[exchange] > 900000:
                async with asyncio.timeout(60):
                    await client.load_markets(reload=exchange in self.loaded)
                self.loaded[exchange] = now_ms()
        return client

    async def enrich_funding(self, exchange, symbols):
        client = await self.client(exchange)
        for native in symbols:
            key = exchange, native
            binding = self.args.registry.bindings.get(key)
            if binding is None:
                continue
            market = next((m for m in client.markets.values() if m["id"] == native
                           and m.get("swap") and m.get("linear") and m.get("settle") == "USDT"), None)
            if market is None:
                continue
            symbol = market["symbol"]
            try:
                old = self.funding.get(key)
                if old is None or now_ms()-old["received_at_ms"] >= 60000:
                    raw = await client.fetch_funding_rate(symbol)
                    if raw.get("symbol") not in (None, symbol):
                        raise ValueError("Funding symbol mismatch")
                    received = now_ms()
                    history = self.histories.get(key)
                    if history is None or received-history["received_at_ms"] > 900000:
                        rows = await client.fetch_funding_rate_history(symbol, since=received-48*3600000, limit=100)
                        if any(row.get("symbol") not in (None, symbol) for row in rows):
                            raise ValueError("Funding history symbol mismatch")
                        history = {"rows": rows, "received_at_ms": now_ms()}
                        self.histories = self.histories | {key: history}
                    value = normalize_funding(raw, history["rows"], received, history["received_at_ms"])
                    self.funding = self.funding | {key: value}
            except Exception as exc:
                self.last_error = f"{exchange}/{native} funding: {type(exc).__name__}: {exc}"
            try:
                old = self.liquidity.get(key)
                if old is None or now_ms()-old["received_at_ms"] >= 60000:
                    ticker = await client.fetch_ticker(symbol)
                    if ticker.get("symbol") not in (None, symbol):
                        raise ValueError("Volume ticker symbol mismatch")
                    volume, source = ticker.get("quoteVolume"), "public_quote_volume_24h"
                    if volume is None and exchange == "okx":
                        candles = await client.publicGetMarketCandles({"instId": native, "bar": "1H", "limit": "24"})
                        volume = okx_volume_lower_bound(candles["data"], now_ms())
                        source = "okx_1h_quote_volume_lower_bound"
                    self.liquidity = self.liquidity | {key: {"quote_volume_24h_usdt": volume,
                                                           "source": source, "received_at_ms": now_ms()}}
            except Exception as exc:
                self.last_error = f"{exchange}/{native} volume: {type(exc).__name__}: {exc}"

    async def fetch_books(self, exchange, symbols):
        client = await self.client(exchange)
        for native in symbols:
            key = exchange, native
            try:
                market = next(m for m in client.markets.values() if m["id"] == native and m.get("swap")
                              and m.get("linear") and m.get("settle") == "USDT")
                if exchange == "mexc":
                    await self.pools[exchange].snapshot_limiter.wait()
                book = await client.fetch_order_book(market["symbol"], limit=20)
                fields = ("id", "symbol", "base", "quote", "settle", "active", "swap", "linear", "contractSize", "precision", "limits", "taker", "percentage")
                compact_market = {field: market.get(field) for field in fields}
                compact_market["info"] = {"taker_fee_rate": market.get("info", {}).get("taker_fee_rate")} if exchange == "gate" else {}
                value = {"market": compact_market, "precision_mode": client.precisionMode, "book": book,
                         "received_at_ms": now_ms()}
                self.legs = self.legs | {key: value}
            except Exception as exc:
                # Do not keep an apparently fresh previous book after a failed refresh.
                self.legs = {k: value for k, value in self.legs.items() if k != key}
                self.last_error = f"{exchange}/{native} depth: {type(exc).__name__}: {exc}"

    def selection(self):
        grouped = {}
        for pair in list(self.candidates):
            for side in ("long", "short"):
                grouped.setdefault(pair[f"{side}_exchange"], set()).add(pair[f"{side}_symbol"])
        return grouped

    async def funding_loop(self):
        while True:
            grouped = self.selection()
            outcomes = await asyncio.gather(*(self.enrich_funding(exchange, sorted(symbols))
                for exchange, symbols in grouped.items()), return_exceptions=True)
            failures = [f"{type(item).__name__}: {item}" for item in outcomes if isinstance(item, Exception)]
            if failures:
                self.last_error = "; ".join(failures)
            await asyncio.sleep(5)

    async def run(self):
        funding_task = asyncio.create_task(self.funding_loop(), name="funding-history")
        try:
            while True:
                selected = list(self.candidates)
                if not selected:
                    self.state = "awaiting_candidates"
                    await asyncio.sleep(0.5)
                    continue
                self.state, self.last_error = "refreshing", None
                grouped = self.selection()
                self.active = {(exchange, native) for exchange, symbols in grouped.items() for native in symbols}
                outcomes = await asyncio.gather(*(self.fetch_books(exchange, sorted(symbols))
                    for exchange, symbols in grouped.items()), return_exceptions=True)
                if funding_task.done():
                    funding_task.result()
                failures = [f"{type(item).__name__}: {item}" for item in outcomes if isinstance(item, Exception)]
                if failures:
                    self.last_error = "; ".join(failures)
                self.state = "partial" if self.last_error else "ready"
                self.cycles += 1
                await asyncio.sleep(1)
        finally:
            self.state = "stopped"
            self.legs = {}
            funding_task.cancel()
            await asyncio.gather(funding_task, return_exceptions=True)
            await asyncio.gather(*(client.close() for client in self.clients.values()), return_exceptions=True)

    def snapshot(self, scanner, timestamp):
        if self.state == "stopped":
            return {"state": "stopped", "cycles": self.cycles, "rows": []}
        legs, funding, liquidity = self.legs, self.funding, self.liquidity
        current = {key: dict(value, funding=funding.get(key), liquidity=liquidity.get(key)) for key, value in legs.items()}
        selected = scanner["rows"][:self.args.economics_pairs]
        needed = {(pair[f"{side}_exchange"], pair[f"{side}_symbol"]) for pair in selected for side in ("long", "short")}
        rows = [evaluate_pair(pair, current, self.args.registry, timestamp, self.args.capital_usdt,
                    self.args.hold_hours, decimal(self.args.max_impact_pct)/100,
                    decimal(self.args.min_budget_utilization), self.args.extra_cost_usdt, self.args.fee_overrides,
                    self.args.max_skew_ms, self.args.min_volume_usdt)
                for pair in selected]
        return {"state": self.state, "cycles": self.cycles, "last_error": self.last_error,
                "generated_at_ms": timestamp, "rows": rows,
                "inputs": [{"exchange": key[0], "native_id": key[1], **value} for key, value in current.items() if key in needed],
                "model": "conditional_convergence_and_constant_rate_funding_scenario",
                "policy": {key: getattr(self.args, key) for key in ("capital_usdt", "hold_hours", "max_impact_pct",
                           "min_budget_utilization", "extra_cost_usdt", "fee_overrides", "max_skew_ms", "min_volume_usdt")},
                "account_fees_verified": False, "execution_verified": False}
