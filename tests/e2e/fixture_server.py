"""Local browser test server with controllable, artificial market snapshots."""

from pathlib import Path

from aiohttp import web

from monitor import Monitor, create_app, now_ms


def fixture_snapshot(*, expired=False):
    published = now_ms()
    stamp = published - 61_000 if expired else published
    markets = {}
    for coin, base in (('BTC', 100), ('ETH', 500)):
        markets[coin] = [
            dict(id=f'{venue}.swap.{coin}.USDT', exchange=name, type='swap', quote='USDT',
                 symbol=f'{coin}/USDT:USDT', bid=bid, ask=bid + 1, last=bid, asset=coin,
                 size=1, sourceAt=stamp, receivedAt=stamp, index=base, oi=None, volume=10,
                 totals={}, networks=[], rate=rate, hours=8, next=published + 60_000,
                 cap=None, floor=None)
            for venue, name, bid, rate in (('binanceusdm', 'Binance', base, 0.001),
                                            ('bybit', 'Bybit', base + 10, 0.01))
        ]
    return dict(state='ready', markets=markets, venues={}, valuations={}, generatedAt=published)


def app():
    monitor = Monitor((), Path('unused.sqlite3'))
    application = create_app(monitor, collect=False)

    async def publish(request):
        await monitor.publish_snapshot(fixture_snapshot(expired=request.query.get('expired') == '1'))
        return web.json_response({'generatedAt': monitor.snapshot['generatedAt']})

    application.router.add_post('/fixture/publish', publish)
    application.on_startup.append(publish_initial(monitor))
    return application


def publish_initial(monitor):
    async def initialize(_app):
        await monitor.publish_snapshot(fixture_snapshot())

    return initialize


if __name__ == '__main__':
    web.run_app(app(), host='127.0.0.1', port=18784)
