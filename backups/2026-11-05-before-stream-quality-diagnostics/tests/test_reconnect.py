import asyncio
import time
import unittest
from unittest.mock import patch

from aiohttp import web

from arbitrage.live import LiveExchange
from arbitrage.streams import URLS


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_ws_close_resubscribes_and_replaces_old_quote(self):
        connections = []
        drop = asyncio.Event()

        async def handler(request):
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
            finally:
                drop.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await runner.cleanup()
