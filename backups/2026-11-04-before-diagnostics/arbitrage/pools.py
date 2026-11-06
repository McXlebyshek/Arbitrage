"""Connection groups, coverage accounting and periodic instrument refresh."""
import asyncio
import time

from .live import LiveExchange, RequestLimiter, now_ms


def structural(markets):
    fields = ("id", "symbol", "base", "quote", "settle", "contractSize", "active", "linear", "swap", "precision", "limits")
    return {key: {field: market.get(field) for field in fields} for key, market in markets.items()}


class ExchangePool:
    def __init__(self, exchange, bases, registry, group_size=20, max_connections=8,
                 refresh_seconds=900, mexc_depth_limit=20):
        self.exchange, self.bases, self.registry = exchange, bases, registry
        self.group_size = min(group_size, 5) if exchange == "mexc" else group_size
        self.max_connections = max_connections
        self.refresh_seconds = refresh_seconds
        self.mexc_depth_limit = mexc_depth_limit
        self.groups = {}
        self.catalog, self.selected = {}, {}
        self.loaded_at_ms = None
        self.metadata_error = None
        self.state = "loading_markets"
        self.generation = 0
        self.omitted = 0
        self.snapshot_limiter = RequestLimiter()
        self.connection_limiter = RequestLimiter(interval=1.1)

    def choose(self, markets):
        requested = {key: m for key, m in markets.items() if self.bases is None or m["base"] in self.bases}
        # Known instrument identities get capacity first; ticker equality does not grant scanner admission.
        ordered = sorted(requested, key=lambda key: ((self.exchange, key) not in self.registry.bindings, key))
        capacity = self.group_size * self.max_connections
        if self.exchange == "mexc":
            capacity = min(capacity, self.mexc_depth_limit)
        selected = {key: requested[key] for key in ordered[:capacity]}
        return selected, len(requested) - len(selected)

    async def stop_group(self, key):
        worker, task = self.groups.pop(key)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        worker.state = "stopped"

    async def reconcile(self, markets):
        selected, omitted = self.choose(markets)
        keys = sorted(selected)
        assignments = {index // self.group_size: {key: selected[key] for key in keys[index:index+self.group_size]}
                       for index in range(0, len(keys), self.group_size)}
        for key, (worker, task) in list(self.groups.items()):
            if task.done():
                task.result()
            if key not in assignments or structural(worker.markets) != structural(assignments[key]):
                await self.stop_group(key)
        for key, assigned in assignments.items():
            if key not in self.groups:
                worker = LiveExchange(self.exchange, {m["base"] for m in assigned.values()})
                worker.markets = assigned
                worker.snapshot_limiter = self.snapshot_limiter
                worker.connection_limiter = self.connection_limiter
                task = asyncio.create_task(worker.run(), name=f"{self.exchange}-{key}")
                self.groups[key] = (worker, task)
                await asyncio.sleep(0.25)  # Pace new connections on catalog changes.
        self.catalog, self.selected, self.omitted = markets, selected, omitted
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
                "updates": sum(s["updates"] for s in group_statuses),
                "reconnects": sum(s["reconnects"] for s in group_statuses),
                "book_resyncs": sum(s["book_resyncs"] for s in group_statuses),
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
        pools.append(ExchangePool(exchange, bases, args.registry, args.group_size,
                     args.max_connections, args.market_refresh_seconds, args.mexc_depth_limit))
    tasks = [asyncio.create_task(pool.run(), name=f"pool-{pool.exchange}") for pool in pools]
    start = time.monotonic()
    try:
        while True:
            for task in tasks:
                if task.done():
                    task.result()
            snapshots = [pool.snapshot(now_ms()) for pool in pools]
            await asyncio.to_thread(publish, args, snapshots, "WebSocket")
            if args.duration and time.monotonic() - start >= args.duration:
                return 0 if all(complete(snapshot) for snapshot in snapshots) else 1
            await asyncio.sleep(args.interval)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for pool in pools:
            pool.state = "stopped"
        await asyncio.to_thread(publish, args, [pool.snapshot(now_ms()) for pool in pools], "WebSocket")
