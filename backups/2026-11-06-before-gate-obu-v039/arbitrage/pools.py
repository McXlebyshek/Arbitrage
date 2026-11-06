"""Connection groups, coverage accounting and periodic instrument refresh."""
import asyncio
import time

from .live import LiveExchange, RequestLimiter, now_ms
from .diagnostics import RunReport


def structural(markets):
    # Order price bands can move with mark price (e.g. Gate). They do not change
    # the WS channel or price unit and must not force a socket restart.
    fields = ("id", "symbol", "base", "quote", "settle", "contractSize", "active", "linear", "swap")
    return {key: {field: market.get(field) for field in fields} for key, market in markets.items()}


class ExchangePool:
    def __init__(self, exchange, bases, registry, group_size=20, max_connections=8,
                 refresh_seconds=900, mexc_depth_limit=20, binance_stream='depth5@500ms',gate_decimal_sizes=True):
        self.exchange, self.bases, self.registry = exchange, bases, registry
        self.group_size = min(group_size, 5) if exchange == "mexc" else group_size
        self.max_connections = max_connections
        self.refresh_seconds = refresh_seconds
        self.mexc_depth_limit = mexc_depth_limit
        self.binance_stream = binance_stream
        self.gate_decimal_sizes=gate_decimal_sizes
        self.groups = {}
        self.catalog, self.selected = {}, {}
        self.loaded_at_ms = None
        self.metadata_error = None
        self.state = "loading_markets"
        self.generation = 0
        self.omitted = 0
        self.snapshot_limiter = RequestLimiter()
        self.connection_limiter = RequestLimiter(interval=1.1)
        self.retired = {"updates": 0, "reconnects": 0, "book_resyncs": 0,
                        "received_stale_updates": 0, "out_of_order_updates": 0,
                        "stale_stream_reconnects": 0,"fractional_size_updates":0}
        self.group_restarts = 0
        self.metadata_failures = 0
        self.last_metadata_failure = None

    def choose(self, markets):
        requested = {key: m for key, m in markets.items() if self.bases is None or m["base"] in self.bases}
        # Known instrument identities get capacity first; ticker equality does not grant scanner admission.
        ordered = sorted(requested, key=lambda key: ((self.exchange, key) not in self.registry.bindings, key))
        capacity = self.group_size * self.max_connections
        if self.exchange == "mexc":
            capacity = min(capacity, self.mexc_depth_limit)
        selected = {key: requested[key] for key in ordered[:capacity]}
        return selected, len(requested) - len(selected)

    async def stop_group(self, key, metadata_change=False):
        worker, task = self.groups.pop(key)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        worker.state = "stopped"
        for metric in self.retired:
            self.retired[metric] += getattr(worker, metric)
        self.group_restarts += bool(metadata_change)

    async def reconcile(self, markets):
        selected, omitted = self.choose(markets)
        # Publish the target before paced socket creation. Otherwise startup can
        # expose received quotes against zero selected markets in status/health.
        self.catalog, self.selected, self.omitted = markets, selected, omitted
        keys = sorted(selected)
        assignments = {index // self.group_size: {key: selected[key] for key in keys[index:index+self.group_size]}
                       for index in range(0, len(keys), self.group_size)}
        for key, (worker, task) in list(self.groups.items()):
            if task.done():
                task.result()
            if key not in assignments or structural(worker.markets) != structural(assignments[key]):
                await self.stop_group(key, metadata_change=True)
            else:
                worker.markets = assignments[key]  # Refresh execution rules without interrupting BBO.
        for key, assigned in assignments.items():
            if key not in self.groups:
                worker = LiveExchange(self.exchange, {m["base"] for m in assigned.values()})
                worker.binance_stream=self.binance_stream
                worker.gate_decimal_sizes=self.gate_decimal_sizes
                worker.markets = assigned
                worker.snapshot_limiter = self.snapshot_limiter
                worker.connection_limiter = self.connection_limiter
                task = asyncio.create_task(worker.run(), name=f"{self.exchange}-{key}")
                self.groups[key] = (worker, task)
                await asyncio.sleep(0.25)  # Pace new connections on catalog changes.
        self.loaded_at_ms = now_ms()
        self.generation += 1
        self.metadata_error = None
        self.state = "ready" if selected else "no_markets"

    async def run(self):
        try:
            while True:
                try:
                    loader = LiveExchange(self.exchange, None)
                    await loader.load_markets()
                    await self.reconcile(loader.markets)
                    await asyncio.sleep(self.refresh_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.metadata_error = f"{type(exc).__name__}: {exc}"
                    self.last_metadata_failure = self.metadata_error
                    self.metadata_failures += 1
                    await asyncio.sleep(min(30, self.refresh_seconds))
        finally:
            self.state = "stopped"
            for key in list(self.groups):
                await self.stop_group(key)

    def snapshot(self, timestamp):
        snapshots = [worker.snapshot(timestamp) for worker, _ in self.groups.values()]
        for _, task in self.groups.values():
            if task.done():
                task.result()
        metadata_stale = self.loaded_at_ms is not None and timestamp - self.loaded_at_ms > self.refresh_seconds * 2000
        connected = any(s["status"]["state"] == "streaming" for s in snapshots)
        state = "streaming" if connected else self.state
        if self.state == "stopped":
            state = "stopped"
        elif metadata_stale:
            state = "metadata_stale"
        rows = [row for snapshot in snapshots for row in snapshot["quotes"]]
        if state != "streaming":
            for row in rows:
                row["quality"] = state
                row["connection_state"] = state
        usable = sum(row["quality"] == "ok" and row["connection_state"] == "streaming" for row in rows)
        group_statuses = [dict(s["status"], connection_id=key) for key, s in zip(self.groups, snapshots)]
        errors = [s["last_error"] for s in group_statuses if s["last_error"]]
        return {"status": {"exchange": self.exchange, "state": state, "transport": "WebSocket",
                "checked_at_ms": timestamp, "quality_evaluated_at_ms": timestamp,
                "quotes": len(rows), "usable_quotes": usable, "markets": len(self.selected),
                "discovered_markets": len(self.catalog), "omitted_markets": self.omitted,
                "requested_markets": len(self.selected) + self.omitted,
                "missing_bases": sorted((self.bases or set()) - {m["base"] for m in self.catalog.values()}),
                "connections": len(self.groups), "connection_statuses": group_statuses,
                "metadata_generation": self.generation, "metadata_loaded_at_ms": self.loaded_at_ms,
                "metadata_error": self.metadata_error, "last_error": "; ".join(errors) or self.metadata_error,
                "last_metadata_failure": self.last_metadata_failure, "metadata_failures": self.metadata_failures,
                "group_restarts": self.group_restarts,
                **{metric: self.retired[metric] + sum(s[metric] for s in group_statuses) for metric in self.retired},
                "trading_availability": "not_checked"},
                "instruments": [m for s in snapshots for m in s["instruments"]], "quotes": rows}


def complete(snapshot):
    status = snapshot["status"]
    return (status["state"] == "streaming" and status["markets"] > 0
            and status["usable_quotes"] == status["markets"]
            and not status["omitted_markets"] and not status["missing_bases"]
            and not status["metadata_error"])


async def run_pools(args, publish):
    pools = []
    for exchange in args.exchanges:
        if args.symbols == "REGISTERED":
            bases = {entry["base"] for entry in args.registry.bindings.values() if entry["exchange"] == exchange}
        elif args.symbols == "ALL":
            bases = None
        else:
            bases = set(args.symbols.split(","))
        group_size=min(args.group_size,getattr(args,'gate_group_size',5)) if exchange=='gate' else args.group_size
        if exchange=='binanceusdm':group_size=min(args.group_size,getattr(args,'binance_group_size',20))
        pools.append(ExchangePool(exchange, bases, args.registry, group_size,
                     args.max_connections, args.market_refresh_seconds, args.mexc_depth_limit,
                     getattr(args,'binance_stream','depth5@500ms')))
    start = time.monotonic()
    report = RunReport(args.database, args.report_warmup_seconds,
                       {key: getattr(args, key) for key in ("symbols", "group_size", "max_connections", "market_refresh_seconds", "mexc_depth_limit", "interval", "duration")})
    report.data["config"]["economics_pairs"] = getattr(args, "economics_pairs", 0)
    report.data['config']['gate_group_size'] = min(args.group_size,getattr(args,'gate_group_size',5))
    report.data['config']['binance_group_size'] = min(args.group_size,getattr(args,'binance_group_size',20))
    report.data['config']['binance_stream'] = getattr(args,'binance_stream','depth5@500ms')
    report.data['config']['gate_decimal_sizes'] = True
    tasks = [asyncio.create_task(pool.run(), name=f"pool-{pool.exchange}") for pool in pools]
    engine = None
    if getattr(args, "economics_pairs", 0):
        from .economic_data import EconomicData
        engine = EconomicData(args, pools)
        args.economic_engine = engine
        tasks.append(asyncio.create_task(engine.run(), name="economic-data"))
    lag_ms, exit_code, finish_reason = 0, None, "cancelled"
    try:
        while True:
            for task in tasks:
                if task.done():
                    task.result()
            snapshots = [pool.snapshot(now_ms()) for pool in pools]
            publish_started = time.monotonic()
            result = await asyncio.to_thread(publish, args, snapshots, "WebSocket")
            report.observe(snapshots, result or {}, 1000*(time.monotonic()-publish_started), lag_ms)
            await asyncio.to_thread(report.write, args.report)
            if args.duration and time.monotonic() - start >= args.duration:
                exit_code = 0 if all(complete(snapshot) for snapshot in snapshots) else 1
                finish_reason = "duration_reached"
                return exit_code
            deadline = time.monotonic() + args.interval
            await asyncio.sleep(args.interval)
            lag_ms = max(0, 1000*(time.monotonic()-deadline))
    except Exception as exc:
        finish_reason = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if engine is not None:
            engine.state, engine.legs = "stopped", {}
        for pool in pools:
            pool.state = "stopped"
        stopped = [pool.snapshot(now_ms()) for pool in pools]
        try:
            await asyncio.to_thread(publish, args, stopped, "WebSocket")
        finally:
            report.finish(finish_reason, exit_code, stopped)
            await asyncio.to_thread(report.write, args.report)
