"""Local HTTP, streaming, and bounded depth handlers for the public monitor."""

import asyncio
from decimal import Decimal
import logging
import webbrowser

import aiohttp
from aiohttp import web
import ccxt.async_support as ccxt
import orjson

from .contract_details import contract_limits
from .market import decimal, fresh, positive
from .pair_economics import estimate_pair, normalized_book
from .thresholds import BOOK_MAX_AGE_MS


LOG = logging.getLogger(__name__)
FUTURES_QUOTE_FIELDS = (
    'id', 'bid', 'ask', 'sourceAt', 'receivedAt', 'quoteSource', 'confirmedAt',
    'validUntil', 'changedAt', 'suspect',
)


def compact_futures_quotes(base_at, pairs):
    """Keep the SSE quote event dynamic; instrument descriptions come from /api/meta."""
    return dict(baseAt=base_at, pairs=[
        dict(coin=pair['coin'], expiresAt=pair['expiresAt'], timestampSuspect=pair['timestampSuspect'],
             legs=[{key: leg[key] for key in FUTURES_QUOTE_FIELDS if key in leg} for leg in pair['legs']])
        for pair in pairs
    ])


def create_app(monitor, *, frontend, now_ms, interval, collect=True, open_url=None):
    @web.middleware
    async def local_host(request, handler):
        address = request.transport.get_extra_info('sockname') if request.transport else None
        if not address or request.headers.getall('Host', []) not in (
            [f'127.0.0.1:{address[1]}'], [f'localhost:{address[1]}'],
        ):
            raise web.HTTPBadRequest(text='Недопустимый адрес локального монитора')
        return await handler(request)

    async def security_headers(_request, response):
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers['X-Content-Type-Options'] = 'nosniff'

    app = web.Application(middlewares=[local_host])
    app.on_response_prepare.append(security_headers)
    depth = {}
    depth_pending = {}
    snapshot_lock = asyncio.Lock()

    async def lifecycle(_app):
        task = asyncio.create_task(monitor.run()) if collect else None
        if task is not None:
            def monitor_finished(done):
                monitor.cancel_event_loop_lag_probe()
                if done.cancelled():
                    return
                monitor.collection_failed = True
                error = done.exception()
                if error is None:
                    LOG.error('Monitor collection task stopped unexpectedly')
                else:
                    LOG.error('Monitor collection task crashed',
                              exc_info=(type(error), error, error.__traceback__))

            task.add_done_callback(monitor_finished)
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
            pending = tuple(depth_pending.values())
            for pending_task in pending:
                pending_task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await monitor.close()

    def payload(data):
        response = web.json_response(data, headers={'Cache-Control': 'no-store'})
        response.enable_compression()
        return response

    async def snapshot(_request):
        compact = monitor.compact_snapshot
        if _request.query.get('compact') == '1' and compact and compact[0] is monitor.snapshot:
            _, snapshot_body, snapshot_etag = compact
        else:
            async with snapshot_lock:
                while not monitor.encoded_snapshot or monitor.encoded_snapshot[0] is not monitor.snapshot:
                    current = monitor.snapshot
                    body = await asyncio.to_thread(orjson.dumps, current)
                    if monitor.snapshot is current:
                        monitor.remember_snapshot(current, body)
                _, snapshot_body, snapshot_etag = monitor.encoded_snapshot
        headers = {'Cache-Control': 'no-store', 'ETag': snapshot_etag}
        candidates = _request.headers.get('If-None-Match', '').split(',')
        if any(tag.strip() in ('*', snapshot_etag, snapshot_etag[2:]) for tag in candidates):
            return web.Response(status=304, headers=headers)
        response = web.Response(body=snapshot_body, content_type='application/json',
                                headers=headers, zlib_executor_size=64 * 1024)
        response.enable_compression()
        return response

    async def metadata(request):
        version = request.query.get('version') or monitor.metadata_version
        body = monitor.metadata_packets.get(version)
        if body is None:
            raise web.HTTPNotFound(text='Версия каталога снимка недоступна')
        response = web.Response(body=body, content_type='application/json', zlib_executor_size=64 * 1024)
        response.enable_compression()
        return response

    stream_stopping = asyncio.Event()

    async def stop_streams(_app):
        stream_stopping.set()
        monitor.early_pair_changed.set()

    async def early_quotes(request):
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store'})
        await response.prepare(request)
        last_publication = None
        try:
            while not stream_stopping.is_set():
                changed = monitor.early_pair_changed
                pair = monitor.early_pair
                message = (pair if pair is not None and pair['expiresAt'] > now_ms()
                           else dict(baseAt=monitor.snapshot['generatedAt'], legs=None))
                frames = bytearray()
                publication = monitor.snapshot['generatedAt']
                if publication is not None and last_publication != monitor.snapshot_revision:
                    notice = dict(generatedAt=publication, intervalMs=interval * 1000)
                    frames.extend(b'event: snapshot-ready\ndata: ')
                    frames.extend(orjson.dumps(notice))
                    frames.extend(b'\n\n')
                    last_publication = monitor.snapshot_revision
                frames.extend(b'data: ')
                frames.extend(orjson.dumps(message))
                frames.extend(b'\n\n')
                if monitor.futures_feeds:
                    now = now_ms()
                    pairs = [pair for pair in monitor.futures_pairs if pair['expiresAt'] > now]
                    event = compact_futures_quotes(monitor.snapshot['generatedAt'], pairs)
                    frames.extend(b'event: futures-quotes\ndata: ')
                    frames.extend(orjson.dumps(event))
                    frames.extend(b'\n\n')
                async with asyncio.timeout(3):
                    await response.write(bytes(frames))
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
        expected = monitor.instruments[key]
        details, samples = await asyncio.gather(
            monitor.detail(key), asyncio.to_thread(monitor.history.read, key, now_ms()))
        if not monitor.current_instrument(key, expected):
            raise web.HTTPServiceUnavailable(text='Каталог инструмента обновляется')
        source, market = expected
        limits = contract_limits(source.venue, market) if source.kind == 'swap' else None
        return payload(dict(details, samples=samples, contractLimits=limits))

    async def get_book(key):
        expected = monitor.instruments.get(key)
        if expected is None or not monitor.current_instrument(key, expected):
            raise web.HTTPServiceUnavailable(text='Каталог инструмента обновляется')
        cached_instrument, cached = depth.get(key, (None, None))
        now = now_ms()
        if (cached and cached_instrument == expected and fresh(cached['receivedAt'], now, BOOK_MAX_AGE_MS)
                and (cached['sourceAt'] is None or fresh(cached['sourceAt'], now, BOOK_MAX_AGE_MS))):
            return cached
        task = depth_pending.get(key)
        if task is None:
            if len(depth_pending) >= 4:
                raise web.HTTPTooManyRequests(text='Лимит одновременных запросов стакана')
            task = depth_pending[key] = asyncio.create_task(load_book(key, expected))
            # Retrieve failures even if all HTTP callers have disconnected.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        result = await asyncio.shield(task)
        if not monitor.current_instrument(key, expected):
            raise web.HTTPServiceUnavailable(text='Каталог инструмента обновляется')
        return result

    async def load_book(key, expected):
        try:
            return await fetch_book(key, expected)
        finally:
            depth_pending.pop(key, None)

    async def fetch_book(key, expected):
        source, market = expected
        try:
            async with asyncio.timeout(8):
                raw = await source.public_book(market)
                if raw.get('symbol') not in (None, market['symbol']):
                    raise ValueError('стакан другого инструмента')
                size = market['contractSize'] if source.kind == 'swap' else 1
                result = normalized_book(raw, size, now_ms(), clock=getattr(source, 'clock', None))
        except (aiohttp.ClientError, ccxt.BaseError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            LOG.warning('Depth %s: %s', key, str(exc)[:250])
            raise web.HTTPServiceUnavailable(text='Публичный стакан временно недоступен') from exc
        if not monitor.current_instrument(key, expected):
            raise web.HTTPServiceUnavailable(text='Каталог инструмента обновляется')
        depth_copy = {item: value for item, value in depth.items()
                      if fresh(value[1]['receivedAt'], now_ms(), BOOK_MAX_AGE_MS)}
        depth.clear()
        depth.update(depth_copy)
        depth[key] = (expected, result)
        return result

    async def book(request):
        return payload(await get_book(instrument(request)))

    def estimate_instruments(keys):
        if len(keys) != 2 or keys[0] == keys[1] or any(key not in monitor.instruments for key in keys):
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
        return instruments

    async def estimate(request):
        keys = [request.query.get(side) for side in ('long', 'short')]
        instruments = estimate_instruments(keys)
        try:
            budget = positive(request.query.get('budget'))
            fees = [decimal(request.query.get(key)) / 100 for key in ('longFee', 'shortFee')]
            if budget > 1_000_000 or any(not 0 <= fee <= Decimal('.01') for fee in fees):
                raise ValueError('Бюджет до 1 000 000, комиссии от 0 до 1%')
            books = await asyncio.gather(*(get_book(key) for key in keys))
            if not all(monitor.current_instrument(key, expected) for key, expected in zip(keys, instruments)):
                raise web.HTTPServiceUnavailable(text='Каталог инструмента обновляется')
            return payload(estimate_pair(instruments, books, budget, fees, now_ms()))
        except ValueError as exc:
            raise web.HTTPUnprocessableEntity(text=str(exc)) from exc

    async def executable_spreads(request):
        pairs = request.query.getall('pair', [])
        if not 1 <= len(pairs) <= 2 or len(set(pairs)) != len(pairs):
            raise web.HTTPBadRequest(text='Укажите одну или две разные связки')
        budget = Decimal(1000)

        async def spread(pair):
            keys = pair.split('~')
            try:
                instruments = estimate_instruments(keys)
                books = await asyncio.gather(*(get_book(key) for key in keys))
                if not all(monitor.current_instrument(key, item) for key, item in zip(keys, instruments)):
                    raise web.HTTPServiceUnavailable(text='Каталог инструмента обновляется')
                estimate = estimate_pair(instruments, books, budget, (Decimal(0), Decimal(0)), now_ms(),
                                         require_full_entry=True)
                long_ask, short_bid = estimate['entry']
                expires_at = min(book[field] for book in books for field in ('sourceAt', 'receivedAt')) + BOOK_MAX_AGE_MS
                return dict(spread=200 * (short_bid - long_ask) / (short_bid + long_ask), expiresAt=expires_at)
            except web.HTTPException as exc:
                return dict(error=exc.text)
            except ValueError as exc:
                return dict(error=str(exc))

        return payload(dict(nominal=int(budget), pairs=dict(zip(pairs, await asyncio.gather(*(spread(pair) for pair in pairs))))))

    async def index(_request):
        return web.FileResponse(frontend / 'index.html')

    async def health(_request):
        return payload(monitor.health(now_ms()))

    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(stop_streams)
    app.router.add_get('/api/markets', snapshot)
    app.router.add_get('/api/meta', metadata)
    app.router.add_get('/api/health', health)
    app.router.add_get('/api/markets/early', early_quotes)
    app.router.add_get('/api/market/{key}/detail', details)
    app.router.add_get('/api/market/{key}/book', book)
    app.router.add_get('/api/estimate', estimate)
    app.router.add_get('/api/executable-spreads', executable_spreads)
    app.router.add_get('/', index)
    app.router.add_static('/', frontend)
    return app
