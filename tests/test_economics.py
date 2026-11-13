import unittest
from decimal import Decimal

from market_monitor.economics import EconomicBlock, check_limits, fill


class EconomicErrorsTests(unittest.TestCase):
    def test_insufficient_depth_reports_readable_error(self):
        with self.assertRaisesRegex(EconomicBlock, "Недостаточная глубина стакана"):
            fill([(Decimal("10"), Decimal("1"))], Decimal("2"))

    def test_order_limit_errors_identify_the_limit(self):
        market = {"limits": {"amount": {"min": "2", "max": "5"}}}
        with self.assertRaisesRegex(EconomicBlock, "Меньше минимума биржи: количество"):
            check_limits(market, Decimal("1"), Decimal("10"))
        with self.assertRaisesRegex(EconomicBlock, "Больше максимума биржи: количество"):
            check_limits(market, Decimal("6"), Decimal("60"))
