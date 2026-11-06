import unittest

from arbitrage.mexc_book import BookGap, MexcBook


MARKET = dict(id="BTC_USDT", symbol="BTC/USDT:USDT", base="BTC", quote="USDT", contractSize=0.0001)


def update(begin, end=None, bids=None, asks=None):
    end = begin if end is None else end
    return dict(channel="push.depth", ts=10100, data=dict(begin=begin, end=end, version=end,
                bids=bids or [], asks=asks or [], cts=10000))


class MexcBookTests(unittest.TestCase):
    def setUp(self):
        self.book = MexcBook()
        self.book.initialize(dict(version=100, bids=[[100, 10, 1], [99, 20, 2]],
                                  asks=[[101, 10, 1], [102, 20, 2]]))

    def test_contiguous_range_produces_verified_bbo(self):
        quote = self.book.apply(update(101, 105, bids=[[100.5, 5, 1]]), MARKET, 10100)
        self.assertEqual((quote.bid, quote.ask, quote.source), (100.5, 101, "ws_depth_verified"))
        self.assertEqual(quote.quality(10100), "ok")
        self.assertEqual(self.book.version, 105)

    def test_absolute_size_and_zero_delete(self):
        self.book.apply(update(101, bids=[[100, 3, 1]]), MARKET, 10100)
        self.assertEqual(self.book.bids[100], 3)
        quote = self.book.apply(update(102, bids=[[100, 0, 0]]), MARKET, 10100)
        self.assertEqual(quote.bid, 99)

    def test_duplicate_does_not_refresh_quote(self):
        self.assertIsNone(self.book.apply(update(95, 100), MARKET, 10100))
        self.assertEqual(self.book.version, 100)

    def test_gap_requires_new_snapshot(self):
        with self.assertRaises(BookGap):
            self.book.apply(update(102, 105), MARKET, 10100)

    def test_overlapping_range_can_bridge_snapshot(self):
        self.assertIsNotNone(self.book.apply(update(98, 104), MARKET, 10100))
        self.assertEqual(self.book.version, 104)

    def test_empty_update_advances_version(self):
        quote = self.book.apply(update(101), MARKET, 10100)
        self.assertEqual(quote.bid, 100)
        self.assertEqual(self.book.version, 101)

    def test_snapshot_boundary_and_crossed_book_fail_closed(self):
        for message in [update(101, bids=[[100, 0], [99, 0], [98, 1]]),
                        update(101, asks=[[101, 0], [102, 0]]),
                        update(101, bids=[[103, 1]])]:
            self.setUp()
            with self.assertRaises(BookGap):
                self.book.apply(message, MARKET, 10100)

    def test_invalid_data_and_missing_snapshot(self):
        with self.assertRaises(BookGap):
            MexcBook().apply(update(101), MARKET, 10100)
        for value in [float("nan"), float("inf"), -1]:
            with self.assertRaises(BookGap):
                self.book.apply(update(101, bids=[[100, value]]), MARKET, 10100)

    def test_malformed_range_rejected(self):
        with self.assertRaises(BookGap):
            self.book.apply(update(105, 102), MARKET, 10100)
