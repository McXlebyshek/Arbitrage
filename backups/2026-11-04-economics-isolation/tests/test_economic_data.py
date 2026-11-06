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
        async def funding_loop():
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
