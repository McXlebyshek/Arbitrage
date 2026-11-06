import asyncio
import time
import unittest
from unittest.mock import patch

from aiohttp import web

from arbitrage.live import LiveExchange
from arbitrage.streams import URLS


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_fractional_gate_sizes_are_valid_and_zero_unknown_negative_are_not(self):
        worker=LiveExchange('gate',{'BTC'})
        worker.markets={'BTC_USDT':dict(symbol='BTC/USDT:USDT',id='BTC_USDT',base='BTC',quote='USDT',contractSize=1)}
        def payload(size):return dict(channel='futures.book_ticker',event='update',result=dict(s='BTC_USDT',b='100',a='101',B=size,A='2',t=10000))
        quote=(await worker.parse_quotes(None,payload('0.25'),10100))[0]
        self.assertEqual(quote.quality(10100),'ok')
        self.assertEqual(worker.fractional_size_updates,1)
        for size in ('0','-0.1',None,'NaN'):
            with self.subTest(size=size):
                quote=(await worker.parse_quotes(None,payload(size),10100))[0]
                self.assertIsNone(quote.bid)
                self.assertEqual(quote.quality(10100),'missing_bid_ask')

    async def test_continuous_stale_frames_do_not_keep_connection_healthy_forever(self):
        async def handler(request):
            socket=web.WebSocketResponse();await socket.prepare(request)
            await socket.receive_json()
            try:
                while not socket.closed:
                    await socket.send_json(dict(channel='futures.book_ticker',event='update',
                        result=dict(s='BTC_USDT',b='100',a='101',B='1',A='1',t=int(time.time()*1000)-5000)))
                    await asyncio.sleep(.01)
            except (ConnectionError,RuntimeError):pass
            return socket
        application=web.Application();application.router.add_get('/ws',handler)
        runner=web.AppRunner(application);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        worker=LiveExchange('gate',{'BTC'});worker.fresh_data_timeout=.05
        worker.markets={'BTC_USDT':dict(symbol='BTC/USDT:USDT',id='BTC_USDT',base='BTC',quote='USDT',contractSize=1)}
        with patch.dict(URLS,{'gate':f'http://127.0.0.1:{runner.addresses[0][1]}/ws'}):
            task=asyncio.create_task(worker.run())
            try:
                async with asyncio.timeout(3):
                    while worker.stale_stream_reconnects==0:await asyncio.sleep(.01)
                    while worker.state!='reconnecting':await asyncio.sleep(.01)
                self.assertEqual(worker.snapshot(int(time.time()*1000))['status']['usable_quotes'],0)
                self.assertIn('No fresh market-data',worker.last_error)
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True);await runner.cleanup()

    async def test_burst_is_drained_without_freshening_delayed_or_reversed_data(self):
        sent=asyncio.Event()
        async def handler(request):
            socket=web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_json()
            stamp=int(time.time()*1000)-5000
            for index in range(3000):
                await socket.send_json(dict(channel='futures.book_ticker',event='update',time_ms=stamp+5000,
                    result=dict(s='BTC_USDT',b='100',a='101',B='1',A='1',t=stamp)))
            await socket.send_json(dict(channel='futures.book_ticker',event='update',
                result=dict(s='BTC_USDT',b='999',a='1000',B='1',A='1',t=stamp-1000)))
            sent.set()
            await socket.receive()
            return socket
        application=web.Application();application.router.add_get('/ws',handler)
        runner=web.AppRunner(application);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        worker=LiveExchange('gate',{'BTC'})
        worker.markets={'BTC_USDT':dict(symbol='BTC/USDT:USDT',id='BTC_USDT',base='BTC',quote='USDT',contractSize=1)}
        with patch.dict(URLS,{'gate':f'http://127.0.0.1:{runner.addresses[0][1]}/ws'}):
            task=asyncio.create_task(worker.run())
            try:
                async with asyncio.timeout(10):
                    await sent.wait()
                    while worker.out_of_order_updates<1: await asyncio.sleep(.01)
                status=worker.snapshot(int(time.time()*1000))['status']
                self.assertEqual(worker.updates,3000)
                self.assertEqual(status['received_stale_updates'],3001)
                self.assertEqual(status['source_lag_samples'],2048)
                self.assertGreaterEqual(status['source_lag_p95_ms'],5000)
                self.assertEqual(status['source_to_event_p95_ms'],5000)
                self.assertEqual(status['usable_quotes'],0)
                self.assertEqual(next(iter(worker.quotes.values())).bid,100)
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
                await runner.cleanup()

    async def test_real_ws_close_resubscribes_and_replaces_old_quote(self):
        connections = []
        decimal_headers = []
        drop = asyncio.Event()

        async def handler(request):
            decimal_headers.append(request.headers.get('X-Gate-Size-Decimal'))
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            subscription = await socket.receive_json()
            connections.append(subscription)
            price = 100 if len(connections) == 1 else 200
            await socket.send_json(dict(channel="futures.book_ticker", event="update",
                result=dict(s="BTC_USDT", b=str(price), a=str(price + 1),
                            B="1", A="1", t=int(time.time() * 1000))))
            if len(connections) == 1:
                await drop.wait()
                await socket.close()
            else:
                await socket.receive()
            return socket

        application = web.Application()
        application.router.add_get("/ws", handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        worker = LiveExchange("gate", {"BTC"})
        worker.markets = {"BTC_USDT": dict(symbol="BTC/USDT:USDT", id="BTC_USDT",
                                          base="BTC", quote="USDT", contractSize=1)}

        async def until(predicate):
            async with asyncio.timeout(10):
                while not predicate():
                    await asyncio.sleep(0.01)

        with patch.dict(URLS, {"gate": f"http://127.0.0.1:{port}/ws"}):
            task = asyncio.create_task(worker.run())
            try:
                await until(lambda: worker.state == "streaming")
                self.assertEqual(next(iter(worker.quotes.values())).bid, 100)
                drop.set()
                await until(lambda: worker.state == "reconnecting")
                self.assertEqual(worker.snapshot(int(time.time()*1000))["status"]["usable_quotes"], 0)
                await until(lambda: len(connections) >= 2 and worker.state == "streaming")
                self.assertEqual(next(iter(worker.quotes.values())).bid, 200)
                self.assertEqual(connections[0]["payload"], connections[1]["payload"])
                self.assertGreaterEqual(worker.reconnects, 1)
                self.assertEqual(decimal_headers[:2],['1','1'])
            finally:
                drop.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await runner.cleanup()
