import asyncio
from decimal import Decimal
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web

from validate_gate_obu import Book, Comparison, DEPTH, TICKER


def snapshot(uid=10):
    return dict(full=True, s="ob.TRX_USDT.50", u=uid, t=10000,
                b=[["10", "0.2"], ["9", "3"]], a=[["11", "4"]])


def update(first, last, **fields):
    return dict(s="ob.TRX_USDT.50", U=first, u=last, t=10001, **fields)


class BookTests(unittest.TestCase):
    def test_replace_and_delete(self):
        book = Book()
        book.apply(snapshot())
        top = book.apply(update(11, 15, b=[["10", "0"]], a=[["11", "0.1"]]))
        self.assertEqual((top.bid, top.ask_size), (Decimal("9"), Decimal("0.1")))
        self.assertEqual(book.apply(update(16, 16)).uid, 16)
        replacement = snapshot(20)
        replacement["b"] = [["8", "1"]]
        self.assertEqual(book.apply(replacement).bid, Decimal("8"))

    def test_invalid_update_clears_all_state(self):
        cases = [update(10, 11), update(12, 13), update(11, 10),
                 update(11, 12, b=[["12", "3"]]), update(11, 12, a=[["11", "0"]]),
                 update(11, 12, a=[["12", "NaN"]]), update(11, 12, b=[["10", "-1"]]),
                 update(11, 12, b=[["10"]]), dict(snapshot(), t=True)]
        for row in cases:
            with self.subTest(row=row):
                book = Book()
                book.apply(snapshot())
                with self.assertRaises(ValueError):
                    book.apply(row)
                self.assertIsNone(book.uid)
                self.assertFalse(book.bids or book.asks)
                with self.assertRaises(ValueError):
                    book.apply(update(13, 14))
                self.assertEqual(book.apply(snapshot(20)).uid, 20)

    def test_freshness_and_equal_id_comparison(self):
        state = Comparison("TRX_USDT")
        top = state.book.apply(snapshot())
        state.record(TICKER, top, 10010)
        state.record(DEPTH, top, 10020)
        self.assertEqual(state.counts["paired_price_and_size_equal"], 1)
        state.sample(13000)
        state.sample(13001)
        self.assertEqual(state.counts["both_fresh"], 1)
        self.assertEqual(state.counts["samples"], 2)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_gap_reconnects_and_waits_for_new_snapshot(self):
        state = Comparison("TRX_USDT")
        waiting = asyncio.Event()
        resume = asyncio.Event()
        recovered = asyncio.Event()
        attempts = 0

        async def handler(request):
            nonlocal attempts
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            attempts += 1
            await ws.receive_json()
            await ws.receive_json()
            if attempts == 1:
                for row in (snapshot(), update(12, 13)):
                    await ws.send_json(dict(channel=DEPTH, result=row))
            else:
                waiting.set()
                await resume.wait()
                await ws.send_json(dict(channel=DEPTH, result=snapshot(20)))
            async for _ in ws:
                pass
            return ws

        app = web.Application()
        app.router.add_get("/", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}/"

        original = state.record
        def observe(channel, top, received):
            original(channel, top, received)
            if top.uid == 20:
                recovered.set()

        with patch("validate_gate_obu.URL", url), patch.object(state, "record", observe):
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(state.collect(session))
                try:
                    await asyncio.wait_for(waiting.wait(), 6)
                    self.assertFalse(state.latest)
                    self.assertIsNone(state.book.uid)
                    resume.set()
                    await asyncio.wait_for(recovered.wait(), 3)
                    self.assertEqual(state.book.uid, 20)
                    self.assertEqual(state.errors["sequence gap"], 1)
                finally:
                    resume.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
