import json
import unittest
from pathlib import Path
from unittest.mock import patch

from market_monitor import classification
from market_monitor.funding_fields import funding_fields

FIXTURES = json.loads((Path(__file__).parent / 'fixtures/market_catalog_fields.json').read_text(encoding='utf-8'))
HOUR = 3_600_000
NEXT = 1_794_412_800_000
NOW = NEXT - 1000
SYMBOL = 'AIGENSYN/USDT:USDT'


class FundingAdapterTests(unittest.TestCase):
    def test_recorded_gate_and_bybit_market_fields(self):
        fields = FIXTURES['fundingCatalog']
        gate = funding_fields({}, {'info': fields['gate']}, 'gate', NOW)
        self.assertEqual(gate, dict(rate=None, hours=4, next=NEXT, cap=2, floor=-2))
        gate = funding_fields({'info': fields['gate']}, {}, 'gate', NOW)
        self.assertEqual(gate, dict(rate=.005, hours=4, next=NEXT, cap=2, floor=-2))
        bybit = funding_fields({'info': {'fundingRate': '0', 'nextFundingTime': str(NEXT)}},
                               {'info': fields['bybit']}, 'bybit', NOW)
        self.assertEqual(bybit, dict(rate=0, hours=4, next=NEXT, cap=2.5, floor=-2.5))

    def test_remaining_funding_formats_and_units(self):
        cases = [
            ('bitget', {'fundingRate': '-.001', 'info': {'nextUpdate': str(NEXT), 'maxFundingRate': '.02',
                                                         'minFundingRate': '-.01'}}, {'fundInterval': 4},
             None, dict(rate=-.1, hours=4, next=NEXT, cap=2, floor=-1)),
            ('mexc', {'info': {'collectCycle': 1, 'fundingRate': '0', 'nextSettleTime': NEXT,
                               'maxFundingRate': '.02', 'minFundingRate': '-.01'}}, {},
             None, dict(rate=0, hours=1, next=NEXT, cap=2, floor=-1)),
            ('binanceusdm', {'fundingRate': '.001', 'fundingTimestamp': NEXT}, {},
             {'fundingIntervalHours': 2, 'adjustedFundingRateCap': '.02', 'adjustedFundingRateFloor': '-.01'},
             dict(rate=.1, hours=2, next=NEXT, cap=2, floor=-1)),
            ('aster', {'fundingRate': '.001', 'nextFundingTimestamp': NEXT}, {},
             {'fundingIntervalHours': 4, 'fundingFeeCap': '.02', 'fundingFeeFloor': '-.01'},
             dict(rate=.1, hours=4, next=NEXT, cap=2, floor=-1)),
            ('kucoinfutures', {'fundingRate': '.001', 'info': {'currentGranularity': 4 * HOUR,
                                'fundingTime': NEXT, 'fundingRateCap': '.02', 'fundingRateFloor': '-.01'}}, {},
             None, dict(rate=.1, hours=4, next=NEXT, cap=2, floor=-1)),
            ('whitebit', {'fundingRate': '.001', 'fundingTimestamp': NEXT, 'info': {
                'funding_interval_minutes': 120, 'funding_cap': '.02', 'funding_floor': '-.01'}}, {},
             None, dict(rate=.1, hours=2, next=NEXT, cap=2, floor=-1)),
            ('edgex', {'fundingRate': '.009', 'interval': '8h', 'info': {
                'fundingRateIntervalMin': 60, 'fundingRate': '.001', 'fundingTime': NEXT - HOUR}}, {},
             None, dict(rate=.1, hours=1, next=NEXT, cap=None, floor=None)),
            ('htx', {'info': {'funding_rate': '.001', 'funding_time': str(NEXT),
                              'max_funding_rate': '.02', 'min_funding_rate': '-.01'}}, {},
             {'settlement_date': NEXT, 'settlement_period': 4}, dict(rate=.1, hours=4, next=NEXT, cap=2, floor=-1)),
            ('okx', {'info': {'fundingRate': '.001', 'fundingTime': str(NEXT), 'nextFundingTime': str(NEXT + 4 * HOUR),
                              'maxFundingRate': '.02', 'minFundingRate': '-.01'}}, {},
             None, dict(rate=.1, hours=4, next=NEXT, cap=2, floor=-1)),
            ('extended', {'fundingRate': '.001', 'interval': '1h', 'fundingTimestamp': NEXT}, {},
             None, dict(rate=.1, hours=1, next=NEXT, cap=None, floor=None)),
        ]
        for venue, raw, instrument, schedule_info, expected in cases:
            with self.subTest(venue=venue):
                schedule = None if schedule_info is None else {SYMBOL: {'info': schedule_info}}
                market = {'symbol': SYMBOL, 'info': instrument}
                self.assertEqual(funding_fields(raw, market, venue, NOW, schedule), expected)
                if venue in ('mexc', 'htx', 'okx'):
                    after = funding_fields(raw, market, venue, NEXT, schedule)
                    self.assertEqual((after['rate'], after['hours'], after['next']), (None, None, None))


class TradfiTests(unittest.TestCase):
    @staticmethod
    def swap(base, info):
        return dict(base=base, quote='USDT', settle='USDT', swap=True, active=True, linear=True,
                    contractSize=1, info=info)

    def test_observed_catalog_markers_and_venue_specific_classes(self):
        cases = FIXTURES['classification'] + [
            ('bybit', 'TEST', {'symbolType': 'ETF'}, False),
            ('htx', 'TEST', {'tradfi_labels': ['TEST']}, False),
            ('mexc', 'TEST', {'conceptPlate': ['mc-trade-zone-tradfi']}, False),
            ('whitebit', 'TEST', {'type': 'tradfiFutures'}, False),
        ]
        for venue, base, info, expected in cases:
            with self.subTest(venue=venue, info=info):
                self.assertEqual(classification.valid_market(self.swap(base, info), 'swap', venue=venue), expected)

    def test_unknown_classes_are_reported_once_without_changing_admission(self):
        with patch.object(classification, '_class_notices', set()), \
             patch.object(classification, 'MAX_CLASS_NOTICES', 2), \
             self.assertLogs('market_monitor.classification', level='WARNING') as logs:
            for value in ('NEW_TYPE', 'NEW_TYPE', 'SECOND_TYPE', 'THIRD_TYPE'):
                market = self.swap('TEST', {'symbolType': value})
                self.assertTrue(classification.valid_market(market, 'swap', venue='bybit'))
        self.assertEqual(len(logs.output), 2)
        self.assertIn("symbolType='NEW_TYPE'", logs.output[0])
