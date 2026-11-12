import asyncio
import unittest
from unittest.mock import patch

from market_monitor.futures_ws import FuturesBookFeed
from market_monitor.ws_reconnect import ReconnectingFeed, retry_delay


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_backoff_caps_resets_after_healthy_connection_and_stops(self):
        class Feed(ReconnectingFeed):
            def __init__(self):
                super().__init__()
                self.attempts = 0
                self.at_last_attempt = asyncio.Event()

            async def read(self):
                self.attempts += 1
                if self.attempts == 7:
                    self.mark_connected()
                    self.connected_at -= 60
                    self.connected = False
                if self.attempts == 8:
                    self.at_last_attempt.set()
                    await asyncio.Future()

        feed = Feed()
        with patch('market_monitor.ws_reconnect.retry_delay', return_value=0) as delay:
            try:
                feed.start_reader()
                reader = feed.reader
                feed.start_reader()
                self.assertIs(feed.reader, reader)
                await asyncio.wait_for(feed.at_last_attempt.wait(), 1)
                self.assertEqual([call.args[0] for call in delay.call_args_list], [1, 2, 4, 8, 16, 30, 1])
                self.assertEqual(feed.reconnects, 7)
            finally:
                await feed.close()
        self.assertTrue(reader.cancelled())
        self.assertIsNone(feed.reader)

    async def test_empty_set_stops_futures_during_backoff(self):
        disconnected = asyncio.Event()
        feed = FuturesBookFeed('mexc', disconnected.set)
        with patch.object(feed, 'connection', side_effect=TimeoutError), \
             patch('market_monitor.ws_reconnect.retry_delay', return_value=30), \
             self.assertLogs('market_monitor.futures_ws', level='WARNING'):
            try:
                feed.start(None, {'BTC_USDT'})
                reader = feed.reader
                await asyncio.wait_for(disconnected.wait(), 1)
                feed.start(None, set())
                with self.assertRaises(asyncio.CancelledError):
                    await reader
                self.assertFalse(feed.symbols)
                self.assertEqual(feed.reconnects, 0)
            finally:
                await feed.close()

    def test_retry_delay_varies_and_stays_below_cap(self):
        with patch('market_monitor.ws_reconnect.uniform', side_effect=(.8, 1.2, 1.2)):
            self.assertEqual(retry_delay(1), .8)
            self.assertEqual(retry_delay(1), 1.2)
            self.assertEqual(retry_delay(30), 30)
