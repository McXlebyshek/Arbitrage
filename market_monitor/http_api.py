"""Local HTTP and SSE routes for the market monitor."""

import asyncio
from decimal import Decimal
import json
from pathlib import Path
import webbrowser

from aiohttp import web

from .books import BookBusy, BookService, BookUnavailable
from .contract_details import contract_limits
from .economics import EconomicBlock, economic_message, estimate_pair, fill
from .market import decimal, fresh, now_ms, pair_coverage, positive


FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'


def create_app(monitor, *, collect=True, open_url=None, clock=now_ms):
    @web.middleware
    async def local_requests(request, handler):
        endpoint = request.transport.get_extra_info('sockname') if request.transport else None
        if not endpoint or request.host.lower() not in (
            f'127.0.0.1:{endpoint[1]}', f'localhost:{endpoint[1]}',
        ):
            raise web.HTTPBadRequest(text='Недопустимый Host')
        return await handler(request)

    async def protect_response(_request, response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )

    app = web.Application(middlewares=[local_requests])
    app.on_response_prepare.append(protect_response)
    book_service = BookService(lambda key: monitor.instruments[key], monitor.clocks.get, clock)

    async def lifecycle(_app):
        task = asyncio.create_task(monitor.run()) if collect else None
        browser_task = None
        if open_url:
            async def open_browser():
                await asyncio.sleep(0.8)
                await asyncio.to_thread(webbrowser.open, open_url)

            browser_task = asyncio.create_task(open_browser())
        try:
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if browser_task:
                browser_task.cancel()
                await asyncio.gather(browser_task, return_exceptions=True)
            await book_service.close()
            await monitor.close()

    def payload(data):
        response = web.json_response(data, headers={'Cache-Control': 'no-store'})
        response.enable_compression()
        return response

    async def snapshot(_request):
        representation = monitor.compact_snapshot_response if _request.query.get('compact') == '1' else None
        snapshot_body, snapshot_etag = representation or monitor.snapshot_response
        headers = {'Cache-Control': 'no-store', 'ETag': snapshot_etag}
        candidates = _request.headers.get('If-None-Match', '').split(',')
        if any(tag.strip() in ('*', snapshot_etag, snapshot_etag[2:]) for tag in candidates):
            return web.Response(status=304, headers=headers)
        response = web.Response(body=snapshot_body, content_type='application/json',
                                headers=headers)
        response.enable_compression()
        return response

    async def market_metadata(request):
        body = monitor.market_metadata.get(request.match_info['version'])
        if body is None:
            raise web.HTTPNotFound(text='Версия каталога уже недоступна')
        response = web.Response(body=body, content_type='application/json',
                                headers={'Cache-Control': 'private, max-age=86400, immutable'})
        response.enable_compression()
        return response

    async def health(request):
        now = clock()
        result = monitor.health(now)
        if request.query.get('coverage') == '1':
            result['pairCoverage'] = await asyncio.to_thread(pair_coverage, monitor.snapshot['markets'], now)
        return payload(result)

    stream_stopping = asyncio.Event()

    async def stop_streams(_app):
        stream_stopping.set()
        monitor.early_pair_changed.set()

    async def early_quotes(request):
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store'})
        await response.prepare(request)
        sent_snapshot = None
        try:
            while not stream_stopping.is_set():
                changed = monitor.early_pair_changed
                pair = monitor.early_pair
                message = (pair if pair is not None and pair['expiresAt'] > clock()
                           else dict(baseAt=monitor.snapshot['generatedAt'], legs=None))
                frames = f'data: {json.dumps(message)}\n\n'
                generated = monitor.snapshot['generatedAt']
                if generated != sent_snapshot:
                    frames += f'event: snapshot-ready\ndata: {json.dumps(dict(generatedAt=generated))}\n\n'
                    sent_snapshot = generated
                if monitor.futures_feeds or monitor.wide_futures_feeds:
                    now = clock()
                    event = dict(baseAt=monitor.snapshot['generatedAt'],
                                 pairs=[pair for pair in monitor.futures_pairs if pair['expiresAt'] > now])
                    frames += f'event: futures-quotes\ndata: {json.dumps(event)}\n\n'
                async with asyncio.timeout(3):
                    await response.write(frames.encode())
                try:
                    async with asyncio.timeout(10):
                        await changed.wait()
                except TimeoutError:
                    if request.transport is None or request.transport.is_closing():
                        break
        except (ConnectionError, TimeoutError):
            pass
        return response

    def instrument(request):
        key = request.match_info['key']
        if key not in monitor.instruments:
            raise web.HTTPNotFound(text='Рынок недоступен в текущем каталоге')
        return key

    async def details(request):
        key = instrument(request)
        details, samples = await asyncio.gather(
            monitor.detail(key), monitor.history.run(monitor.history.read, key, clock()))
        source, market = monitor.instruments.get(key, (None, None))
        limits = contract_limits(source.venue, market) if source and source.kind == 'swap' else None
        networks = monitor.network_details(source.venue, market.get('base'))[0] if source else []
        return payload(dict(details, samples=samples, contractLimits=limits, networks=networks))

    async def get_book(key):
        try:
            return await book_service.get(key)
        except BookBusy as exc:
            raise web.HTTPTooManyRequests(text=str(exc)) from exc
        except BookUnavailable as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc

    async def book(request):
        return payload(await get_book(instrument(request)))

    async def executable_spread(request):
        try:
            data = await request.json()
            nominal = positive(data['nominal'])
            pairs = data['pairs']
            if not Decimal(1) <= nominal <= Decimal(1_000_000) or not isinstance(pairs, list) or not 1 <= len(pairs) <= 2:
                raise ValueError
            if not all(isinstance(pair, list) and len(pair) == 2 and
                       all(isinstance(key, str) and key in monitor.instruments for key in pair)
                       and pair[0] != pair[1] for pair in pairs):
                raise ValueError
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            raise web.HTTPBadRequest(text='Укажите номинал и до двух доступных связок') from exc

        async def measure(keys):
            legs = [monitor.instruments[key] for key in keys]
            (left_source, left), (right_source, right) = legs
            bindings = [monitor.registry.match(source.venue, source.kind, market) for source, market in legs]
            if (right_source.kind != 'swap' or left['quote'] != right['quote']
                    or not all(bindings) or bindings[0]['asset'] != bindings[1]['asset']
                    or bindings[0].get('price_units', 1) != bindings[1].get('price_units', 1)):
                return dict(executableSpread=None, reason='Нужны две совместимые ноги из реестра')
            try:
                books = await asyncio.gather(*(get_book(key) for key in keys))
                a, b = (book['sourceAt'] for book in books)
                now = clock()
                if not (fresh(a, now, 3000) and fresh(b, now, 3000) and abs(a - b) <= 1500):
                    raise EconomicBlock('Нужны синхронные стаканы с биржевым временем')
                asks = [(decimal(price), decimal(size)) for price, size in books[0]['asks']]
                bids = [(decimal(price), decimal(size)) for price, size in books[1]['bids']]
                quantity = nominal * 2 / (asks[0][0] + bids[0][0])
                ask, _ = fill(asks, quantity)
                bid, _ = fill(bids, quantity)
                return dict(executableSpread=float(200 * (bid - ask) / (ask + bid)),
                            expiresAt=min(a, b, *(book['receivedAt'] for book in books)) + 3000)
            except EconomicBlock as exc:
                return dict(executableSpread=None, reason=economic_message(str(exc)))
            except web.HTTPException as exc:
                return dict(executableSpread=None, reason=exc.text)

        return payload(dict(nominal=float(nominal), results=await asyncio.gather(*(measure(pair) for pair in pairs))))

    async def estimate(request):
        keys = [request.query.get(side) for side in ('long', 'short')]
        if keys[0] == keys[1] or any(key not in monitor.instruments for key in keys):
            raise web.HTTPBadRequest(text='Выберите два разных рынка')
        instruments = [monitor.instruments[key] for key in keys]
        sources, markets = zip(*instruments)
        bindings = [monitor.registry.match(source.venue, source.kind, market)
                    for source, market in instruments]
        if (any(source.kind != 'swap' for source in sources)
                or not all(bindings) or bindings[0]['asset'] != bindings[1]['asset']
                or bindings[0]['price_units'] != bindings[1]['price_units']
                or markets[0]['quote'] != markets[1]['quote']):
            raise web.HTTPBadRequest(text='Для расчёта нужна пара совместимых контрактов из реестра')
        try:
            budget = positive(request.query.get('budget'))
            fees = [decimal(request.query.get(key)) / 100 for key in ('longFee', 'shortFee')]
            if budget > 1_000_000 or any(not 0 <= fee <= Decimal('.01') for fee in fees):
                raise ValueError('Бюджет до 1 000 000, комиссии от 0 до 1%')
            books = await asyncio.gather(*(get_book(key) for key in keys))
            return payload(estimate_pair(instruments, books, budget, fees, clock()))
        except EconomicBlock as exc:
            raise web.HTTPUnprocessableEntity(text=economic_message(str(exc))) from exc
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise web.HTTPUnprocessableEntity(text='Проверьте бюджет и ставки комиссий') from exc

    async def history_watch(request):
        try:
            payload = await request.json()
            keys, coins = payload['markets'], payload['coins']
            if not isinstance(keys, list) or not isinstance(coins, list) or len(keys) > 2 or len(coins) > 10_000:
                raise ValueError
            if any(not isinstance(value, str) or len(value) > 100 for value in (*keys, *coins)):
                raise ValueError
        except (ValueError, TypeError, KeyError) as exc:
            raise web.HTTPBadRequest(text='Неверный список наблюдаемых рынков') from exc
        monitor.watch_history(keys, coins, clock())
        return web.Response(status=204, headers={'Cache-Control': 'no-store'})

    async def index(_request):
        return web.FileResponse(FRONTEND / 'index.html')

    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(stop_streams)
    app.router.add_get('/api/markets', snapshot)
    app.router.add_get('/api/meta/{version}', market_metadata)
    app.router.add_get('/api/health', health)
    app.router.add_post('/api/history/watch', history_watch)
    app.router.add_get('/api/markets/early', early_quotes)
    app.router.add_get('/api/market/{key}/detail', details)
    app.router.add_get('/api/market/{key}/book', book)
    app.router.add_get('/api/estimate', estimate)
    app.router.add_post('/api/executable-spread', executable_spread)
    app.router.add_get('/', index)
    app.router.add_static('/', FRONTEND)
    return app
