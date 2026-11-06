from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aiohttp.test_utils import TestClient, TestServer
from arbitrage.__main__ import save
from arbitrage.economics import evaluate_pair
from arbitrage.scanner import Registry, scan
from arbitrage.ui import Dashboard, create_app
from test_economics import NOW, fixture
from test_scanner import snapshot, binding


def sample():
    _, legs, registry = fixture()
    snapshots = [snapshot("alpha", stamp=NOW), snapshot("beta", bid=102, ask=103, stamp=NOW)]
    for item in snapshots:
        item["status"]["checked_at_ms"] = NOW
        item["quotes"][0]["received_at_ms"] = NOW
    scanner = scan(snapshots, registry, NOW)
    pair = scanner["rows"][0]
    model = evaluate_pair(pair, legs, registry, NOW)
    economics = dict(generated_at_ms=NOW, state="ready", rows=[model], policy=dict(capital_usdt=1000),
        inputs=[dict(exchange=key[0], native_id=key[1], **value) for key,value in legs.items()],
        funding_observations=[dict(exchange=key[0], native_id=key[1], **value["funding"]) for key,value in legs.items()])
    signals = dict(generated_at_ms=NOW, run_id="ui-test", state="monitoring", events=[],
                   rows=[dict(id=pair["id"], state="confirmed", confirmations=3)])
    return dict(generated_at_ms=NOW, exchanges=snapshots, scanner=scanner, economics=economics, signals=signals), registry


class DashboardTests(unittest.TestCase):
    def test_live_formulas_and_confirmed_state_match_sources(self):
        document, registry = sample()
        with TemporaryDirectory() as directory:
            path = Path(directory)/"latest.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            state = Dashboard(path, Path(directory)/"missing.sqlite3", registry, lambda: NOW).state()
            row = state["rows"][0]
            self.assertEqual(state["mode"], "live")
            self.assertEqual(row["open_spread_pct"], 2)
            self.assertAlmostEqual(row["exit_spread_pct"], 100*(99-103)/103)
            self.assertTrue(row["confirmed_current"])
            self.assertEqual(row["legs"]["long"]["base"], "BTC")
            self.assertFalse(row["execution_verified"])

    def test_quote_age_and_funding_age_cannot_reuse_confirmed_label(self):
        document, registry = sample()
        with TemporaryDirectory() as directory:
            path = Path(directory)/"latest.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            store = Dashboard(path, Path(directory)/"missing.sqlite3", registry, lambda: NOW+3500)
            self.assertFalse(any(row["confirmed_current"] for row in store.state()["rows"]))
            self.assertFalse(store.state()["rows"][0]["data_fresh"])
            document["economics"]["funding_observations"][0]["received_at_ms"] = NOW-120001
            path.write_text(json.dumps(document), encoding="utf-8")
            store.clock = lambda: NOW
            self.assertTrue(store.state()["rows"][0]["data_fresh"])
            self.assertFalse(store.state()["rows"][0]["confirmed_current"])

    def test_archive_fallback_is_read_only_and_preserves_real_points(self):
        document, registry = sample()
        with TemporaryDirectory() as directory:
            database, path = Path(directory)/"market.sqlite3", Path(directory)/"latest.json"
            save(database, document["exchanges"], document["scanner"], document["economics"], document["signals"])
            stopped = deepcopy(document)
            stopped["scanner"]["rows"] = []
            for item in stopped["exchanges"]:
                item["status"]["state"] = "stopped"
            path.write_text(json.dumps(stopped), encoding="utf-8")
            before = database.read_bytes()
            store = Dashboard(path, database, registry, lambda: NOW+60000)
            state = store.state()
            self.assertEqual(state["mode"], "archive")
            self.assertTrue(state["rows"])
            self.assertFalse(any(row["confirmed_current"] for row in state["rows"]))
            detail = store.pair(state["rows"][0]["id"], 15)
            self.assertEqual(len(detail["points"]), 1)
            self.assertEqual(detail["points"][0]["entry"], 2)
            self.assertEqual(len(detail["observed_funding"]["long"]), 1)
            self.assertEqual(before, database.read_bytes())

    def test_unknown_values_are_null_and_invalid_snapshot_has_empty_state(self):
        document, registry = sample()
        document["economics"]["funding_observations"] = []
        document["economics"]["inputs"] = []
        with TemporaryDirectory() as directory:
            path, database = Path(directory)/"latest.json", Path(directory)/"absent.sqlite3"
            path.write_text(json.dumps(document), encoding="utf-8")
            store = Dashboard(path, database, registry, lambda: NOW)
            self.assertIsNone(store.state()["rows"][0]["funding_net_24h_pct"])
            self.assertIsNone(store.state()["rows"][0]["legs"]["long"]["volume_24h_usdt"])
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(store.state()["mode"], "empty")
            self.assertIsNotNone(store.state()["error"])
            path.unlink()
            self.assertEqual(store.state()["rows"], [])

    def test_scaled_instrument_exit_uses_registered_price_units(self):
        registry = Registry(dict(version="scaled-ui", bindings=[binding("alpha", units="1000"),binding("beta")]))
        snapshots = [snapshot("alpha",bid=99000,ask=100000,stamp=NOW),snapshot("beta",bid=102,ask=103,stamp=NOW)]
        for item in snapshots:
            item["quotes"][0]["received_at_ms"] = NOW
        scanner = scan(snapshots, registry, NOW)
        with TemporaryDirectory() as directory:
            path = Path(directory)/"latest.json"
            path.write_text(json.dumps(dict(generated_at_ms=NOW,exchanges=snapshots,scanner=scanner)), encoding="utf-8")
            row = Dashboard(path, Path(directory)/"none.sqlite3", registry, lambda: NOW).state()["rows"][0]
            self.assertEqual(row["legs"]["long"]["ask"], 100)
            self.assertEqual(row["legs"]["long"]["bid"], 99)
            self.assertAlmostEqual(row["exit_spread_pct"], 100*(99-103)/103)


class DashboardHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_api_and_path_and_method_limits(self):
        document, registry = sample()
        with TemporaryDirectory() as directory:
            path, database = Path(directory)/"latest.json", Path(directory)/"none.sqlite3"
            path.write_text(json.dumps(document), encoding="utf-8")
            async with TestClient(TestServer(create_app(path, database, registry))) as client:
                response = await client.get("/")
                self.assertEqual(response.status, 200)
                self.assertIn("Арбитраж", await response.text())
                self.assertIn("frame-ancestors 'none'",response.headers["Content-Security-Policy"])
                response = await client.get("/api/state")
                self.assertEqual(response.status, 200)
                self.assertEqual(len((await response.json())["rows"]), 2)
                self.assertEqual((await client.post("/api/state",json={})).status, 405)
                self.assertEqual((await client.get("/assets/pyproject.toml")).status, 404)
                self.assertEqual((await client.get("/api/pair?id=missing")).status, 404)
                self.assertEqual((await client.get("/api/pair?id=missing&minutes=900")).status, 400)
                response = await client.get('/api/pair', params=dict(id=document['scanner']['rows'][0]['id'], minutes=1440))
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())['window_minutes'], 1440)
                self.assertFalse(database.exists())
