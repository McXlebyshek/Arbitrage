"""Public REST enrichment of a bounded set of WS candidates. Never submits orders."""
import asyncio
import json
from pathlib import Path
import time

from .economics import evaluate_pair, normalize_funding
from .scanner import decimal, output_number
from .collector import EXCHANGES
from .candidates import rank_candidates


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
        self.selected_at, self.ranking, self.ranking_at_ms = {}, [], None
        self.legs, self.funding, self.histories, self.liquidity = {}, {}, {}, {}
        self.clients, self.loaded = {}, {}
        self.client_locks = {}
        self.state, self.last_error, self.cycles = "starting", None, 0
        self.errors = {}
        self.active = set()

    def error(self, key, kind, message=None):
        error_key = key[0], key[1], kind
        self.errors = self.errors | {error_key: message} if message else {k:v for k,v in self.errors.items() if k != error_key}
        self.last_error = "; ".join(self.errors.values()) or None

    def submit(self, scanner):
        rows, timestamp = list(scanner.get("rows", [])), now_ms()
        markets = {(exchange, native): market for exchange, pool in self.pools.items()
                   for native, market in pool.selected.items()}
        markets.update({key: leg["market"] for key, leg in self.legs.items() if key not in markets})
        ranked = rank_candidates(rows, self.funding, markets, self.args.registry, timestamp,
            self.args.hold_hours, self.args.fee_overrides, self.args.extra_cost_usdt)
        available = {row["id"]: row for row in rows}
        hold_ms = 1000*getattr(self.args, "candidate_hold_seconds", 10)
        retained = [pair["id"] for pair in self.candidates if pair["id"] in available
                    and timestamp-self.selected_at.get(pair["id"], timestamp) < hold_ms]
        chosen = retained[:self.args.economics_pairs]
        for item in ranked:
            if len(chosen) >= self.args.economics_pairs:
                break
            if item["id"] not in chosen:
                chosen.append(item["id"])
        self.selected_at = {key: self.selected_at.get(key, timestamp) for key in chosen}
        self.candidates = [available[key] for key in chosen]
        self.ranking, self.ranking_at_ms = ranked, timestamp

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
                self.error(key, "funding")
            except Exception as exc:
                self.error(key, "funding", f"{exchange}/{native} funding: {type(exc).__name__}: {exc}")
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
                self.error(key, "volume")
            except Exception as exc:
                self.error(key, "volume", f"{exchange}/{native} volume: {type(exc).__name__}: {exc}")

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
                self.error(key, "depth")
            except Exception as exc:
                # Do not keep an apparently fresh previous book after a failed refresh.
                self.legs = {k: value for k, value in self.legs.items() if k != key}
                self.error(key, "depth", f"{exchange}/{native} depth: {type(exc).__name__}: {exc}")

    def selection(self):
        grouped = {}
        for pair in list(self.candidates):
            for side in ("long", "short"):
                grouped.setdefault(pair[f"{side}_exchange"], set()).add(pair[f"{side}_symbol"])
        return grouped

    def funding_selection(self):
        if not self.pools:  # Supports isolated/offline test clients.
            return self.selection()
        return {exchange: {native for native in pool.selected if (exchange, native) in self.args.registry.bindings}
                for exchange, pool in self.pools.items()}

    async def funding_loop(self, exchange):
        while True:
            symbols = self.funding_selection().get(exchange, set())
            try:
                if symbols:
                    await self.enrich_funding(exchange, sorted(symbols))
                self.error((exchange, "client"), "funding")
            except Exception as exc:
                self.error((exchange, "client"), "funding", f"{exchange} funding client: {type(exc).__name__}: {exc}")
            await asyncio.sleep(5)

    async def book_loop(self, exchange):
        while True:
            symbols = self.selection().get(exchange, set())
            try:
                if symbols:
                    await self.fetch_books(exchange, sorted(symbols))
                    self.cycles += 1
                self.error((exchange, "client"), "depth")
            except Exception as exc:
                self.error((exchange, "client"), "depth", f"{exchange} depth client: {type(exc).__name__}: {exc}")
            await asyncio.sleep(1)

    async def run(self):
        workers = {}
        try:
            while True:
                selected = list(self.candidates)
                for exchange in set(self.selection()) | set(self.funding_selection()):
                    if exchange not in workers:
                        workers[exchange] = [asyncio.create_task(self.funding_loop(exchange), name=f"funding-{exchange}"),
                                             asyncio.create_task(self.book_loop(exchange), name=f"depth-{exchange}")]
                for tasks in workers.values():
                    for task in tasks:
                        if task.done():
                            task.result()
                if not selected:
                    self.state = "awaiting_candidates"
                    await asyncio.sleep(0.5)
                    continue
                grouped = self.selection()
                self.active = {(exchange, native) for exchange, symbols in grouped.items() for native in symbols}
                self.state = "partial" if self.last_error else "ready" if self.legs else "refreshing"
                await asyncio.sleep(0.25)
        finally:
            self.state = "stopped"
            self.legs = {}
            tasks = [task for values in workers.values() for task in values]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(client.close() for client in self.clients.values()), return_exceptions=True)

    def snapshot(self, scanner, timestamp):
        if self.state == "stopped":
            return {"state": "stopped", "cycles": self.cycles, "rows": []}
        legs, funding, liquidity = self.legs, self.funding, self.liquidity
        current = {key: dict(value, funding=funding.get(key), liquidity=liquidity.get(key)) for key, value in legs.items()}
        available = {pair["id"]: pair for pair in scanner["rows"]}
        selected = [available[pair["id"]] for pair in self.candidates if pair["id"] in available]
        needed = {(pair[f"{side}_exchange"], pair[f"{side}_symbol"]) for pair in selected for side in ("long", "short")}
        rows = [evaluate_pair(pair, current, self.args.registry, timestamp, self.args.capital_usdt,
                    self.args.hold_hours, decimal(self.args.max_impact_pct)/100,
                    decimal(self.args.min_budget_utilization), self.args.extra_cost_usdt, self.args.fee_overrides,
                    self.args.max_skew_ms, self.args.min_volume_usdt)
                for pair in selected]
        return {"state": self.state, "cycles": self.cycles, "last_error": self.last_error,
                "generated_at_ms": timestamp, "rows": rows,
                "selection": {"policy": "conditional_price_funding_fees_with_bounded_hold",
                              "ranked_at_ms": self.ranking_at_ms,
                              "candidate_hold_seconds": getattr(self.args, "candidate_hold_seconds", 10),
                              "selected_ids": [pair["id"] for pair in selected],
                              "selected_at_ms": self.selected_at, "ranking": self.ranking},
                "funding_coverage": {exchange: {"requested": len(symbols),
                    "cached": sum((exchange, native) in funding for native in symbols),
                    "with_24h_history": sum((exchange, native) in funding and funding[(exchange, native)]["history_24h_covered"]
                                            for native in symbols),
                    "fresh": sum((exchange, native) in funding and 0 <= timestamp-funding[(exchange, native)]["received_at_ms"] <= 120000
                                 for native in symbols)} for exchange, symbols in self.funding_selection().items()},
                "funding_observations": [{"exchange": exchange, "native_id": native, **funding[(exchange, native)]}
                    for exchange, symbols in self.funding_selection().items() for native in sorted(symbols)
                    if (exchange, native) in funding],
                "inputs": [{"exchange": key[0], "native_id": key[1], **value} for key, value in current.items() if key in needed],
                "model": "conditional_convergence_and_constant_rate_funding_scenario",
                "policy": {key: getattr(self.args, key) for key in ("capital_usdt", "hold_hours", "max_impact_pct",
                           "min_budget_utilization", "extra_cost_usdt", "fee_overrides", "max_skew_ms", "min_volume_usdt")},
                "account_fees_verified": False, "execution_verified": False}
