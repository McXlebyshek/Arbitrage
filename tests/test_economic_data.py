import asyncio
from contextlib import closing, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from arbitrage.economic_data import EconomicData, load_fee_overrides, okx_volume_lower_bound
from arbitrage.__main__ import save
from test_economics import NOW, HOUR, fixture


def arguments(registry):
    return SimpleNamespace(registry=registry, economics_pairs=3, capital_usdt=1000, hold_hours=8,
        max_impact_pct=0.1, min_budget_utilization=0.9, extra_cost_usdt=0, fee_overrides={},
        max_skew_ms=1000, min_volume_usdt=1000000)


class FeeProfileTests(unittest.TestCase):
    def test_okx_quote_volume_is_conservative_window_sum(self):
        end = NOW-NOW%HOUR
        rows = [[end-hours*HOUR, 1, 1, 1, 1, 999999, 999999, 100] for hours in range(25)]
        # 24 in-window starts; base volume fields are deliberately unrelated.
        self.assertEqual(okx_volume_lower_bound(rows, NOW), 2400)
        with self.assertRaises(ValueError):
            okx_volume_lower_bound(rows+[rows[0]], NOW)

    def test_fee_profile_is_explicit_and_validated(self):
        self.assertEqual(load_fee_overrides(None), {})
        with TemporaryDirectory() as directory:
            path = Path(directory)/"fees.json"
            path.write_text('{"bybit":"0.00055"}', encoding="utf-8")
            self.assertEqual(load_fee_overrides(path), {"bybit": "0.00055"})
            for text in ['{"bybit": -0.1}', '{"typo": 0.0005}', '{"bybit":"nan"}', '[]']:
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_fee_overrides(path)


class EconomicDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_contract_backs_off_without_blocking_healthy_contract(self):
        pair, legs, registry = fixture()
        engine = EconomicData(arguments(registry), [])
        engine.selection = lambda: {"alpha": {"FAILED", "FAST"}}
        calls = {"FAILED": 0, "FAST": 0}
        async def books(exchange, symbols):
            native = symbols[0]
            calls[native] += 1
            if native == "FAILED":
                raise OSError("rate limit or transport failure")
            engine.legs = engine.legs | {(exchange, native): {"book": "fresh"}}
        engine.fetch_books = books
        task = asyncio.create_task(engine.book_loop("alpha"))
        try:
            await asyncio.sleep(0.65)
            self.assertEqual(calls["FAILED"], 1)
            self.assertGreaterEqual(calls["FAST"], 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_slow_contract_does_not_block_same_venue_and_removed_worker_is_cancelled(self):
        pair, legs, registry = fixture()
        engine = EconomicData(arguments(registry), [])
        wanted = {"SLOW", "FAST"}
        engine.selection = lambda: {"alpha": set(wanted)}
        slow_started, fast_updated, slow_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def books(exchange, symbols):
            native = symbols[0]
            if native == "SLOW":
                slow_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    slow_cancelled.set()
            engine.legs = engine.legs | {(exchange, native): {"book": "fresh"}}
            fast_updated.set()
        engine.fetch_books = books
        task = asyncio.create_task(engine.book_loop("alpha"))
        try:
            await asyncio.wait_for(slow_started.wait(), 2)
            await asyncio.wait_for(fast_updated.wait(), 2)
            engine.legs = engine.legs | {("alpha", "SLOW"): {"book": "old"}}
            wanted.remove("SLOW")
            await asyncio.wait_for(slow_cancelled.wait(), 2)
            # Cancellation completes before the manager removes the cached entry.
            for _ in range(30):
                if ("alpha", "SLOW") not in engine.legs:
                    break
                await asyncio.sleep(0.01)
            self.assertNotIn(("alpha", "SLOW"), engine.legs)
            self.assertIn(("alpha", "FAST"), engine.legs)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_depth_request_concurrency_is_bounded_and_cancelled_on_shutdown(self):
        pair, legs, registry = fixture()
        engine = EconomicData(arguments(registry), [])
        engine.selection = lambda: {"alpha": {str(i) for i in range(8)}}
        started, active, peak = asyncio.Event(), 0, 0
        async def books(exchange, symbols):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 3:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
        engine.fetch_books = books
        task = asyncio.create_task(engine.book_loop("alpha"))
        try:
            await asyncio.wait_for(started.wait(), 2)
            self.assertEqual(peak, 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(active, 0)

    async def test_public_enrichment_cache_snapshot_and_sqlite(self):
        pair, legs, registry = fixture()
        engine = EconomicData(arguments(registry), [])

        class Client:
            precisionMode = 4
            def __init__(self, market, book):
                self.markets = {market["symbol"]: market}
                self.book = book
                self.calls = 0
            async def fetch_funding_rate(self, symbol):
                self.calls += 1
                return dict(interval="8h", fundingRate=0.001, fundingTimestamp=NOW+HOUR)
            async def fetch_funding_rate_history(self, symbol, **kwargs):
                return [{"timestamp": NOW-hours*HOUR, "fundingRate": 0.001} for hours in range(48, 0, -8)]
            async def fetch_ticker(self, symbol):
                return {"quoteVolume": 5000000}
            async def fetch_order_book(self, symbol, **kwargs):
                return self.book

        clients = {exchange: Client(value["market"], value["book"]) for (exchange, _), value in legs.items()}
        async def client(exchange):
            return clients[exchange]
        engine.client = client
        with patch("arbitrage.economic_data.now_ms", return_value=NOW):
            for exchange in ("alpha", "beta"):
                await engine.enrich_funding(exchange, ["BTC_USDT"])
                await engine.fetch_books(exchange, ["BTC_USDT"])
                await engine.enrich_funding(exchange, ["BTC_USDT"])
                self.assertEqual(clients[exchange].calls, 1)
        engine.state = "ready"
        with patch("arbitrage.economic_data.now_ms", return_value=NOW):
            engine.submit({"rows": [pair]})
        result = engine.snapshot({"rows": [pair]}, NOW)
        self.assertEqual(result["rows"][0]["status"], "scenario_estimated")
        self.assertEqual(len(result["inputs"]), 2)
        self.assertTrue(result["inputs"][0]["funding"]["history_24h_covered"])
        self.assertEqual(engine.snapshot({"rows": []}, NOW)["rows"], [])
        self.assertEqual(engine.snapshot({"rows": [pair]}, NOW+4000)["rows"][0]["status"], "blocked")
        with TemporaryDirectory() as directory:
            path = Path(directory)/"history.sqlite3"
            save(path, [], None, result)
            with closing(sqlite3.connect(path)) as database:
                payload = json.loads(database.execute("SELECT payload FROM economics").fetchone()[0])
                self.assertEqual(payload["rows"][0]["classification"], "conditional_economics")

    async def test_depth_failure_invalidates_previous_cache(self):
        pair, legs, registry = fixture()
        engine = EconomicData(arguments(registry), [])
        engine.legs = dict(legs)
        class Client:
            markets = {"BTC/USDT:USDT": legs[("alpha", "BTC_USDT")]["market"]}
            async def fetch_order_book(self, *args, **kwargs):
                raise OSError("Simulated depth failure")
        async def client(exchange):
            return Client()
        engine.client = client
        await engine.fetch_books("alpha", ["BTC_USDT"])
        self.assertNotIn(("alpha", "BTC_USDT"), engine.legs)
        self.assertIn("Simulated depth failure", engine.last_error)

    async def test_slow_funding_does_not_block_depth_and_shutdown_closes_client(self):
        pair, legs, registry = fixture()
        engine = EconomicData(arguments(registry), [])
        engine.submit({"rows": [pair]})
        depth_updated, funding_started = asyncio.Event(), asyncio.Event()
        class Client:
            closed = False
            async def close(self):
                self.closed = True
        client = Client()
        engine.clients = {"alpha": client}
        async def funding_loop(exchange):
            funding_started.set()
            await asyncio.Event().wait()
        async def books(exchange, symbols):
            engine.legs = dict(legs)
            depth_updated.set()
        engine.funding_loop, engine.fetch_books = funding_loop, books
        task = asyncio.create_task(engine.run())
        try:
            await asyncio.wait_for(funding_started.wait(), 2)
            await asyncio.wait_for(depth_updated.wait(), 2)
            self.assertTrue(engine.legs)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(engine.state, "stopped")
        self.assertEqual(engine.legs, {})
        self.assertTrue(client.closed)

    async def test_slow_exchange_does_not_block_other_exchange_books(self):
        pair, legs, registry = fixture()
        engine = EconomicData(arguments(registry), [])
        engine.submit({"rows": [pair]})
        slow_started, fast_updated = asyncio.Event(), asyncio.Event()
        async def funding_loop(exchange):
            await asyncio.Event().wait()
        async def books(exchange, symbols):
            if exchange == "alpha":
                slow_started.set()
                await asyncio.Event().wait()
            fast_updated.set()
        engine.funding_loop, engine.fetch_books = funding_loop, books
        task = asyncio.create_task(engine.run())
        try:
            await asyncio.wait_for(slow_started.wait(), 2)
            await asyncio.wait_for(fast_updated.wait(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(engine.state, "stopped")
