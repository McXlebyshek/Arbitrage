import json
from pathlib import Path
import unittest

from monitor import Monitor


class MarketMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_changes_only_on_static_change_and_retains_previous_version(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        snapshot = dict(monitor.snapshot, generatedAt=1, markets={
            'BTC': [dict(id='bybit.swap.BTC.USDT', symbol='BTC/USDT:USDT', bid=1)],
        })
        await monitor.publish_snapshot(snapshot)
        initial = monitor.current_metadata
        self.assertEqual(json.loads(monitor.snapshot_response[0]), snapshot)
        self.assertEqual(json.loads(monitor.compact_snapshot_response[0])['marketRows'][0][0], 1)

        await monitor.publish_snapshot(dict(snapshot, generatedAt=2, markets={
            'BTC': [dict(snapshot['markets']['BTC'][0], bid=2)],
        }))
        self.assertIs(monitor.current_metadata, initial)
        self.assertEqual(json.loads(monitor.compact_snapshot_response[0])['marketRows'][0][0], 2)

        await monitor.publish_snapshot(dict(snapshot, generatedAt=3, markets={
            'BTC': [dict(snapshot['markets']['BTC'][0], symbol='XBT/USDT:USDT')],
        }))
        self.assertNotEqual(monitor.current_metadata.version, initial.version)
        self.assertEqual(monitor.market_metadata[initial.version], initial.body)

    async def test_unknown_market_fields_keep_full_representation(self):
        monitor = Monitor((), Path('unused.sqlite3'))
        snapshot = dict(monitor.snapshot, generatedAt=1, markets={'BTC': [{'id': 'a', 'newField': 1}]})
        await monitor.publish_snapshot(snapshot)
        self.assertIsNone(monitor.compact_snapshot_response)
        self.assertEqual(json.loads(monitor.snapshot_response[0]), snapshot)
