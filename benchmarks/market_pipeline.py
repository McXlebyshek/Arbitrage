"""Offline fixture server for the market pipeline benchmark."""

import asyncio
import os
from pathlib import Path
import platform
import sys
from time import perf_counter

import aiohttp
from aiohttp import web
import orjson

sys.path.insert(0, os.environ.get('ARBITRAGE_TARGET', str(Path(__file__).resolve().parents[1])))
import monitor as module  # noqa: E402


NOW = 1_800_000_000_000
VENUES = ('binanceusdm', 'bybit', 'okx', 'bitget', 'gate', 'mexc')
COINS = 1000
ITERATIONS = 21
WARMUPS = 5


def create_monitor():
    monitor = module.Monitor(VENUES, Path('unused-benchmark.sqlite3'))
    for index, ((venue, kind), source) in enumerate(monitor.sources.items()):
        markets, tickers, currencies = {}, {}, {}
        for number in range(COINS):
            base = f'TEST{number:04d}'
            quote = 'USDT' if number % 5 else 'USDC'
            symbol = f'{base}/{quote}' + (f':{quote}' if kind == 'swap' else '')
            market = dict(id=symbol, symbol=symbol, base=base, quote=quote, settle=quote,
                          active=True, swap=kind == 'swap', spot=kind == 'spot', linear=True, contractSize=1)
            price = 100 + (number + index) % 7 / 10
            markets[symbol] = market
            tickers[symbol] = dict(symbol=symbol, bid=price, ask=price + 1, last=price,
                                   timestamp=NOW, quoteVolume=5_000_000, fundingRate=index / 100_000,
                                   fundingTimestamp=NOW + 30_000, interval='8h')
            if kind == 'spot':
                currencies[base] = dict(networks={'ERC20': {'deposit': True, 'withdraw': True}})
        source.catalogs[venue] = markets
        source.catalog_at = NOW
        source.batches[venue] = dict(tickers=tickers, received_at_ms=NOW)
        source.currencies = currencies
    return monitor


def measure_build(monitor, *, cold):
    samples = []
    for _ in range(WARMUPS + ITERATIONS):
        if cold:
            monitor.catalog_descriptors.clear()
            monitor.network_descriptors.clear()
        start = perf_counter()
        markets = monitor.snapshot_markets(NOW)
        samples.append((perf_counter() - start) * 1000)
        assert len(markets) == COINS
        assert sum(map(len, markets.values())) == 12 * COINS
    return samples[WARMUPS:]


async def main():
    monitor = create_monitor()
    module.now_ms = lambda: NOW
    markets = monitor.snapshot_markets(NOW)
    generation = NOW
    stopped = asyncio.Event()

    async def publish_snapshot():
        nonlocal generation
        generation += 1
        snapshot = dict(state='ready', generatedAt=generation, expectedIntervalMs=15_000,
                        markets=markets, venues={}, valuations={})
        started = perf_counter()
        if hasattr(monitor, 'publish_snapshot'):
            await monitor.publish_snapshot(snapshot)
        else:
            monitor.snapshot = snapshot
            monitor.notify_quotes_changed()
        return generation, (perf_counter() - started) * 1000

    async def publish(_request):
        generated, elapsed = await publish_snapshot()
        return web.json_response(dict(generatedAt=generated, publicationMs=elapsed))

    async def build(_request):
        return web.json_response(dict(
            cold=measure_build(monitor, cold=True),
            warm=measure_build(monitor, cold=False),
            python=platform.python_version(), aiohttp=aiohttp.__version__, orjson=orjson.__version__,
        ))

    async def stop(_request):
        stopped.set()
        return web.Response()

    await publish_snapshot()
    app = module.create_app(monitor, collect=False)
    app.router.add_post('/__bench/publish', publish)
    app.router.add_get('/__bench/build', build)
    app.router.add_post('/__bench/stop', stop)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, '127.0.0.1', 0).start()
        print(f'PORT={runner.addresses[0][1]}', flush=True)
        async with asyncio.timeout(180):
            await stopped.wait()
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
