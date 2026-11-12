"""Local fixture server for Playwright. All quotes are synthetic."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import monitor as module


async def main():
    snapshot = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    now = snapshot['generatedAt']
    monitor = module.Monitor([], Path('unused.sqlite3'))
    await monitor.publish_snapshot(snapshot)
    monitor.detail = AsyncMock(return_value=dict(errors={}, totals={}, events=[], refreshAfter=now + 60000))
    monitor.history.read = lambda *_: []
    monitor.registry.match = lambda venue, kind, market: dict(asset=market['symbol'].split('.')[0], price_units=1)
    for legs in snapshot['markets'].values():
        for leg in legs:
            source = SimpleNamespace(venue='test', kind=leg['type'], public_book=AsyncMock(
                return_value=dict(timestamp=now, bids=[[100, 10]], asks=[[101, 10]])))
            monitor.instruments[leg['id']] = source, dict(symbol=leg['id'], quote=leg['quote'], contractSize=1)
    monitor.futures_feeds = {'test': SimpleNamespace(close=AsyncMock(), reconnects=0)}
    app = module.create_app(monitor, collect=False, clock=lambda: now)
    stopped = asyncio.Event()

    async def quotes(request):
        event = await request.json()
        monitor.futures_pairs = event['pairs']
        monitor.notify_quotes_changed()
        return web.json_response({'ok': True})

    async def publish(_request):
        monitor.futures_pairs = []
        await monitor.publish_snapshot(dict(monitor.snapshot, generatedAt=now + 1))
        return web.json_response({'ok': True})

    async def stop(_request):
        stopped.set()
        return web.json_response({'ok': True})

    app.router.add_post('/__test/quotes', quotes)
    app.router.add_post('/__test/publish', publish)
    app.router.add_post('/__test/stop', stop)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        print('PORT=' + str(site._server.sockets[0].getsockname()[1]), flush=True)
        async with asyncio.timeout(180):
            await stopped.wait()
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
