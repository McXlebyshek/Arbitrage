import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import ccxt.async_support as ccxt

from market_monitor.clock_calibration import (
    CONFIRM_SECONDS,
    MAX_AGE_SECONDS,
    REFRESH_SECONDS,
    ClockCalibration,
)
from market_monitor.market import fresh
from market_monitor.sources import Source

NOW = 1_800_000_000_000


class Timeline:
    def __init__(self):
        self.wall = NOW
        self.steady = 5_000_000

    def advance(self, milliseconds):
        self.wall += milliseconds
        self.steady += milliseconds

    def calibration(self, venue='bybit'):
        return ClockCalibration(venue, wall=lambda: self.wall, steady=lambda: self.steady / 1000)

    def exchange(self, clock, *, offset=0, rtt=80, queue=0, wall_step=0):
        async def transport():
            exchange_stamp = self.wall + offset + rtt // 2
            self.advance(rtt)
            self.wall += wall_step
            return exchange_stamp

        timed_transport = clock.observe_transport(transport)

        async def fetch_time():
            self.advance(queue)
            return await timed_transport()

        return SimpleNamespace(has={'fetchTime': True}, fetch_time=fetch_time)


class ClockTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, clock, client):
        clock.start(client)
        await clock._task

    async def confirm(self, timeline, clock, client):
        await self.probe(clock, client)
        timeline.advance(CONFIRM_SECONDS * 1000)
        await self.probe(clock, client)

    async def test_requires_two_compatible_samples_and_corrects_only_exchange_stamp(self):
        timeline = Timeline()
        clock = timeline.calibration()
        client = timeline.exchange(clock, offset=1200, rtt=100, queue=2500)
        await self.probe(clock, client)
        self.assertEqual(clock.status()['state'], 'sampling')
        self.assertEqual(clock.correct(NOW + 1200), NOW + 1200)
        timeline.advance(CONFIRM_SECONDS * 1000)
        await self.probe(clock, client)
        status = clock.status()
        self.assertEqual((status['state'], status['offsetMs'], status['uncertaintyMs'], status['rttMs']),
                         ('calibrated', 1200, 51, 100))
        self.assertTrue(status['warning'])
        received = timeline.wall
        self.assertFalse(fresh(received + 1200, received, 1500))
        self.assertTrue(fresh(clock.correct(received + 1200), received, 1500))
        self.assertFalse(fresh(clock.correct(received + 1200 - 2000), received, 1500))
        self.assertIsNone(clock.correct(None))

    async def test_rejects_slow_or_inconsistent_samples_and_reconfirms(self):
        timeline = Timeline()
        clock = timeline.calibration()
        await self.probe(clock, timeline.exchange(clock, offset=5000, rtt=800))
        self.assertEqual(clock.status()['lastError'], 'slow_response')
        self.assertIsNone(clock.offset())
        timeline.advance(60_000)
        await self.confirm(timeline, clock, timeline.exchange(clock, offset=-600))
        self.assertEqual(clock.correct(NOW - 600), NOW)
        timeline.advance(REFRESH_SECONDS * 1000)
        await self.probe(clock, timeline.exchange(clock, offset=850))
        self.assertEqual(clock.status()['lastError'], 'offset_changed')
        self.assertIsNone(clock.offset())
        timeline.advance(CONFIRM_SECONDS * 1000)
        await self.probe(clock, timeline.exchange(clock, offset=850))
        self.assertEqual(clock.offset(), 850)

    async def test_invalidates_on_local_rollback_and_in_request_wall_jump(self):
        timeline = Timeline()
        clock = timeline.calibration()
        client = timeline.exchange(clock, offset=800)
        await self.confirm(timeline, clock, client)
        timeline.wall -= 900
        self.assertEqual(clock.correct(NOW + 800), NOW + 800)
        self.assertEqual(clock.status()['lastError'], 'local_clock_step')
        await self.probe(clock, timeline.exchange(clock, offset=800, wall_step=200))
        self.assertEqual(clock.status()['state'], 'unavailable')
        self.assertEqual(clock.status()['lastError'], 'local_clock_step')

    async def test_timeout_keeps_only_still_valid_sample(self):
        timeline = Timeline()
        clock = timeline.calibration()
        client = timeline.exchange(clock, offset=800)
        await self.confirm(timeline, clock, client)
        timeline.advance(REFRESH_SECONDS * 1000)
        client = SimpleNamespace(has={'fetchTime': True}, fetch_time=AsyncMock(side_effect=TimeoutError))
        await self.probe(clock, client)
        self.assertEqual(clock.status()['lastError'], 'timeout')
        self.assertEqual(clock.offset(), 800)
        timeline.advance(MAX_AGE_SECONDS * 1000)
        self.assertEqual(clock.status()['lastError'], 'sample_expired')
        self.assertIsNone(clock.offset())

    async def test_unsupported_or_coarse_clients_use_unmodified_timestamps(self):
        timeline = Timeline()
        client = SimpleNamespace(has={}, fetch_time=AsyncMock())
        clock = timeline.calibration()
        clock.start(client)
        self.assertEqual(clock.status()['state'], 'unsupported')
        self.assertEqual(clock.correct(NOW), NOW)
        for venue, state in (('whitebit', 'coarse'), ('edgex', 'unsupported'), ('extended', 'unsupported')):
            clock = timeline.calibration(venue)
            client.has['fetchTime'] = True
            clock.start(client)
            self.assertEqual(clock.status()['state'], state)
            self.assertEqual(clock.correct(NOW), NOW)
        client.fetch_time.assert_not_awaited()

    async def test_ccxt_probe_uses_public_transport_and_excludes_rest_budget_queue(self):
        timeline = Timeline()
        clock = timeline.calibration()
        queued = AsyncMock(side_effect=lambda **kwargs: timeline.advance(2700))
        budget = SimpleNamespace(wait=queued)

        async def transport(_client, url, *args, **kwargs):
            self.assertTrue(url.endswith('/v5/market/time'))
            time = timeline.wall + 900 + 35
            timeline.advance(70)
            return {'time': time}

        with patch.object(ccxt.bybit, 'fetch', transport):
            source = Source('bybit', 'swap', budget, clock=clock)
            source.open(None)
            try:
                await self.confirm(timeline, clock, source.catalog_client)
                self.assertEqual(clock.status()['rttMs'], 70)
                self.assertEqual(source.correct_timestamp(NOW + 900), NOW)
                self.assertEqual([call.kwargs for call in queued.await_args_list],
                                 [{'background': True}, {'background': True}])
            finally:
                await source.close()

    async def test_close_cancels_a_running_probe(self):
        timeline = Timeline()
        clock = timeline.calibration()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def transport():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        client = SimpleNamespace(has={'fetchTime': True}, fetch_time=clock.observe_transport(transport))
        clock.start(client)
        async with asyncio.timeout(2):
            await started.wait()
            await clock.close()
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(clock.offset())
