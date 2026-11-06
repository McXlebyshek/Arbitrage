import asyncio
from unittest.mock import patch
import unittest

from arbitrage.models import normalize
from arbitrage.pools import ExchangePool, complete
from arbitrage.scanner import Registry, scan


def market(index):
    base = f"COIN{index}"
    return dict(id=f"{base}_USDT", symbol=f"{base}/USDT:USDT", base=base,
                quote="USDT", settle="USDT", contractSize=1, active=True, linear=True, swap=True)


def registry():
    return Registry(dict(version="pool-test", bindings=[dict(exchange="gate", id="COIN4_USDT",
                    base="COIN4", asset="coin4", price_units="1", contract_size="1")]))


class PoolTests(unittest.IsolatedAsyncioTestCase):
    def pool(self):
        return ExchangePool("gate", None, registry(), group_size=2, max_connections=2, refresh_seconds=60)

    async def test_metadata_loop_recovers_after_error_and_retains_failure_history(self):
        calls = 0
        async def load(worker):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("Simulated catalog failure")
            instrument = market(4)
            worker.markets = {instrument["id"]: instrument}
        async def idle(worker):
            await asyncio.Event().wait()
        pool = self.pool()
        pool.refresh_seconds = 0.03
        with patch("arbitrage.pools.LiveExchange.load_markets", load), patch("arbitrage.pools.LiveExchange.run", idle):
            task = asyncio.create_task(pool.run())
            try:
                async with asyncio.timeout(3):
                    while pool.generation < 2:
                        await asyncio.sleep(0.01)
                self.assertEqual(pool.metadata_failures, 1)
                self.assertIsNone(pool.metadata_error)
                self.assertIn("Simulated catalog failure", pool.last_metadata_failure)
                self.assertEqual(pool.group_restarts, 0)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(pool.groups, {})
            self.assertEqual(pool.state, "stopped")

    async def test_counters_survive_metadata_group_replacement(self):
        async def idle(worker):
            await asyncio.Event().wait()
        pool = self.pool()
        instrument = market(4)
        with patch("arbitrage.pools.LiveExchange.run", idle):
            try:
                await pool.reconcile({instrument["id"]: instrument})
                old_worker = pool.groups[0][0]
                old_worker.reconnects, old_worker.updates, old_worker.book_resyncs = 3, 20, 2
                await pool.reconcile({instrument["id"]: dict(instrument, contractSize=2)})
                result = pool.snapshot(pool.loaded_at_ms)
                self.assertEqual(result["status"]["reconnects"], 3)
                self.assertEqual(result["status"]["updates"], 20)
                self.assertEqual(result["status"]["group_restarts"], 1)
            finally:
                for key in list(pool.groups):
                    await pool.stop_group(key)

    async def test_capacity_prioritizes_registry_and_reports_omissions(self):
        pool = self.pool()
        markets = {m["id"]: m for m in [market(i) for i in range(6)]}
        selected, omitted = pool.choose(markets)
        self.assertEqual((len(selected), omitted), (4, 2))
        self.assertIn("COIN4_USDT", selected)

    async def test_mexc_depth_budget_is_independent_of_connection_budget(self):
        pool = ExchangePool("mexc", None, registry(), group_size=20, max_connections=8, mexc_depth_limit=7)
        selected, omitted = pool.choose({m["id"]: m for m in [market(i) for i in range(12)]})
        self.assertEqual(pool.group_size, 5)
        self.assertEqual((len(selected), omitted), (7, 5))

    async def test_refresh_keeps_unchanged_groups_and_replaces_changed_metadata(self):
        async def idle(worker):
            await asyncio.Event().wait()
        pool = self.pool()
        catalog = {m["id"]: m for m in [market(i) for i in range(4)]}
        with patch("arbitrage.pools.LiveExchange.run", idle):
            try:
                reconfiguration = asyncio.create_task(pool.reconcile(catalog))
                await asyncio.sleep(0.01)
                during_startup = pool.snapshot(10100)
                self.assertEqual(during_startup["status"]["markets"], 4)
                self.assertEqual(during_startup["status"]["requested_markets"], 4)
                self.assertFalse(complete(during_startup))
                await reconfiguration
                self.assertEqual(len(pool.groups), 2)
                original = pool.groups[0][1]
                await pool.reconcile({key: dict(value, info={"dynamic": 123}) for key, value in catalog.items()})
                self.assertIs(pool.groups[0][1], original)
                execution_rules = {key: dict(value, limits={"price": {"min": 50, "max": 150}},
                                             precision={"price": 0.01}) for key, value in catalog.items()}
                await pool.reconcile(execution_rules)
                self.assertIs(pool.groups[0][1], original)
                self.assertEqual(pool.groups[0][0].markets["COIN0_USDT"]["limits"]["price"]["min"], 50)
                changed = {key: dict(value) for key, value in catalog.items()}
                changed["COIN0_USDT"]["contractSize"] = 2
                await pool.reconcile(changed)
                self.assertIsNot(pool.groups[0][1], original)
                self.assertTrue(original.cancelled())
                await pool.reconcile({})
                self.assertEqual(pool.groups, {})
                self.assertEqual(pool.state, "no_markets")
            finally:
                for key in list(pool.groups):
                    await pool.stop_group(key)

    async def test_failed_connection_quotes_are_excluded_while_other_group_streams(self):
        async def idle(worker):
            await asyncio.Event().wait()
        pool = self.pool()
        catalog = {m["id"]: m for m in [market(i) for i in range(4)]}
        with patch("arbitrage.pools.LiveExchange.run", idle):
            try:
                await pool.reconcile(catalog)
                for index, (worker, _) in pool.groups.items():
                    worker.state = "streaming" if index == 0 else "reconnecting"
                    for instrument in worker.markets.values():
                        quote = normalize("gate", instrument, dict(bid=100, ask=101, timestamp=10000), 10100, "ws_bbo")
                        worker.quotes[quote.symbol] = quote
                pool.loaded_at_ms = 10100
                result = pool.snapshot(10100)
                self.assertEqual(result["status"]["state"], "streaming")
                self.assertEqual(result["status"]["usable_quotes"], 2)
                self.assertEqual(sum(q["connection_state"] == "reconnecting" for q in result["quotes"]), 2)
                self.assertFalse(complete(result))
                pool.loaded_at_ms = 1
                result = pool.snapshot(121000)
                self.assertEqual(result["status"]["state"], "metadata_stale")
                self.assertEqual(result["status"]["usable_quotes"], 0)
            finally:
                for key in list(pool.groups):
                    await pool.stop_group(key)
