import unittest

import orjson

from market_monitor.market import fresh
from market_monitor.snapshot_wire import encode_compact
from market_monitor.thresholds import (
    BOOK_MAX_AGE_MS,
    EARLY_QUOTE_MAX_AGE_MS,
    FUTURES_QUOTE_CONFIRMATION_AGE_MS,
    FUTURE_CLOCK_TOLERANCE_MS,
    MAX_PRICE_RATIO,
    PUBLIC_CONFIG,
    QUOTE_MAX_AGE_MS,
    RECEIPT_TIME_SKEW_MS,
    SOURCE_TIME_SKEW_MS,
)


class ThresholdTests(unittest.TestCase):
    def test_public_config_matches_the_collector_thresholds(self):
        self.assertEqual(PUBLIC_CONFIG, {
            'quoteMaxAgeMs': QUOTE_MAX_AGE_MS,
            'sourceTimeSkewMs': SOURCE_TIME_SKEW_MS,
            'receiptTimeSkewMs': RECEIPT_TIME_SKEW_MS,
            'bookMaxAgeMs': BOOK_MAX_AGE_MS,
            'earlyQuoteMaxAgeMs': EARLY_QUOTE_MAX_AGE_MS,
            'futuresQuoteConfirmationAgeMs': FUTURES_QUOTE_CONFIRMATION_AGE_MS,
            'maxPriceRatio': MAX_PRICE_RATIO,
            'futureClockToleranceMs': FUTURE_CLOCK_TOLERANCE_MS,
        })

    def test_quote_freshness_uses_the_shared_default_age(self):
        now = 10_000_000
        self.assertTrue(fresh(now - QUOTE_MAX_AGE_MS, now))
        self.assertFalse(fresh(now - QUOTE_MAX_AGE_MS - 1, now))

    def test_snapshot_and_metadata_both_publish_the_same_config(self):
        body, _version, _metadata, metadata_body = encode_compact(
            {'markets': {}, 'config': PUBLIC_CONFIG}, None, None, 'threshold-test',
        )
        self.assertEqual(orjson.loads(body)['config'], PUBLIC_CONFIG)
        self.assertEqual(orjson.loads(metadata_body)['config'], PUBLIC_CONFIG)


if __name__ == '__main__':
    unittest.main()
