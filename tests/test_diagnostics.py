import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import subprocess
import sys
from unittest.mock import patch

from arbitrage.diagnostics import RunReport, percentile
from arbitrage.pools import run_pools


def snapshot(state="streaming", fresh=8):
    return {"status": dict(exchange="test", state=state, markets=10, quotes=10,
             usable_quotes=fresh, omitted_markets=0, missing_bases=[], metadata_error=None,
             connections=1, metadata_generation=2, reconnects=1, book_resyncs=3,
             connection_statuses=[{"last_failure": "ConnectionError: test close"}])}


class DiagnosticsTests(unittest.TestCase):
    def test_cli_rejects_out_of_range_binance_group_before_network(self):
        result=subprocess.run([sys.executable,'-m','arbitrage','--binance-group-size','0'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2)
        self.assertIn('--binance-group-size must be 1',result.stderr)

    def test_cli_rejects_out_of_range_gate_group_before_network(self):
        result=subprocess.run([sys.executable,'-m','arbitrage','--gate-group-size','21'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2)
        self.assertIn('--gate-group-size must be 1',result.stderr)

    def test_cli_prevents_overwriting_database_with_json(self):
        with TemporaryDirectory() as directory:
            database = Path(directory)/"existing.sqlite3"
            database.write_bytes(b"existing database contents")
            process = subprocess.run([sys.executable, "-m", "arbitrage", "--output", str(database),
                                      "--database", str(database)], capture_output=True, text=True)
            self.assertEqual(process.returncode, 2)
            self.assertIn("different file paths", process.stderr)
            self.assertEqual(database.read_bytes(), b"existing database contents")

    def test_warmup_cpu_memory_and_sampled_coverage(self):
        clock = {"wall": 0, "cpu": 0}
        with TemporaryDirectory() as directory, \
                patch("arbitrage.diagnostics.time.monotonic", side_effect=lambda: clock["wall"]), \
                patch("arbitrage.diagnostics.time.process_time", side_effect=lambda: clock["cpu"]), \
                patch("arbitrage.diagnostics.rss_bytes", return_value=2000):
            database = Path(directory)/"test.sqlite3"
            database.write_bytes(b"x"*100)
            report = RunReport(database, 10)
            report.observe([snapshot(fresh=0)], {}, 100, 20)
            self.assertEqual(report.exchanges["test"]["steady_observations"], 0)
            clock.update(wall=10, cpu=2)
            report.observe([snapshot()], {"displayed_pairs": 3, "eligible_quotes": 2}, 10, 1)
            clock.update(wall=20, cpu=3)
            database.write_bytes(b"x"*150)
            result = report.document()
            self.assertEqual(result["exchanges"]["test"]["mean_sampled_fresh_fraction"], 0.8)
            self.assertEqual(len(result["exchanges"]["test"]["recent_failures"]), 1)
            self.assertEqual(result["cpu_pct_one_core"], 15)
            self.assertEqual(result["steady_cpu_pct_one_core"], 10)
            self.assertEqual(result["peak_rss_bytes"], 2000)
            self.assertEqual(result["database_growth_bytes"], 50)
            self.assertEqual(result["max_spread_rows"], 3)
            self.assertEqual(result["peak_loop_lag_ms"], 20)
            report.finish("duration_reached", 1, [snapshot("stopped")])
            path = Path(directory)/"report.json"
            report.write(path)
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(result["finished"])
            self.assertEqual(result["shutdown_states"], {"test": "stopped"})
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_unavailable_memory_and_zero_selected_are_not_fabricated(self):
        with TemporaryDirectory() as directory, patch("arbitrage.diagnostics.rss_bytes", return_value=None):
            report = RunReport(Path(directory)/"missing.sqlite3", 0)
            sample = snapshot()
            sample["status"].update(markets=0, usable_quotes=0)
            report.observe([sample], {}, 0, 0)
            self.assertIsNone(report.document()["peak_rss_bytes"])
            self.assertIsNone(report.document()["exchanges"]["test"]["mean_sampled_fresh_fraction"])

    def test_percentiles(self):
        self.assertIsNone(percentile([], 0.95))
        self.assertEqual(percentile([1, 2, 3, 4, 5], 0.95), 4)


class RunLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_binance_group_override_reaches_pool_and_respects_global_cap(self):
        await self.run_case(False,5,20)
        await self.run_case(False,10,2)

    async def test_bounded_run_writes_final_report_and_stops_workers(self):
        await self.run_case(False)

    async def test_publication_error_still_writes_failure_report(self):
        await self.run_case(True)

    async def run_case(self, fail, binance_group_size=None, group_size=20):
        class Pool:
            exchange = "test"
            state = "streaming"
            async def run(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    self.state = "stopped"
            def snapshot(self, timestamp):
                return snapshot(self.state, 10)

        with TemporaryDirectory() as directory:
            pool = Pool()
            args = SimpleNamespace(exchanges=["test"], symbols="ALL", registry=None,
                group_size=group_size, max_connections=1, market_refresh_seconds=60, mexc_depth_limit=5,
                interval=0.01, duration=0.03, database=Path(directory)/"test.sqlite3",
                report=Path(directory)/"report.json", report_warmup_seconds=0)
            if binance_group_size is not None:
                args.exchanges=['binanceusdm']
                args.binance_group_size=binance_group_size
            def publish(*unused):
                if fail:
                    raise OSError("Simulated write failure")
                return {"displayed_pairs": 2, "eligible_quotes": 2}
            with patch("arbitrage.pools.ExchangePool", return_value=pool) as factory:
                if fail:
                    with self.assertRaisesRegex(OSError, "Simulated"):
                        await run_pools(args, publish)
                else:
                    self.assertEqual(await run_pools(args, publish), 0)
                if binance_group_size is not None:
                    self.assertEqual(factory.call_args.args[3],min(group_size,binance_group_size))
                    self.assertEqual(factory.call_args.args[-1],'depth5@500ms')
            report = json.loads(args.report.read_text(encoding="utf-8"))
            if binance_group_size is not None:
                self.assertEqual(report['config']['binance_group_size'],min(group_size,binance_group_size))
            self.assertTrue(report["finished"])
            self.assertEqual(pool.state, "stopped")
            self.assertEqual(report["shutdown_states"], {"test": "stopped"})
            if fail:
                self.assertIn("OSError", report["finish_reason"])
