import unittest

from validate_mexc_full_replay import Comparison, full_top


class FullProbeTests(unittest.TestCase):
    def setUp(self):
        self.state = Comparison("ATOM_USDT")
        self.levels = dict(bids=[[10, 3]], asks=[[11, 2]], version=5)

    def test_duplicate_snapshot_cannot_renew_age(self):
        self.state.update("full", dict(data=dict(self.levels, cts=1000)), 1001)
        self.state.update("full", dict(data=dict(self.levels, cts=4000)), 4001)
        self.state.sample(4002)
        self.assertEqual(self.state.counts["full_fresh"], 0)
        self.assertEqual(self.state.counts["full_duplicate_versions"], 1)
        with self.assertRaises(ValueError):
            self.state.update("full", dict(data=dict(self.levels, bids=[[10, 9]], cts=4003)), 4003)

    def test_shared_version_compares_sizes_and_clocks(self):
        self.state.update("incremental", dict(data=dict(begin=6, end=6, version=6, bids=[[10, 4]], cts=2000)), 2010)
        self.state.update("seed", dict(data=self.levels), 2020)
        self.state.update("full", dict(data=dict(self.levels, version=6, bids=[[10, 4]], cts=2030)), 2040)
        result = self.state.result()
        self.assertEqual(result["matched_versions"], 1)
        self.assertEqual(result["matches"]["equal_prices_sizes"], 1)
        self.assertEqual(result["matches"]["equal_source_time"], 0)
        self.state.sample(5001)
        self.assertEqual(self.state.counts["full_only_same_version"], 1)

    def test_bad_snapshot_or_incremental_gap_rejected(self):
        with self.assertRaisesRegex(ValueError, "valid cts"):
            self.state.update("full", dict(data=self.levels, ts=2000), 2010)
        for bids in ([], [[10, 0]], [[12, 1]], [[9, 1], [10, 3]]):
            with self.subTest(bids=bids), self.assertRaises(ValueError):
                full_top(dict(self.levels, bids=bids))
        self.state.update("seed", dict(data=self.levels), 1000)
        with self.assertRaisesRegex(ValueError, "Depth gap"):
            self.state.update("incremental", dict(data=dict(begin=7, end=7, version=7), ts=1010), 1020)
